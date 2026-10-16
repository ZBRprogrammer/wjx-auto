"""wjx-auto 命令行入口。

用法示例
--------
python main.py gui                          # 打开图形界面（默认）
python main.py mock --full-rate 0.4         # 启动本地模拟问卷服务
python main.py add --name 问卷C --url https://v.wjx.cn/vm/xxx.aspx --at "2026-01-01 12:00:00"
python main.py list                         # 列出任务与状态
python main.py run --task 问卷C             # 用当前后端立即跑一次并打印判定结果
python main.py daemon --scale 600           # 后台静默运行（scale>1 仅用于加速自测）
python main.py demo                         # 一键演示 A/B 条件与优先级场景
python main.py selftest                     # 规则引擎/驱动冒烟自测
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

if sys.stdout is not None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from wjx import config as config_mod
from wjx import logging_setup, manualtest, notify, paths
from wjx import rules
from wjx.models import AppConfig, RunRecord, STATUS_TEXT, Task, TaskStatus
from wjx.scheduler import Scheduler


# --------------------------------------------------------------------------- #
def _load(args) -> AppConfig:
    cfg = config_mod.load_config(getattr(args, "config", None))
    paths.ensure_dirs()
    return cfg


def _find_or_die(cfg: AppConfig, key: str) -> Task:
    task = cfg.find(key)
    if task is None:
        print(f"未找到任务: {key}", file=sys.stderr)
        sys.exit(2)
    return task


# --------------------------------------------------------------------------- #
def cmd_gui(args) -> int:
    cfg = _load(args)
    _ensure_mock_server(cfg)
    from wjx.gui import run_gui

    return run_gui(cfg, getattr(args, "config", None))


def cmd_mock(args) -> int:
    from wjx import mockserver

    argv = ["--port", str(args.port), "--host", args.host]
    if args.full_rate is not None:
        argv += ["--full-rate", str(args.full_rate)]
    return mockserver.main(argv)


def cmd_list(args) -> int:
    cfg = _load(args)
    if not cfg.tasks:
        print("（没有任务）")
        return 0
    print(f"{'状态':<12} {'名称':<14} {'计划时间':<20} {'分组':<10} {'优先级':<6} 条件")
    for t in cfg.tasks:
        print(f"{STATUS_TEXT.get(t.status_enum, t.status):<12} {t.name:<14} "
              f"{t.start_at or '(仅手动)':<20} {t.group or '-':<10} {t.priority:<6} "
              f"{t.start_rule or '-'}")
    return 0


def cmd_add(args) -> int:
    cfg = _load(args)
    if not args.at:
        args.at = (datetime.now() + timedelta(minutes=10)).replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    task = Task(
        name=args.name,
        url=args.url,
        start_at=args.at,
        group=args.group or "",
        priority=args.priority,
        start_rule=args.rule or "",
        reminder_minutes=args.remind,
        prepare_minutes=args.prepare,
        answers=config_mod.parse_answer_text(args.answers or ""),
        answer_text=args.answers or "",
    )
    cfg.tasks.append(task)
    config_mod.save_config(cfg, getattr(args, "config", None))
    print(f"已添加任务 {task.name}（id={task.id}），计划 {task.start_at}")
    return 0


def cmd_remove(args) -> int:
    cfg = _load(args)
    task = _find_or_die(cfg, args.task)
    cfg.tasks = [t for t in cfg.tasks if t.id != task.id]
    config_mod.save_config(cfg, getattr(args, "config", None))
    print(f"已删除任务 {task.name}")
    return 0


def cmd_run(args) -> int:
    cfg = _load(args)
    task = _find_or_die(cfg, args.task)
    rec = manualtest.fill_once(cfg, task)
    config_mod.save_config(cfg, getattr(args, "config", None))
    print(json.dumps(rec.to_dict(), ensure_ascii=False, indent=2))
    return 0 if rec.result == "success" else 1


def cmd_check_rule(args) -> int:
    cfg = _load(args)
    err = rules.validate_rule(args.expr, cfg.tasks)
    if err:
        print(f"语法错误: {err}")
        return 1
    ok, warn = rules.safe_evaluate_rule(args.expr, cfg.tasks)
    print(f"语法正确；当前评估 = {'真' if ok else '假'}" + (f"（{warn}）" if warn else ""))
    print()
    print(rules.describe_functions())
    return 0


def cmd_daemon(args) -> int:
    cfg = _load(args)
    logger = logging_setup.setup()
    _ensure_mock_server(cfg)

    def on_event(ev) -> None:
        kind = ev.get("kind")
        if kind == "task_done":
            print(f"[{ev['time']}] {'✔' if ev.get('ok') else '✘'} {ev.get('task_name')}：{ev.get('message')}")
        elif kind == "reminder":
            print(f"[{ev['time']}] ⏰ 提醒 {ev.get('task_name')}：{ev.get('text')}")
        elif kind == "task_skipped":
            print(f"[{ev['time']}] — 跳过 {ev.get('task_name')}：{ev.get('reason')}")

    sched = Scheduler(cfg, on_event=on_event, config_path=getattr(args, "config", None))
    if args.scale and args.scale > 1:
        sched.clock_scale = float(args.scale)
        logger.warning("已启用加速时钟 scale=%s（仅用于自测，正式使用请勿开启）", args.scale)
    sched.start()
    print("已进入后台静默运行（Ctrl+C 退出）。" + ("加速模式 scale=%s" % args.scale if args.scale and args.scale > 1 else ""))
    try:
        deadline = time.time() + args.seconds if args.seconds else None
        while True:
            time.sleep(1)
            if deadline and time.time() > deadline:
                break
    except KeyboardInterrupt:
        print("\n正在退出…")
    finally:
        sched.stop()
        for t in cfg.tasks:
            print(f"  {t.name}: {STATUS_TEXT.get(t.status_enum, t.status)}"
                  + (f" | {t.last_run.summary()}" if t.last_run else ""))
    return 0


def cmd_demo(args) -> int:
    """一键演示：A 先执行成功后 B 自动跳过；A 失败则 B 继续。"""
    from wjx import mockserver

    cfg = AppConfig()
    cfg.settings.backend = "mock"
    base = cfg.settings.mock_base_url.rstrip("/")

    if not manualtest.check_mock_server(cfg.settings):
        print("未检测到模拟问卷服务，正在自动启动 …")
        if not _ensure_mock_server(cfg):
            print("模拟问卷服务启动失败，请手动运行: python main.py mock")
            return 2

    a = Task(name="问卷A", url=f"{base}/vm/A.aspx", start_at=_past(2),
             answers={"q1": "1", "q2": "A的默认填写内容"}, group="今日问卷", priority=10,
             retry_on_full=0)
    b = Task(name="问卷B", url=f"{base}/vm/B.aspx", start_at=_past(1),
             answers={"q1": "2", "q2": "B的默认填写内容"}, group="今日问卷", priority=5,
             retry_on_full=0, start_rule="not submitted('问卷A')")
    cfg.tasks = [a, b]

    print("=" * 74)
    print("场景一：A 成功 → B 应被跳过（互斥 + 条件）")
    print("=" * 74)
    sched = Scheduler(cfg)
    _pump_until_idle(sched, [a, b], budget=6)
    print(f"  A 结果: {a.status} {a.last_run.summary() if a.last_run else ''}")
    print(f"  B 结果: {b.status} {b.last_run.summary() if b.last_run else ''}")
    ok1 = a.status == "success" and b.status == "skipped"

    print()
    print("=" * 74)
    print("场景二：A 提交失败（人数已满）→ B 条件成立，应继续填写 B")
    print("=" * 74)
    cfg2 = AppConfig()
    cfg2.settings.backend = "mock"
    a2 = Task(name="问卷A", url=f"{base}/vm/A.aspx?force=full", start_at=_past(2),
              answers={"q1": "1", "q2": "A的默认填写内容"}, group="今日问卷", priority=10,
              retry_on_full=0)
    b2 = Task(name="问卷B", url=f"{base}/vm/B.aspx?force=ok", start_at=_past(1),
              answers={"q1": "2", "q2": "B的默认填写内容"}, group="今日问卷", priority=5,
              retry_on_full=0, start_rule="not submitted('问卷A')")
    cfg2.tasks = [a2, b2]
    sched2 = Scheduler(cfg2)
    _pump_until_idle(sched2, [a2, b2], budget=6)
    print(f"  A 结果: {a2.status} {a2.last_run.summary() if a2.last_run else ''}")
    print(f"  B 结果: {b2.status} {b2.last_run.summary() if b2.last_run else ''}")
    ok2 = (a2.last_run is not None and a2.last_run.result == "full"
           and b2.status == "success")

    print()
    print("=" * 74)
    print(f"结论：场景一 {'通过 ✔' if ok1 else '未通过 ✘'}；场景二 {'通过 ✔' if ok2 else '未通过 ✘'}")
    print("=" * 74)
    return 0 if (ok1 and ok2) else 1


def _pump_until_idle(sched: Scheduler, tasks: List[Task], budget: float = 8.0) -> None:
    """在测试中手动推进调度器（不启动线程），直到所有任务有了终态或超时。"""
    sched.clock_scale = 1.0
    sched.cfg.settings.respect_min_interval = 0
    deadline = time.time() + budget
    settled = 0
    while time.time() < deadline:
        sched._tick()
        if all(t.status_enum in (TaskStatus.SUCCESS, TaskStatus.SKIPPED, TaskStatus.FAILED,
                                 TaskStatus.ERROR) for t in tasks):
            settled += 1
            if settled >= 3:   # 连续几轮无新动作，确认稳定
                break
        else:
            settled = 0
        time.sleep(0.3)
    for t in tasks:
        _join(sched, t)


def _join(sched: Scheduler, task: Task, timeout: float = 15) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        th = sched._running.get(task.id)
        if th is None and task.status_enum != TaskStatus.RUNNING:
            return
        if th is not None:
            th.join(timeout=0.5)
            if not th.is_alive():
                return
        time.sleep(0.1)


def _past(minutes: int) -> str:
    return (datetime.now() - timedelta(minutes=minutes)).replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def cmd_selftest(args) -> int:
    from wjx import selftest

    return selftest.run()


# --------------------------------------------------------------------------- #
def _soon(minutes: int) -> str:
    return (datetime.now() + timedelta(minutes=minutes)).replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _ensure_mock_server(cfg: AppConfig) -> bool:
    """GUI/daemon 启动时若模拟服务未运行则自动拉起（仅当配置指向本地 mock 地址）。"""
    base = cfg.settings.mock_base_url or ""
    if "127.0.0.1" not in base and "localhost" not in base:
        return False
    if manualtest.check_mock_server(cfg.settings):
        return True
    try:
        port = 8765
        if ":" in base.rsplit("/", 1)[-1]:
            try:
                port = int(base.rsplit(":", 1)[-1].strip("/"))
            except ValueError:
                pass
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(
            [sys.executable, str(paths.ROOT / "main.py"), "mock", "--port", str(port)],
            cwd=str(paths.ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        for _ in range(20):
            time.sleep(0.25)
            if manualtest.check_mock_server(cfg.settings):
                return True
    except Exception as exc:
        print(f"启动模拟问卷服务失败: {exc}", file=sys.stderr)
    return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wjx-auto", description="问卷星定时自动填写助手",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__,
    )
    parser.add_argument("--config", type=Path, default=None, help="配置文件路径（默认 data/tasks.json）")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("gui", help="打开图形界面")
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("mock", help="启动本地模拟问卷服务")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--full-rate", type=float, default=None, help="问卷B 出现'人数已满'的概率（0~1）")
    p.set_defaults(func=cmd_mock)

    p = sub.add_parser("list", help="列出任务")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("add", help="添加任务")
    p.add_argument("--name", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--at", default=None, help="YYYY-MM-DD HH:MM:SS")
    p.add_argument("--group", default="")
    p.add_argument("--priority", type=int, default=0)
    p.add_argument("--rule", default="", help="启动条件表达式")
    p.add_argument("--answers", default="", help="默认填写内容，如 'q1 = 1\\nq2 = 文本'")
    p.add_argument("--remind", type=int, default=5)
    p.add_argument("--prepare", type=int, default=5)
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("remove", help="删除任务")
    p.add_argument("--task", required=True, help="任务名称或 id")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("run", help="立即执行一次并输出判定结果")
    p.add_argument("--task", required=True)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("check-rule", help="校验条件表达式")
    p.add_argument("expr")
    p.set_defaults(func=cmd_check_rule)

    p = sub.add_parser("daemon", help="后台静默运行调度器")
    p.add_argument("--seconds", type=int, default=0, help="运行多少秒后退出（0=一直运行）")
    p.add_argument("--scale", type=float, default=1.0, help="加速时钟（仅用于自测）")
    p.set_defaults(func=cmd_daemon)

    p = sub.add_parser("demo", help="一键演示 A/B 条件与优先级场景")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("selftest", help="运行内置自测")
    p.set_defaults(func=cmd_selftest)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = ["gui"]
    args = parser.parse_args(argv)
    if not getattr(args, "command", None) or not hasattr(args, "func"):
        parser.print_help()
        return 0
    paths.ensure_dirs()
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
