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
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:  # pragma: no cover
    from wjx.answers import AnswerBank

if sys.stdout is not None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from wjx import config as config_mod
from wjx import logging_setup, manualtest, notify, paths
from wjx import rules
from wjx.models import AppConfig, RESULT_TEXT, RunRecord, STATUS_TEXT, Task, TaskStatus
from wjx.scheduler import Scheduler


# --------------------------------------------------------------------------- #
def _load(args) -> AppConfig:
    paths.setup_environment()          # vendor/ 依赖、selenium 缓存目录
    cfg = config_mod.load_config(getattr(args, "config", None))
    return cfg


def _find_or_die(cfg: AppConfig, key: str) -> Task:
    task = cfg.find(key)
    if task is None:
        print(f"未找到任务: {key}", file=sys.stderr)
        sys.exit(2)
    return task


def _bank(args) -> "AnswerBank":
    from wjx.answers import AnswerBank

    return AnswerBank.load()


# --------------------------------------------------------------------------- #
# 题目 / 答案库相关命令
# --------------------------------------------------------------------------- #
def cmd_probe(args) -> int:
    """读取问卷题目并在答案库中检索答案（不提交）。"""
    cfg = _load(args)
    _ensure_mock_server(cfg)
    from wjx import autorun

    bank = _bank(args)
    res = autorun.probe_questions(cfg, args.url, prefer_browser=not args.static, bank=bank)
    print(res.report(show_options=not args.brief))
    if args.json:
        print()
        print(json.dumps({
            "url": res.url, "source": res.source, "error": res.error,
            "questions": [dict(q.to_dict(), answer=m.answer, match=m.describe(),
                               note=m.note)
                          for q, m in zip(res.questions, res.matches)],
        }, ensure_ascii=False, indent=2))
    return 0 if res.ok and not res.unresolved else (1 if res.error else 0)


def cmd_resolve(args) -> int:
    """预演：显示将会填写的内容（不提交），等价于 GUI 的『预演』。"""
    cfg = _load(args)
    _ensure_mock_server(cfg)
    from wjx import autorun

    task = cfg.find(args.task) if args.task else None
    url = args.url or (task.url if task else "")
    if not url:
        print("请通过 --url 或 --task 指定问卷", file=sys.stderr)
        return 2
    res = autorun.probe_questions(cfg, url, prefer_browser=not args.static, bank=_bank(args))
    print(res.report(show_options=not args.brief))
    print()
    print("将提交的内容：")
    for k, v in res.answers.items():
        print(f"  {k} = {v!r}")
    if res.unresolved:
        print(f"  （{len(res.unresolved)} 道题未命中答案库："
              + "; ".join(q.label[:20] for q in res.unresolved) + "）")
    return 0 if res.ok else 1


def cmd_fill(args) -> int:
    """按题目内容检索答案库并真实填写提交。"""
    cfg = _apply_cli_overrides(args, _load(args))
    _ensure_mock_server(cfg)
    from wjx import autorun

    task = cfg.find(args.task) if args.task else None
    url = args.url or (task.url if task else "")
    if not url:
        print("请通过 --url 或 --task 指定问卷", file=sys.stderr)
        return 2
    task = task or Task(name=args.task or "(临时任务)", url=url)
    rec = autorun.run_task(cfg, task, bank=_bank(args), dry_run=args.dry_run)
    if not args.url and args.task:
        config_mod.save_config(cfg, getattr(args, "config", None))
    print(json.dumps(rec.to_dict(), ensure_ascii=False, indent=2))
    print(f"\n判定: {RESULT_TEXT.get(rec.result_enum, rec.result)}")
    return 0 if rec.result == "success" else 1


def cmd_bank(args) -> int:
    """查看/维护答案库。"""
    from wjx.answers import AnswerBank, CONFIDENCE_TEXT, load_pending

    path = Path(args.bank_file) if getattr(args, "bank_file", None) else None
    bank = AnswerBank.load(path) if path else _bank(args)
    action = args.action

    if action == "show":
        data = bank.to_dict()
        print(f"答案库: {bank.path}")
        print(f"规则 {len(bank.rules)} 条，记住的答案 {len(bank.remembered)} 条\n")
        for r in bank.rules:
            flag = "" if r.enabled else "（已停用）"
            print(f"  [{r.id}] {r.match:6} {r.pattern!r:34} -> {r.answer!r} {flag}")
            if r.note:
                print(f"         备注: {r.note}")
        if bank.remembered:
            print("\n记住的答案（按题目原文精确命中）：")
            for q, e in list(bank.remembered.items())[:30]:
                print(f"  {q[:44]!r:46} -> {e.answer!r}  (命中 {e.hits} 次)")
        pending = load_pending()
        if pending:
            print(f"\n待补充答案的题目 {len(pending)} 道（data/pending_questions.json）：")
            for it in pending[:20]:
                print(f"  [{it.get('qtype','')}] {it.get('question','')}")
                if it.get("options"):
                    print("      选项: " + " / ".join(
                        str(o.get("label")) for o in it["options"][:8]))
        return 0

    if action == "add":
        rule = bank.add_rule(args.pattern, args.answer, match=args.match, note=args.note or "")
        bank.save()
        print(f"已添加规则 [{rule.id}] {rule.match} {rule.pattern!r} -> {rule.answer!r}")
        return 0

    if action == "remove":
        ok = bank.remove_rule(args.pattern)
        if ok:
            bank.save()
            print(f"已删除规则: {args.pattern}")
            return 0
        print(f"未找到规则: {args.pattern}", file=sys.stderr)
        return 1

    if action == "remember":
        bank.remember(args.pattern, args.answer, source="cli")
        bank.save()
        print(f"已记住: {args.pattern[:40]!r} -> {args.answer!r}")
        return 0

    if action == "clear-pending":
        from wjx.paths import DATA_DIR

        p = DATA_DIR / "pending_questions.json"
        if p.exists():
            p.unlink()
        print("已清空待补充题目列表")
        return 0

    print(f"未知操作: {action}", file=sys.stderr)
    return 2


def cmd_autostart(args) -> int:
    """为"没有填写开始时间"的任务推导开始时间并在到点后执行。

    流程：读题页 → 从"将于 X 开放 / 距离开始还有…"推导开放时间
          → 写回任务的开始时间 → 等到那个时刻 → 自动填写提交。

    这样即使问卷星没告诉你开放时间，也能自动拿到并准时提交。
    """
    cfg = _apply_cli_overrides(args, _load(args))
    _ensure_mock_server(cfg)
    from wjx import autorun
    from wjx.questionnaire import extract_open_datetime, fetch_html

    task = cfg.find(args.task) if args.task else None
    url = args.url or (task.url if task else "")
    if not url:
        print("请通过 --url 或 --task 指定问卷", file=sys.stderr)
        return 2
    task = task or Task(name=args.name or "自动定时任务", url=url)
    bank = _bank(args)

    # 手动指定开始时间时跳过推导
    if args.at:
        task.start_at = args.at
        if not cfg.find(task.id):
            cfg.tasks.append(task)
        config_mod.save_config(cfg, getattr(args, "config", None))
        print(f"已按手动指定的开始时间写入任务：{task.name} @ {task.start_at}")
        if args.no_run:
            return 0
        return _execute_task(cfg, task, bank, args)

    # 1) 先看问卷当前是什么状态
    res = autorun.probe_questions(cfg, url, prefer_browser=not args.static, bank=bank)
    print(res.report())

    if res.state == "ok":
        print("问卷当前已开放，无需推导开始时间。")
        if not args.no_run:
            return _execute_task(cfg, task, bank, args)
        return 0
    if res.state != "not_open":
        print(f"无法推导开始时间（页面状态 {res.state}）", file=sys.stderr)
        return 1

    when, seconds = res.open_time, res.open_seconds
    if not when:
        page = ""
        try:
            page = fetch_html(url)
        except Exception as exc:
            print(f"读取开放时间失败: {exc}", file=sys.stderr)
        when, seconds = extract_open_datetime(page)
    if not when:
        print("未能从页面推导出开放时间；请手动指定 --at", file=sys.stderr)
        return 1

    # 2) 写回任务的开始时间
    task.start_at = when
    if not cfg.find(task.id):
        cfg.tasks.append(task)
    config_mod.save_config(cfg, getattr(args, "config", None))
    print(f"\n✔ 已推导开始时间：{when}（距今约 {seconds // 60} 分 {seconds % 60} 秒）")
    print(f"  任务「{task.name}」已写入配置，到点后会自动读题→检索答案→填写提交。")

    if args.no_run:
        return 0

    # 3) 等到点再执行（最多再等 --max-wait 秒）
    max_wait = int(args.max_wait or (seconds + 120))
    print(f"  开始等待（最长 {max_wait // 60} 分钟）… 到时自动执行；Ctrl+C 可中止等待。")
    res2 = autorun.probe_until_open(cfg, url, bank=bank, max_wait=seconds + 60,
                                    on_wait=lambda r, remain, n: print(
                                        f"  … 尚未开放，剩余约 {int(remain)} 秒，{n} 次重试后继续",
                                        flush=True))
    if res2.state != "ok":
        print(f"等待结束但问卷仍未开放：{res2.report()}", file=sys.stderr)
        return 1
    print("  问卷已开放，开始填写提交 …")
    return _execute_task(cfg, task, bank, args)


def _execute_task(cfg: AppConfig, task: Task, bank, args) -> int:
    from wjx import autorun

    rec = autorun.run_task(cfg, task, bank=bank, dry_run=args.dry_run)
    config_mod.save_config(cfg, getattr(args, "config", None))
    print(json.dumps(rec.to_dict(), ensure_ascii=False, indent=2))
    print(f"\n判定: {RESULT_TEXT.get(rec.result_enum, rec.result)}")
    return 0 if rec.result == "success" else 1


def cmd_check_env(args) -> int:
    """环境自检：打包/换机器后先跑这个，确认缺什么。"""
    import platform

    from wjx import paths

    paths.setup_environment()
    ok = True
    print("=" * 66)
    print("wjx-auto 环境自检")
    print("=" * 66)
    print(f"Python      : {platform.python_version()} ({platform.machine()})")
    print(f"运行方式    : {'打包 exe' if paths.is_frozen() else '源码'}")
    print(f"数据目录    : {paths.ROOT}")
    print(f"资源目录    : {paths.BUNDLE_DIR}")

    # 可写性
    for name in ("data", "logs", "run"):
        d = paths.ROOT / name
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".write_test"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
            print(f"  {name:6} 可写 ✔  {d}")
        except Exception as exc:
            ok = False
            print(f"  {name:6} 不可写 ✘  {d} ({exc})")

    # tkinter
    try:
        import tkinter

        print(f"tkinter     : 可用 ✔ (Tk {tkinter.TkVersion})")
    except Exception as exc:
        ok = False
        print(f"tkinter     : 不可用 ✘ {exc}")

    # 内置解析器
    try:
        from wjx.htmlparse import Soup

        Soup("<div class='field'><input name='q1'/></div>").select("div.field")
        print("HTML 解析   : 内置解析器可用 ✔")
    except Exception as exc:
        ok = False
        print(f"HTML 解析   : 失败 ✘ {exc}")

    # selenium
    try:
        import selenium

        print(f"selenium    : 可用 ✔ {selenium.__version__}")
    except Exception as exc:
        print(f"selenium    : 不可用 ✘ {exc}（只能使用 mock 后端）")

    # 浏览器与驱动
    cfg = config_mod.load_config(getattr(args, "config", None))
    for browser in ("edge", "chrome"):
        exe = _browser_path(browser)
        driver = paths.find_bundled_driver(browser, strict=True)
        status = [f"浏览器 {'✔' if exe else '✘'}", f"驱动 {'✔' if driver else '✘'}"]
        print(f"{browser:11} : {'  '.join(status)}"
              + (f"  {driver}" if driver else "  （未找到驱动，可放到 drivers/ 目录）"))
    print(f"默认后端    : {cfg.settings.backend} / 浏览器 {cfg.settings.browser}"
          f" / 无头 {cfg.settings.headless}")

    # 网络（能否访问问卷星）
    try:
        from wjx.questionnaire import fetch_html

        fetch_html("https://v.wjx.cn/vm/mYv3i3D.aspx", timeout=12)
        print("网络访问    : 可访问问卷星 ✔")
    except Exception as exc:
        print(f"网络访问    : 失败 ✘ {type(exc).__name__}: {exc}")

    # 答案库 / 任务
    from wjx.answers import AnswerBank

    bank = AnswerBank.load()
    print(f"答案库      : {len(bank.rules)} 条规则  {bank.path}")
    print(f"任务        : {len(cfg.tasks)} 个")
    print("=" * 66)
    print("结论：" + ("环境就绪 ✔" if ok else "存在问题，请按上面提示处理 ✘"))
    return 0 if ok else 1


def _browser_path(browser: str) -> str:
    import os as _os

    from wjx.drivers.selenium_driver import BROWSER_PATHS

    for path in BROWSER_PATHS.get(browser, ()):
        if _os.path.exists(path):
            return path
    return ""


def cmd_learn(args) -> int:
    """交互式补答案：读取问卷题目，对未命中的题目逐个询问并写入答案库。"""
    cfg = _load(args)
    _ensure_mock_server(cfg)
    from wjx import autorun
    from wjx.answers import load_pending

    bank = _bank(args)
    questions: List[Any] = []
    if args.url:
        res = autorun.probe_questions(cfg, args.url, prefer_browser=not args.static, bank=bank)
        if not res.ok:
            print(res.report(), file=sys.stderr)
            return 1
        questions = res.questions
    else:
        pending = load_pending()
        if not pending:
            print("没有待补充的题目（可先用 --url 指定问卷）")
            return 0
        from wjx.questionnaire import Question

        questions = [Question.from_dict({
            "label": it.get("question", ""), "name": it.get("field", ""),
            "qtype": it.get("qtype", ""), "options": it.get("options", []),
        }) for it in pending]

    added = 0
    for q in questions:
        m = bank.resolve(q.label, q.name, q.options, q.qtype)
        if m.ok:
            print(f"✔ 已有答案: {q.label[:40]} -> {m.answer!r}（{m.describe()}）")
            continue
        print(f"\n✘ 未命中: [{q.qtype}] {q.label}")
        if q.options:
            print("   选项: " + " / ".join(f"{o.get('label')}" for o in q.options[:10]))
        try:
            answer = input("   请输入答案（回车跳过，'expr' 表示按算式自动计算）: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已中断")
            break
        if not answer:
            continue
        if answer.lower() == "expr":
            bank.add_rule(q.label, "", match="expr", pattern="arithmetic", note="用户标记为算术题")
        else:
            bank.add_rule(q.label, answer, match="text", note="learn 命令补充")
        bank.remember(q.label, answer, source="learn")
        added += 1
    if added:
        bank.save()
        print(f"\n已写入 {added} 条答案到 {bank.path}")
    return 0


# --------------------------------------------------------------------------- #
def cmd_gui(args) -> int:
    cfg = _load(args)
    _ensure_mock_server(cfg)
    if getattr(args, "minimized", False):
        # 开机自启：启动后直接最小化到托盘，不弹窗打扰
        cfg.settings.start_minimized = True
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


def _apply_cli_overrides(args, cfg: AppConfig) -> AppConfig:
    """把命令行开关映射到本次运行的设置（不写回配置文件）。"""
    if getattr(args, "headed", False):
        cfg.settings.headless = False
    if getattr(args, "backend", None):
        cfg.settings.backend = args.backend
    if getattr(args, "browser", None):
        cfg.settings.browser = args.browser
    return cfg


def cmd_run(args) -> int:
    cfg = _load(args)
    _ensure_mock_server(cfg)      # mock 后端：服务没起就自动拉起
    task = _find_or_die(cfg, args.task)
    if args.manual:
        rec = manualtest.fill_once(cfg, task)
    else:
        from wjx import autorun

        rec = autorun.run_task(cfg, task, bank=_bank(args), dry_run=args.dry_run)
    config_mod.save_config(cfg, getattr(args, "config", None))
    print(json.dumps(rec.to_dict(), ensure_ascii=False, indent=2))
    print(f"\n判定: {RESULT_TEXT.get(rec.result_enum, rec.result)}", file=sys.stderr)
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

    # 单实例互斥：避免 GUI 与 daemon 同时跑调度器造成重复提交
    lock = paths.AppLock()
    if not lock.acquire():
        print(f"已有调度器在运行（{lock.describe_holder()}），本次不再启动。\n"
              "如需接管，请先退出那个实例，或删除 run/app.lock 后重试。", file=sys.stderr)
        return 3

    def on_event(ev) -> None:
        kind = ev.get("kind")
        if kind == "task_done":
            print(f"[{ev['time']}] {'✔' if ev.get('ok') else '✘'} {ev.get('task_name')}：{ev.get('message')}")
        elif kind == "reminder":
            print(f"[{ev['time']}] ⏰ 提醒 {ev.get('task_name')}：{ev.get('text')}")
        elif kind == "task_skipped":
            print(f"[{ev['time']}] — 跳过 {ev.get('task_name')}：{ev.get('reason')}")
        elif kind == "backoff":
            print(f"[{ev['time']}] ⏸ {ev.get('task_name')} 连续失败 {ev.get('streak')} 次，"
                  f"{ev.get('seconds')} 秒内不再重试")

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
        lock.release()
        for t in cfg.tasks:
            print(f"  {t.name}: {STATUS_TEXT.get(t.status_enum, t.status)}"
                  + (f" | {t.last_run.summary()}" if t.last_run else ""))
    return 0


def cmd_mock_stop(args) -> int:
    """停止由自动拉起功能启动的模拟问卷服务（PID 记录在 run/mock.pid）。"""
    pid_file = paths.RUN_DIR / "mock.pid"
    if not pid_file.exists():
        print("没有记录的模拟问卷服务进程")
        return 0
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (ValueError, OSError) as exc:
        print(f"读取 PID 失败: {exc}")
        return 1
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=10,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        print(f"已停止模拟问卷服务 (pid={pid})")
    except Exception as exc:
        print(f"停止失败（可手动结束 PID {pid}）: {exc}")
    finally:
        try:
            pid_file.unlink()
        except OSError:
            pass
    return 0


def cmd_demo(args) -> int:
    """一键演示：A 先执行成功后 B 自动跳过；A 失败则 B 继续。

    全程在独立的配置与端口上运行（data/demo-*.json + 8799），
    不会读写你自己的任务配置，也不影响正在运行的模拟服务。
    """
    from wjx import mockserver

    demo_port = 8799
    demo_base = f"http://127.0.0.1:{demo_port}"
    cfg = AppConfig()
    cfg.settings.backend = "mock"
    cfg.settings.mock_base_url = demo_base

    if not manualtest.check_mock_server(cfg.settings):
        print("未检测到演示用的模拟问卷服务，正在自动启动 …")
        if not _ensure_mock_server(cfg, port=demo_port, pid_name="mock-demo.pid"):
            print("模拟问卷服务启动失败，请手动运行: python main.py mock --port 8799")
            return 2

    base = demo_base
    cfg2 = AppConfig()
    cfg2.settings.backend = "mock"
    cfg2.settings.mock_base_url = demo_base

    a = Task(name="问卷A", url=f"{base}/vm/A.aspx", start_at=_past(2),
             answers={"q1": "1", "q2": "A的默认填写内容", "q3": "7"},
             group="今日问卷", priority=10, retry_on_full=0)
    b = Task(name="问卷B", url=f"{base}/vm/B.aspx", start_at=_past(1),
             answers={"q1": "2", "q2": "B的默认填写内容", "q3": "7"},
             group="今日问卷", priority=5, retry_on_full=0,
             start_rule="not submitted('问卷A')")
    cfg.tasks = [a, b]

    print("=" * 74)
    print("场景一：A 成功 → B 应被跳过（互斥 + 条件）")
    print("=" * 74)
    sched = Scheduler(cfg, config_path=False)
    _pump_until_idle(sched, [a, b], budget=6)
    print(f"  A 结果: {a.status} {a.last_run.summary() if a.last_run else ''}")
    print(f"  B 结果: {b.status} {b.last_run.summary() if b.last_run else ''}")
    ok1 = a.status == "success" and b.status == "skipped"

    print()
    print("=" * 74)
    print("场景二：A 提交失败（人数已满）→ B 条件成立，应继续填写 B")
    print("=" * 74)
    a2 = Task(name="问卷A", url=f"{base}/vm/A.aspx?force=full", start_at=_past(2),
              answers={"q1": "1", "q2": "A的默认填写内容", "q3": "7"},
              group="今日问卷", priority=10, retry_on_full=0)
    b2 = Task(name="问卷B", url=f"{base}/vm/B.aspx?force=ok", start_at=_past(1),
              answers={"q1": "2", "q2": "B的默认填写内容", "q3": "7"},
              group="今日问卷", priority=5, retry_on_full=0,
              start_rule="not submitted('问卷A')")
    cfg2.tasks = [a2, b2]
    sched2 = Scheduler(cfg2, config_path=False)
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


def _ensure_mock_server(cfg: AppConfig, port: Optional[int] = None,
                        pid_name: str = "mock.pid") -> bool:
    """确保模拟问卷服务可用。

    * 已经有人（本进程/别的进程）在跑 → 直接返回 True；
    * 否则**在本进程内起一个线程版**服务（打包成 exe 后没有 main.py 可执行，
      原子进程方案在冻结环境里会失败）。

    ``port`` 可显式指定（demo 用 8799 与正式环境隔离）。
    """
    from wjx import mockserver

    base = cfg.settings.mock_base_url or ""
    if "127.0.0.1" not in base and "localhost" not in base:
        return False
    return mockserver.ensure_running(cfg.settings, port=port)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wjx-auto", description="问卷星定时自动填写助手",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__,
    )
    # --config 前后位置都支持：default=SUPPRESS 让子解析器不覆盖已有值
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=argparse.SUPPRESS,
                        help="配置文件路径（默认 data/tasks.json）")
    parser.add_argument("--config", type=Path, default=None, help="配置文件路径（默认 data/tasks.json）")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("gui", help="打开图形界面", parents=[common])
    p.add_argument("--minimized", action="store_true",
                   help="启动后直接最小化到托盘（安装包的开机自启用这个参数）")
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("mock", help="启动本地模拟问卷服务", parents=[common])
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--full-rate", type=float, default=None, help="问卷B 出现'人数已满'的概率（0~1）")
    p.set_defaults(func=cmd_mock)

    p = sub.add_parser("list", help="列出任务", parents=[common])
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("add", help="添加任务", parents=[common])
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

    p = sub.add_parser("remove", help="删除任务", parents=[common])
    p.add_argument("--task", required=True, help="任务名称或 id")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("run", help="立即执行一次并输出判定结果", parents=[common])
    p.add_argument("--task", required=True)
    p.add_argument("--manual", action="store_true",
                   help="忽略答案库，直接用任务里的默认填写内容")
    p.add_argument("--dry-run", action="store_true", help="只检索答案不提交")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("probe", help="读取问卷题目并在答案库中检索答案（不提交）", parents=[common])
    p.add_argument("--url", required=True, help="问卷网址")
    p.add_argument("--static", action="store_true", help="不启动浏览器，直接 HTTP 解析")
    p.add_argument("--brief", action="store_true", help="不显示选项列表")
    p.add_argument("--json", action="store_true", help="额外输出 JSON")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("resolve", help="预演：显示将会填写的内容（不提交）", parents=[common])
    p.add_argument("--url", default="")
    p.add_argument("--task", default="")
    p.add_argument("--static", action="store_true")
    p.add_argument("--brief", action="store_true")
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("fill", help="按题目内容检索答案库并填写提交", parents=[common])
    p.add_argument("--url", default="")
    p.add_argument("--task", default="")
    p.add_argument("--dry-run", action="store_true", help="只检索答案不提交")
    p.add_argument("--headed", action="store_true",
                   help="显示浏览器窗口（不隐藏），可降低触发安全校验的概率")
    p.add_argument("--backend", default=None, choices=["auto", "mock", "selenium"])
    p.add_argument("--browser", default=None, choices=["edge", "chrome"])
    p.set_defaults(func=cmd_fill)

    p = sub.add_parser("bank", help="查看/维护默认答案库", parents=[common])
    p.add_argument("action", choices=["show", "add", "remove", "remember", "clear-pending"])
    p.add_argument("--match", default="text", choices=["text", "regex", "expr"])
    p.add_argument("--pattern", default="", help="题目文本（或 add/remove/remember 的目标）")
    p.add_argument("--answer", default="")
    p.add_argument("--note", default="")
    p.add_argument("--bank-file", default=None, help="指定答案库文件（默认 data/answer_bank.json）")
    p.set_defaults(func=cmd_bank)

    p = sub.add_parser("autostart", parents=[common],
                       help="没有开始时间的任务：从问卷页推导开放时间，到点自动填写")
    p.add_argument("--url", default="")
    p.add_argument("--task", default="", help="已有任务名/id（省略则新建）")
    p.add_argument("--name", default="", help="新建任务的名字")
    p.add_argument("--at", default="", help="手动指定开始时间（跳过推导）")
    p.add_argument("--static", action="store_true", help="用 HTTP 解析推导（不启动浏览器）")
    p.add_argument("--no-run", action="store_true", help="只推导并写入任务，不等待执行")
    p.add_argument("--dry-run", action="store_true", help="到点后只检索答案不提交")
    p.add_argument("--max-wait", type=int, default=0, help="最多等待多少秒（默认=距开放时间+2分钟）")
    p.add_argument("--headed", action="store_true")
    p.add_argument("--backend", default=None, choices=["auto", "mock", "selenium"])
    p.add_argument("--browser", default=None, choices=["edge", "chrome"])
    p.set_defaults(func=cmd_autostart)

    p = sub.add_parser("check-env", help="环境自检（打包后/换机器先跑这个）", parents=[common])
    p.set_defaults(func=cmd_check_env)

    p = sub.add_parser("learn", help="交互式为未命中的题目补答案（写入答案库）", parents=[common])
    p.add_argument("--url", default="", help="问卷网址；省略则处理 pending 列表")
    p.add_argument("--static", action="store_true")
    p.set_defaults(func=cmd_learn)

    p = sub.add_parser("check-rule", help="校验条件表达式", parents=[common])
    p.add_argument("expr")
    p.set_defaults(func=cmd_check_rule)

    p = sub.add_parser("daemon", help="后台静默运行调度器", parents=[common])
    p.add_argument("--seconds", type=int, default=0, help="运行多少秒后退出（0=一直运行）")
    p.add_argument("--scale", type=float, default=1.0, help="加速时钟（仅用于自测）")
    p.set_defaults(func=cmd_daemon)

    p = sub.add_parser("demo", help="一键演示 A/B 条件与优先级场景", parents=[common])
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("mock-stop", help="停止自动拉起的模拟问卷服务", parents=[common])
    p.set_defaults(func=cmd_mock_stop)

    p = sub.add_parser("selftest", help="运行内置自测", parents=[common])
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
