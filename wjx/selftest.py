"""内置自测：规则引擎、答案映射、mock 后端、调度器条件/优先级。

运行: python main.py selftest
"""

from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, List, Tuple

from . import mockserver
from .drivers.base import build_form_payload, classify_post_submit
from .models import AppConfig, Task, TaskStatus
from .rules import evaluate_rule, safe_evaluate_rule, validate_rule
from .scheduler import Scheduler

PASS, FAIL = "PASS", "FAIL"
_results: List[Tuple[str, str, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((PASS if cond else FAIL, name, detail))
    print(f"[{PASS if cond else FAIL}] {name}" + (f"  -> {detail}" if detail else ""))


# --------------------------------------------------------------------------- #
def test_rules() -> None:
    a = Task(name="问卷A", url="http://x/A")
    a.status = TaskStatus.SUCCESS.value
    b = Task(name="问卷B", url="http://x/B")
    b.status = TaskStatus.FAILED.value
    b.attempts = 2
    tasks = [a, b]

    _check("空表达式由调用方视为无条件（safe_evaluate 返回真）", safe_evaluate_rule("", tasks)[0])
    _check("submitted() 识别成功", evaluate_rule("submitted('问卷A')", tasks))
    _check("not submitted() 取反", not evaluate_rule("not submitted('问卷A')", tasks))
    _check("status 比较", evaluate_rule("status('问卷B') == 'failed'", tasks))
    _check("attempts 数值比较", evaluate_rule("attempts('B') < 3", tasks))
    _check("and/or 优先级", evaluate_rule("submitted('A') or status('B') == 'failed' and attempts('B') > 5", tasks))
    _check("括号改变优先级", not evaluate_rule("(submitted('A') or status('B') == 'failed') and attempts('B') > 5", tasks))
    err = validate_rule("status('A') ==", tasks)
    _check("语法错误可检出", bool(err), err)
    ok, warn = safe_evaluate_rule("bogus('A')", tasks, default=True)
    _check("未知函数回退放行并给出警告", ok and bool(warn), warn)


def test_answers() -> None:
    fields = [
        {"key": "q1", "type": "radio",
         "options": [{"value": "1", "label": "方案一"}, {"value": "2", "label": "方案二"}]},
        {"key": "q2", "type": "checkbox",
         "options": [{"value": "a", "label": "阅读"}, {"value": "b", "label": "运动"}]},
        {"key": "q3", "type": "text", "options": []},
    ]
    payload = build_form_payload(fields, {"q1": "2", "q2": ["阅读", "b"], "q3": "你好"})
    _check("单选按序号映射", payload.get("q1") == "2", str(payload))
    _check("多选混合序号与文案", payload.get("q2") == "a,b", str(payload.get("q2")))
    _check("填空原样保留", payload.get("q3") == "你好")

    payload2 = build_form_payload(fields, {"q1": "方案一"})
    _check("单选按文案映射", payload2.get("q1") == "1", str(payload2))

    payload3 = build_form_payload(fields, {"q1": "9"})
    _check("无效选项被记录", "9" in str(payload3.get("__unresolved__", "")), str(payload3))


def test_classify() -> None:
    r, _ = classify_post_submit("http://x/vm/1.aspx", "http://x/join/1_done.aspx")
    _check("URL 跳转判成功", r == "success")
    r, _ = classify_post_submit("http://x/vm/1.aspx", "http://x/vm/1.aspx", "抱歉，该问卷填写人数过多")
    _check("停留原页+人数过多判失败(full)", r == "full")
    r, _ = classify_post_submit("http://x/vm/1.aspx", "http://x/vm/1.aspx", "第3题 请填写")
    _check("停留原页+必填提示判校验失败", r == "validation")
    r, _ = classify_post_submit("http://x/vm/1.aspx", "http://x/vm/1.aspx")
    _check("停留原页无提示为 unknown", r == "unknown")


def test_reminder() -> None:
    """提醒必须在"提前 N 分钟"时触发一次，且绝不提前执行任务。"""
    from . import notify

    calls: List[tuple] = []
    original_alert, original_info = notify.alert, notify.info
    notify.alert = lambda title, message, **kw: calls.append((title, message))  # type: ignore
    notify.info = lambda title, message: calls.append((title, message))  # type: ignore
    import wjx.scheduler as sched_mod

    sched_mod.notify.alert, sched_mod.notify.info = notify.alert, notify.info
    try:
        cfg = AppConfig()
        task = Task(
            name="提醒测试",
            url="http://127.0.0.1:9/vm/A.aspx",
            # 距开始 4 分 55 秒 => 提前 5 分钟的提醒窗口已经打开
            start_at=(datetime.now() + timedelta(minutes=4, seconds=55)).replace(microsecond=0)
            .strftime("%Y-%m-%d %H:%M:%S"),
            reminder_minutes=5, prepare_minutes=5,
        )
        cfg.tasks = [task]
        sched = Scheduler(cfg)
        events: List[dict] = []
        sched.on_event = events.append
        for _ in range(3):
            sched._tick()
        _check("提前 5 分钟触发提醒", bool(calls), str(calls[:1]))
        _check("提醒产生 reminder 事件",
               any(e.get("kind") == "reminder" for e in events),
               str([e.get("kind") for e in events]))
        _check("提醒阶段不提前执行任务", task.attempts == 0, f"attempts={task.attempts}")

        # 同一计划时间只提醒一次
        calls.clear()
        for _ in range(3):
            sched._tick()
        _check("同一计划时间只提醒一次", not calls, f"重复提醒 {len(calls)} 次")

        # 尚未进入提醒窗口的任务不应提醒
        cfg2 = AppConfig()
        far = Task(name="远处的任务", url="http://127.0.0.1:9/vm/A.aspx",
                   start_at=(datetime.now() + timedelta(minutes=20)).replace(microsecond=0)
                   .strftime("%Y-%m-%d %H:%M:%S"),
                   reminder_minutes=5)
        cfg2.tasks = [far]
        calls.clear()
        sched2 = Scheduler(cfg2)
        sched2._tick()
        _check("未到提醒窗口不打扰", not calls, str(calls))
    finally:
        notify.alert, notify.info = original_alert, original_info
        sched_mod.notify.alert, sched_mod.notify.info = original_alert, original_info


# --------------------------------------------------------------------------- #
def test_backend_and_scheduler() -> None:
    httpd = mockserver.create_server(port=8791)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.3)
    base = "http://127.0.0.1:8791"
    try:
        cfg = AppConfig()
        cfg.settings.backend = "mock"
        cfg.settings.mock_base_url = base

        # 成功路径
        cfg.tasks = []
        t_ok = Task(name="成功卷", url=f"{base}/vm/A.aspx", answers={"q1": "1", "q2": "ok"})
        cfg.tasks.append(t_ok)
        sched = Scheduler(cfg)
        sched._start_task(t_ok, "自测")
        _join(sched, t_ok)
        _check("mock 后端提交成功", t_ok.status == TaskStatus.SUCCESS.value,
               t_ok.last_run.summary() if t_ok.last_run else "")

        # 人数已满
        t_full = Task(name="满员卷", url=f"{base}/vm/B.aspx?force=full", answers={"q1": "2", "q2": "full"})
        cfg.tasks.append(t_full)
        sched._start_task(t_full, "自测")
        _join(sched, t_full)
        _check("mock 后端人数已满判失败", t_full.status == TaskStatus.FAILED.value
               and t_full.last_run.result == "full",
               t_full.last_run.summary() if t_full.last_run else "")

        # 必填缺失
        t_bad = Task(name="缺填卷", url=f"{base}/vm/A.aspx", answers={"q1": "1"})
        cfg.tasks.append(t_bad)
        sched._start_task(t_bad, "自测")
        _join(sched, t_bad)
        _check("必填缺失判校验失败", t_bad.last_run.result == "validation",
               t_bad.last_run.summary() if t_bad.last_run else "")

        # 打开失败
        t_dead = Task(name="坏网址", url="http://127.0.0.1:9/vm/X.aspx", answers={})
        cfg.tasks.append(t_dead)
        sched._start_task(t_dead, "自测")
        _join(sched, t_dead)
        _check("无法连接时报 navigation_error", t_dead.last_run.result == "navigation_error",
               t_dead.last_run.result if t_dead.last_run else "")

        # 条件与优先级：A 成功后 B 跳过
        cfg2 = AppConfig()
        cfg2.settings.backend = "mock"
        cfg2.settings.mock_base_url = base
        cfg2.settings.respect_min_interval = 0
        a = Task(name="问卷A", url=f"{base}/vm/A.aspx", answers={"q1": "1", "q2": "A"},
                 start_at=_past(), group="G", priority=10)
        b = Task(name="问卷B", url=f"{base}/vm/B.aspx", answers={"q1": "2", "q2": "B"},
                 start_at=_past(1), group="G", priority=5, start_rule="not submitted('问卷A')")
        cfg2.tasks = [a, b]
        s2 = Scheduler(cfg2)
        _tick_until(s2, [a, b])
        _check("A 成功后 B 被跳过（互斥+条件）",
               a.status == TaskStatus.SUCCESS.value and b.status == TaskStatus.SKIPPED.value,
               f"A={a.status} B={b.status}")

        # A 失败 -> B 继续
        cfg3 = AppConfig()
        cfg3.settings.backend = "mock"
        cfg3.settings.mock_base_url = base
        cfg3.settings.respect_min_interval = 0
        a3 = Task(name="问卷A", url=f"{base}/vm/A.aspx?force=full", answers={"q1": "1", "q2": "A"},
                  start_at=_past(), group="G", priority=10)
        b3 = Task(name="问卷B", url=f"{base}/vm/B.aspx?force=ok", answers={"q1": "2", "q2": "B"},
                  start_at=_past(1), group="G", priority=5, start_rule="not submitted('问卷A')")
        cfg3.tasks = [a3, b3]
        s3 = Scheduler(cfg3)
        _tick_until(s3, [a3, b3])
        _check("A 失败后 B 继续填写成功",
               a3.last_run is not None and a3.last_run.result == "full"
               and b3.status == TaskStatus.SUCCESS.value,
               f"A={a3.status}/{a3.last_run.result if a3.last_run else '-'} B={b3.status}")

        # 优先级：同组同时到点，只跑优先级高的
        cfg4 = AppConfig()
        cfg4.settings.backend = "mock"
        cfg4.settings.mock_base_url = base
        cfg4.settings.respect_min_interval = 0
        hi = Task(name="高优先", url=f"{base}/vm/A.aspx", answers={"q1": "1", "q2": "hi"},
                  start_at=_past(), group="P", priority=99)
        lo = Task(name="低优先", url=f"{base}/vm/B.aspx?force=ok", answers={"q1": "2", "q2": "lo"},
                  start_at=_past(), group="P", priority=1)
        cfg4.tasks = [hi, lo]
        s4 = Scheduler(cfg4)
        s4._tick()
        time.sleep(1.2)
        s4._tick()
        # 优先级：同组同时到点，只跑优先级高的（低优先仅可能处于"待定/跳过"）
        _check("同组内只启动最高优先级",
               hi.status in (TaskStatus.SUCCESS.value, TaskStatus.RUNNING.value)
               and lo.attempts == 0 and lo.status != TaskStatus.SUCCESS.value,
               f"hi={hi.status} lo={lo.status} lo.attempts={lo.attempts}")
        _join(s4, hi)

        # 过期任务不再执行
        cfg5 = AppConfig()
        cfg5.settings.backend = "mock"
        cfg5.settings.mock_base_url = base
        old = Task(name="过期卷", url=f"{base}/vm/A.aspx", answers={"q1": "1", "q2": "x"},
                   start_at=_past(600), window_minutes=5)
        cfg5.tasks = [old]
        s5 = Scheduler(cfg5)
        s5._tick()
        time.sleep(0.5)
        _check("超出窗口的过期任务不执行", old.attempts == 0, f"attempts={old.attempts}")
    finally:
        httpd.shutdown()
        httpd.server_close()


def _past(minutes: int = 0) -> str:
    return (datetime.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def _join(sched: Scheduler, task: Task, timeout: float = 15) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        th = sched._running.get(task.id)
        if th is None:
            if task.status_enum != TaskStatus.RUNNING:
                return
        else:
            th.join(timeout=0.5)
            if not th.is_alive():
                return
        time.sleep(0.1)


def _tick_until(sched: Scheduler, tasks: List[Task], budget: float = 25) -> None:
    deadline = time.time() + budget
    while time.time() < deadline:
        sched._tick()
        time.sleep(0.25)
        if all(t.status_enum in (TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.SKIPPED, TaskStatus.ERROR)
               for t in tasks):
            for t in tasks:
                _join(sched, t)
            sched._tick()
            time.sleep(0.2)
            return
    for t in tasks:
        _join(sched, t, timeout=5)


# --------------------------------------------------------------------------- #
def run() -> int:
    print("=" * 70)
    print("wjx-auto 自测")
    print("=" * 70)
    test_rules()
    test_answers()
    test_classify()
    test_reminder()
    test_backend_and_scheduler()
    failed = [r for r in _results if r[0] == FAIL]
    print("-" * 70)
    print(f"共 {len(_results)} 项，通过 {len(_results) - len(failed)} 项，失败 {len(failed)} 项")
    for status, name, detail in failed:
        print(f"  {status}: {name} {detail}")
    return 0 if not failed else 1
