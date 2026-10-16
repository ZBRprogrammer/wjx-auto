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
        sched = Scheduler(cfg, config_path=False)
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
        sched2 = Scheduler(cfg2, config_path=False)
        sched2._tick()
        _check("未到提醒窗口不打扰", not calls, str(calls))
    finally:
        notify.alert, notify.info = original_alert, original_info
        sched_mod.notify.alert, sched_mod.notify.info = original_alert, original_info


def test_config_io() -> None:
    """配置读写回归：UTF-8 BOM、答案文本解析、原子写与备份。"""
    import json
    import shutil
    from pathlib import Path

    from . import config as config_mod
    from .paths import RUN_DIR

    # 用项目内目录，避免系统临时目录权限限制（tempfile 会 chmod，沙箱下可能失败）
    tmp_root = RUN_DIR / "selftest-tmp"
    if tmp_root.exists():
        shutil.rmtree(tmp_root, ignore_errors=True)
    tmp_root.mkdir(parents=True, exist_ok=True)
    try:
        _config_io_checks(tmp_root, json, config_mod, Path)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def _config_io_checks(tmp_root, json, config_mod, Path) -> None:
    path = Path(tmp_root) / "tasks.json"
    # 模拟记事本 / PowerShell 写出的带 BOM JSON
    payload = {
        "version": 1,
        "settings": {"backend": "mock"},
        "tasks": [{"id": "x1", "name": "带BOM的任务", "url": "http://x/1", "answer_text": "q1 = 1\nq2 = 文本"}],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8-sig")
    cfg = config_mod.load_config(path)
    _check("带 BOM 的配置文件可正常读取", len(cfg.tasks) == 1 and cfg.tasks[0].name == "带BOM的任务",
           f"tasks={len(cfg.tasks)}")
    _check("answer_text 解析为答案字典",
           cfg.tasks[0].answers.get("q1") == "1" and cfg.tasks[0].answers.get("q2") == "文本",
           str(cfg.tasks[0].answers))

    # 原子写 + 备份 + 往返一致
    cfg.tasks[0].priority = 42
    config_mod.save_config(cfg, path)
    cfg2 = config_mod.load_config(path)
    _check("保存后往返一致", cfg2.tasks[0].priority == 42, str(cfg2.tasks[0].priority))

    # 损坏的配置文件不应导致崩溃（回退到 .bak 或空任务列表）
    path.write_text("{ 这不是 JSON", encoding="utf-8")
    cfg3 = config_mod.load_config(path)
    _check("损坏配置不崩溃且回退到备份", isinstance(cfg3.tasks, list), f"tasks={len(cfg3.tasks)}")


def test_answers_bank() -> None:
    """答案库：算术求值、按题检索、顺序无关、记住答案、未配置占位。"""
    from .answers import AnswerBank, AnswerRule, eval_arithmetic, normalize_expr
    from .questionnaire import Question, clean_label

    # 算术
    _check("算术 11-4", eval_arithmetic("11-4") == "7")
    _check("算术 5*8（乘号不被当必填星号）", eval_arithmetic("5*8") == "40")
    _check("算术 9/3", eval_arithmetic("9/3") == "3")
    _check("算术 6+7=?", eval_arithmetic("6+7=?") == "13")
    _check("算术 3加5等于多少", eval_arithmetic("3加5等于多少") == "8")
    _check("算术 12÷4（全角除号）", eval_arithmetic("12÷4") == "3")
    _check("判断题 1+1=2 不当作求值题", normalize_expr("1+1=2") is None)
    _check("非算术文本返回 None", normalize_expr("你喜欢什么颜色") is None)

    # 题号/星号清洗
    _check("清洗题号前缀", clean_label("1. 11-4") == "11-4")
    _check("清洗必填星号但保留乘号", clean_label("* 5*8") == "5*8")
    _check("清洗中文题号", clean_label("（2）9/3") == "9/3")

    # 答案库检索
    bank = AnswerBank(rules=[
        AnswerRule.from_dict({"id": "a", "match": "text", "pattern": "你的姓名", "answer": "张三"}),
        AnswerRule.from_dict({"id": "b", "match": "regex", "pattern": "性别", "answer": "男"}),
        AnswerRule.from_dict({"id": "c", "match": "expr", "pattern": "arithmetic", "answer": ""}),
        AnswerRule.from_dict({"id": "d", "match": "text", "pattern": "占位题",
                              "answer": "未配置：请补充"}),
    ])
    m = bank.resolve("1. 你的姓名是？", "q1")
    _check("文本规则命中（题目带题号也能匹配）", m.ok and m.answer == "张三", f"{m.answer}/{m.source}")
    m = bank.resolve("2. 你的性别", "q2")
    _check("正则规则命中", m.ok and m.answer == "男", f"{m.answer}/{m.source}")
    m = bank.resolve("25*4", "q3")
    _check("表达式规则算出答案", m.ok and m.answer == "100", f"{m.answer}/{m.source}")
    m = bank.resolve("未登记的题目", "q4")
    _check("未命中标记为 unknown", not m.ok and m.source == "unknown", m.source)
    m = bank.resolve("占位题", "q5")
    _check("未配置占位答案不参与提交", not m.ok and "未配置" in str(m.answer), str(m.answer))

    # 顺序变化：字段名错位也能按题目内容给出正确答案
    questions = [
        Question(label="5*8", name="q1", qtype="text"),
        Question(label="你的姓名是？", name="q2", qtype="text"),
        Question(label="11-4", name="q3", qtype="text"),
    ]
    matches = bank.resolve_many(questions)
    _check("顺序变化仍按题目内容作答",
           [m.answer for m in matches] == ["40", "张三", "7"],
           str([m.answer for m in matches]))

    # 任务覆盖优先
    m = bank.resolve("你的姓名是？", "q1", override="李四")
    _check("任务自定义答案覆盖答案库", m.answer == "李四" and m.source == "override", m.source)

    # 记住的答案
    bank.remember("这里是一道新题", "记住的答案", source="test")
    m = bank.resolve("这里是一道新题", "q9")
    _check("记住的答案可复用", m.ok and m.answer == "记住的答案" and m.source == "remembered",
           f"{m.answer}/{m.source}")

    # 新增规则：id 唯一 + 同题目重复新增时合并而不是插入重复项（GUI 曾因 id 冲突而"点了没反应"）
    gui_bank = AnswerBank()
    gui_bank.add_rule("你最喜欢的颜色", "蓝色")
    gui_bank.add_rule("你最喜欢的颜色", "红色")
    _check("同一题目重复新增会合并（不产生重复规则）",
           len(gui_bank.rules) == 1 and gui_bank.rules[0].answer == "红色",
           str([(r.id, r.answer) for r in gui_bank.rules]))
    ids = set()
    clash = False
    for p in ("你最喜欢的颜色", "你的年龄", "你的年龄", "your age", "你的年龄"):
        rule = gui_bank.add_rule(p, "x") if p not in ("你最喜欢的颜色",) else None
    for i in range(5):
        r = gui_bank.add_rule(f"题目{i}", f"答案{i}", match="text")
        if r.id in ids:
            clash = True
        ids.add(r.id)
    _check("新增规则的 id 不会重复", not clash, str(sorted(ids)))
    _check("中文题目也能生成可读 id",
           any("你" in r.id or "题" in r.id for r in gui_bank.rules),
           str([r.id for r in gui_bank.rules]))

    # 删除语义：pattern 重名时按 id 删除**只删一条**（历史 bug：会连带删除所有同名规则）
    bank_del = AnswerBank(rules=[
        AnswerRule.from_dict({"id": "a1", "match": "text", "pattern": "姓名", "answer": "甲"}),
        AnswerRule.from_dict({"id": "a2", "match": "text", "pattern": "姓名", "answer": "甲"}),
        AnswerRule.from_dict({"id": "a3", "match": "regex", "pattern": "姓名", "answer": "甲"}),
        AnswerRule.from_dict({"id": "b1", "match": "text", "pattern": "学号", "answer": "1"}),
    ])
    bank_del.remove_rule("a1")
    _check("按 id 删除只删一条（重名 pattern 不被连带删除）",
           [r.id for r in bank_del.rules] == ["a2", "a3", "b1"],
           str([r.id for r in bank_del.rules]))
    removed = bank_del.remove_rules(["a2", "a3#2", "不存在"])
    _check("批量按 id 删除返回实际删除条数", removed == 2 and len(bank_del.rules) == 1,
           f"removed={removed}")

    dup_bank = AnswerBank(rules=[
        AnswerRule.from_dict({"id": f"d{i}", "match": "text", "pattern": "班级", "answer": "2606"})
        for i in range(5)
    ])
    _check("识别重复规则组", len(dup_bank.duplicate_groups()) == 1)
    n = dup_bank.deduplicate()
    _check("一键去重保留一条", n == 4 and len(dup_bank.rules) == 1, f"removed={n}")

    # 题库读写往返
    import shutil
    from pathlib import Path

    from .paths import RUN_DIR

    tmp = Path(RUN_DIR) / "selftest-bank"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        target = tmp / "bank.json"
        bank.path = target
        bank.save()
        reloaded = AnswerBank.load(target)
        _check("答案库读写往返一致",
               len(reloaded.rules) == len(bank.rules)
               and reloaded.resolve("你的姓名是？", "q1").answer == "张三",
               f"rules={len(reloaded.rules)} remembered={len(reloaded.remembered)}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
def test_questionnaire_parse() -> None:
    """真实问卷星 DOM（本项目实测结构）的题目提取。"""
    from .questionnaire import parse_questions_html

    html = """
    <fieldset class='fieldset' id='fieldset1'>
      <div class='field ui-field-contain' topic='1' id='div1' req='1' data-role='fieldcontain' type='1'>
        <div class='field-label'><span class='req'>*</span>
          <div class='topicnumber'>1.</div><div class='topichtml'>11-4</div></div>
        <div class='ui-input-text'><input type='text' id='q1' name='q1'/></div>
        <div class='errorMessage'></div>
      </div>
      <div class='field ui-field-contain' topic='2' id='div2' req='1' data-role='fieldcontain' type='3'>
        <div class='field-label'><span class='req'>*</span>
          <div class='topicnumber'>2.</div><div class='topichtml'>你更倾向哪一种方案？</div></div>
        <div class='ui-radio'><input type='radio' name='q2' value='1'/><label>方案一</label>
          <input type='radio' name='q2' value='2'/><label>方案二</label></div>
      </div>
    </fieldset>
    """
    qs = parse_questions_html(html)
    _check("识别题目数量", len(qs) == 2, f"got {len(qs)}")
    if len(qs) == 2:
        _check("填空题题干与字段", qs[0].label == "11-4" and qs[0].name == "q1"
               and qs[0].qtype == "text" and qs[0].required, str(qs[0].to_dict()))
        _check("单选题题干与选项", qs[1].qtype == "radio" and len(qs[1].options) == 2
               and qs[1].label.startswith("你更倾向"), str(qs[1].to_dict()))


def test_page_state() -> None:
    """问卷星的"定时开放/已结束"提示页识别（真实页面实测文案）。"""
    from .questionnaire import detect_page_state, extract_open_time, parse_questions_html

    not_open_html = """<html><head><title>测试2</title></head><body>
      <div class="wrap"><h2>提示</h2>
      <p>很抱歉，此问卷将于2099-01-01 09:30（北京时间）开放，请到时再进入此页面进行填写！</p>
      </div></body></html>"""
    _check("识别尚未开放", detect_page_state(not_open_html) == "not_open")
    _check("提取开放时间（绝对时间，返回 YYYY-MM-DD HH:MM）",
           extract_open_time(not_open_html) == "2099-01-01 09:30",
           extract_open_time(not_open_html))
    _check("提示页解析不出题目", len(parse_questions_html(not_open_html)) == 0)

    closed_html = "<html><body><p>此问卷已结束，感谢您的参与</p></body></html>"
    _check("识别已结束", detect_page_state(closed_html) == "closed")

    paused_html = "<html><body><p>提示信息</p><p>很抱歉，此问卷(389236309)已暂停，不能填写！</p></body></html>"
    _check("识别已暂停/不能填写", detect_page_state(paused_html) == "closed")

    ok_html = "<html><body><p>程序测试 1. 11-4</p></body></html>"
    _check("正常页面不误判为未开放/已结束", detect_page_state(ok_html) == "ok")
    _check("无正文时返回 unknown", detect_page_state("<html><body></body></html>") == "unknown")


def test_open_time_derivation() -> None:
    """从"尚未开放"提示页推导开始时间（绝对时间与倒计时两种形态）。"""
    import datetime as _dt

    from .questionnaire import extract_open_datetime, extract_open_time

    now = _dt.datetime(2026, 10, 16, 16, 49, 0)

    absolute = ("<html><body><p>很抱歉，此问卷将于2026-10-16 16:55（北京时间）开放，"
                "请到时再进入此页面进行填写！</p></body></html>")
    when, secs = extract_open_datetime(absolute, now=now)
    _check("绝对时间推导开放时间", when == "2026-10-16 16:55:00", when)
    _check("绝对时间算出剩余秒数", secs == 6 * 60, str(secs))

    countdown = "<html><body><p>距离开始还有0天0时9分59秒</p></body></html>"
    when, secs = extract_open_datetime(countdown, now=now)
    _check("倒计时推导开放时间（9分59秒 → 16:58:59）", when == "2026-10-16 16:58:59", when)
    _check("倒计时剩余秒数正确", secs == 9 * 60 + 59, str(secs))

    hours = "<html><body><p>距离开始还有1天2时3分4秒</p></body></html>"
    _, secs = extract_open_datetime(hours, now=now)
    _check("带天/小时的倒计时", secs == 86400 + 2 * 3600 + 3 * 60 + 4, str(secs))

    _check("没有时间信息时返回空", extract_open_datetime("<p>随便什么</p>", now=now) == ("", 0))
    _check("extract_open_time 兼容旧接口",
           extract_open_time(absolute).startswith("2026-10-16 16:55"))


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
        sched = Scheduler(cfg, config_path=False)
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
        s2 = Scheduler(cfg2, config_path=False)
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
        s3 = Scheduler(cfg3, config_path=False)
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
        s4 = Scheduler(cfg4, config_path=False)
        s4._tick()
        time.sleep(1.2)
        s4._tick()
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
        s5 = Scheduler(cfg5, config_path=False)
        s5._tick()
        time.sleep(0.5)
        _check("超出窗口的过期任务不执行", old.attempts == 0, f"attempts={old.attempts}")
        # 答案库路径：任务不给任何默认内容，全靠答案库按题目内容检索
        from .answers import AnswerBank, AnswerRule

        cfg_bank = AppConfig()
        cfg_bank.settings.backend = "mock"
        cfg_bank.settings.mock_base_url = base
        t_bank = Task(name="答案库卷", url=f"{base}/vm/A.aspx", answers={})
        cfg_bank.tasks = [t_bank]
        bank = AnswerBank(rules=[
            AnswerRule.from_dict({"id": "b1", "match": "regex", "pattern": "你更倾向哪一种方案",
                                  "answer": "方案二"}),
            AnswerRule.from_dict({"id": "b2", "match": "text", "pattern": "请填写你的默认备注",
                                  "answer": "答案库自动填写的备注"}),
            AnswerRule.from_dict({"id": "b3", "match": "expr", "pattern": "arithmetic", "answer": ""}),
        ])
        sched_bank = Scheduler(cfg_bank, config_path=False)
        sched_bank._bank = bank
        sched_bank._start_task(t_bank, "自测-答案库")
        _join(sched_bank, t_bank)
        _check("仅靠答案库即可提交成功（无任务默认内容）",
               t_bank.status == TaskStatus.SUCCESS.value,
               t_bank.last_run.summary() if t_bank.last_run else "")

        # 答案库 + 顺序变化：把题目文案对调，仍应按题目内容给出答案
        from .questionnaire import Question

        qs = [Question(label="5*8", name="q1", qtype="text"),
              Question(label="你更倾向哪一种方案？", name="q2", qtype="radio",
                       options=[{"value": "1", "label": "方案一"},
                                {"value": "2", "label": "方案二"}])]
        ms = bank.resolve_many(qs)
        _check("字段名与题目顺序错位时仍答对",
               [m.answer for m in ms] == ["40", "方案二"], str([m.answer for m in ms]))

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
    test_config_io()
    test_answers_bank()
    test_questionnaire_parse()
    test_page_state()
    test_open_time_derivation()
    test_backend_and_scheduler()
    failed = [r for r in _results if r[0] == FAIL]
    print("-" * 70)
    print(f"共 {len(_results)} 项，通过 {len(_results) - len(failed)} 项，失败 {len(failed)} 项")
    for status, name, detail in failed:
        print(f"  {status}: {name} {detail}")
    return 0 if not failed else 1
