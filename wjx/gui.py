"""tkinter 图形界面：任务增删改、条件与优先级、默认填写内容、设置、运行日志。

界面结构
--------
工具栏   新建 / 编辑 / 删除 / 启用停用 / 立即执行 / 打开网址 / 测试填写 / 设置 / 最小化到托盘
任务列表 状态 | 名称 | 计划时间 | 分组 | 优先级 | 倒计时 | 上次结果 | 启动条件
详情区   选中任务的完整信息（网址、默认填写内容、条件、历史记录）
状态栏   下一任务倒计时 | 后端 | 调度器状态
日志面板 运行日志（含提醒、跳过原因、提交结果与失败截图路径）
"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from datetime import datetime, timedelta
from tkinter import messagebox, ttk
from typing import Any, Dict, List, Optional

from . import config as config_mod
from . import logging_setup, notify, paths, rules
from . import instance
from .drivers import selenium_available
from .logging_setup import get_logger
from .models import (
    AppConfig,
    AppSettings,
    RESULT_TEXT,
    RunResult,
    STATUS_TEXT,
    Task,
    TaskStatus,
)
from .scheduler import Scheduler

log = get_logger()

BG = "#f5f6f8"
ACCENT = "#2b7de9"
OK_COLOR = "#1a7f37"
BAD_COLOR = "#c62828"
WARN_COLOR = "#b26a00"

HELP_RULE = rules.describe_functions()

TEMPLATE_ANSWERS = (
    "# 默认填写内容（两种写法任选）\n"
    "# 1) 简单键值：值为选项序号(1 起)或选项文案；多个用逗号分隔表示多选\n"
    "q1 = 1\n"
    "q2 = 这是默认填写的文本内容\n"
    "# 2) JSON：\n"
    '# {"q1": "方案一", "q2": ["A", "C"], "q3": "文本框内容"}'
)


# --------------------------------------------------------------------------- #
# 任务编辑对话框
# --------------------------------------------------------------------------- #
class TaskDialog(tk.Toplevel):
    def __init__(self, master: "MainWindow", task: Optional[Task] = None):
        super().__init__(master)
        self.app = master
        self.result: Optional[Task] = None
        self.task = task.clone() if task else Task(name="新任务", start_at=_default_start())

        self.title("编辑任务" if task else "新建任务")
        self.transient(master)
        self.resizable(True, True)
        self.configure(bg=BG)
        self.geometry("760x720")
        self.minsize(680, 560)

        self._build()
        self._load(self.task)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda _e: self._cancel())

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)

        nb = ttk.Notebook(outer)
        nb.pack(fill="both", expand=True)

        # --- 基本 ------------------------------------------------------- #
        basic = ttk.Frame(nb, padding=12)
        nb.add(basic, text=" 基本 ")
        row = 0
        self.var_name = tk.StringVar()
        self.var_url = tk.StringVar()
        self.var_start = tk.StringVar()
        self.var_repeat = tk.BooleanVar()
        self.var_enabled = tk.BooleanVar(value=True)
        self.var_window = tk.IntVar(value=30)

        _label(basic, "任务名称", row); row += 1
        ttk.Entry(basic, textvariable=self.var_name, width=60).grid(row=row, column=0, sticky="we", pady=(0, 8)); row += 1
        _label(basic, "问卷网址（问卷星链接，如 https://v.wjx.cn/vm/xxxx.aspx）", row); row += 1
        url_frame = ttk.Frame(basic)
        url_frame.grid(row=row, column=0, sticky="we", pady=(0, 8)); row += 1
        ttk.Entry(url_frame, textvariable=self.var_url).pack(side="left", fill="x", expand=True)
        ttk.Button(url_frame, text="打开", width=6, command=self._open_url).pack(side="left", padx=(6, 0))

        _label(basic, "开始填写时间（YYYY-MM-DD HH:MM:SS）", row); row += 1
        time_frame = ttk.Frame(basic)
        time_frame.grid(row=row, column=0, sticky="we", pady=(0, 8)); row += 1
        ttk.Entry(time_frame, textvariable=self.var_start, width=24).pack(side="left")
        for text, minutes in (("1分钟后", 1), ("5分钟后", 5), ("30分钟后", 30), ("明天此刻", None)):
            ttk.Button(
                time_frame, text=text, width=9,
                command=lambda m=minutes: self._quick_time(m),
            ).pack(side="left", padx=(6, 0))

        opt = ttk.Frame(basic)
        opt.grid(row=row, column=0, sticky="we", pady=(4, 8)); row += 1
        ttk.Checkbutton(opt, text="每天重复", variable=self.var_repeat).pack(side="left")
        ttk.Checkbutton(opt, text="启用此任务", variable=self.var_enabled).pack(side="left", padx=(16, 0))
        ttk.Label(opt, text="   超时作废(分钟)").pack(side="left")
        ttk.Spinbox(opt, from_=1, to=1440, textvariable=self.var_window, width=6).pack(side="left")

        # --- 提醒与准备 -------------------------------------------------- #
        prep = ttk.Frame(nb, padding=12)
        nb.add(prep, text=" 提醒与准备 ")
        self.var_reminder = tk.IntVar(value=5)
        self.var_prepare = tk.IntVar(value=5)
        self.var_preopen = tk.BooleanVar(value=True)
        self.var_confirm = tk.BooleanVar(value=False)
        self.var_retry_full = tk.IntVar(value=0)
        self.var_retry_interval = tk.IntVar(value=60)

        ttk.Label(prep, text="提前多少分钟提醒（弹窗+提示音，建议 1~5 分钟）").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Spinbox(prep, from_=0, to=60, textvariable=self.var_reminder, width=6).grid(row=0, column=1, sticky="w")
        ttk.Label(prep, text="提前多少分钟打开问卷页面（真实后端会在后台预加载，方便临时干预）").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Spinbox(prep, from_=1, to=30, textvariable=self.var_prepare, width=6).grid(row=1, column=1, sticky="w")
        ttk.Checkbutton(prep, text="提前打开页面（预加载）", variable=self.var_preopen).grid(row=2, column=0, sticky="w", pady=4)
        ttk.Checkbutton(prep, text="提醒后需人工确认才提交（半自动模式）", variable=self.var_confirm).grid(row=3, column=0, sticky="w", pady=4)
        ttk.Label(prep, text="提交遇到「填写人数过多」时最多重试次数").grid(row=4, column=0, sticky="w", pady=4)
        ttk.Spinbox(prep, from_=0, to=10, textvariable=self.var_retry_full, width=6).grid(row=4, column=1, sticky="w")
        ttk.Label(prep, text="重试间隔（秒）").grid(row=5, column=0, sticky="w", pady=4)
        ttk.Spinbox(prep, from_=5, to=3600, textvariable=self.var_retry_interval, width=6).grid(row=5, column=1, sticky="w")

        # --- 条件与优先级 ------------------------------------------------ #
        cond = ttk.Frame(nb, padding=12)
        nb.add(cond, text=" 条件与优先级 ")
        self.var_rule = tk.StringVar()
        self.var_group = tk.StringVar()
        self.var_priority = tk.IntVar(value=0)

        ttk.Label(cond, text="启动条件（留空=无条件执行；条件为假则本次跳过）").grid(row=0, column=0, sticky="w")
        entry = ttk.Entry(cond, textvariable=self.var_rule, width=70)
        entry.grid(row=1, column=0, sticky="we", pady=(2, 2))
        self.rule_status = ttk.Label(cond, text="", foreground=OK_COLOR)
        self.rule_status.grid(row=2, column=0, sticky="w")
        self.var_rule.trace_add("write", lambda *_: self._validate_rule())

        presets = ttk.Frame(cond)
        presets.grid(row=3, column=0, sticky="we", pady=(8, 4))
        ttk.Label(presets, text="常用条件：").pack(side="left")
        for text, expr in (
            ("A未提交才填", "not submitted('问卷A')"),
            ("A已提交才填", "submitted('问卷A')"),
            ("上次人数已满才重试", "result('本任务名') == 'full'"),
            ("清空", ""),
        ):
            ttk.Button(presets, text=text, command=lambda e=expr: self.var_rule.set(e)).pack(side="left", padx=2)

        ttk.Label(cond, text="任务分组（同组内按优先级只启动一个；其中任意一个提交成功，其余自动跳过）").grid(row=4, column=0, sticky="w", pady=(10, 0))
        gframe = ttk.Frame(cond)
        gframe.grid(row=5, column=0, sticky="we", pady=(2, 8))
        ttk.Entry(gframe, textvariable=self.var_group, width=24).pack(side="left")
        ttk.Label(gframe, text="   优先级（数值越大越优先）").pack(side="left")
        ttk.Spinbox(gframe, from_=-99, to=999, textvariable=self.var_priority, width=6).pack(side="left")

        help_box = tk.Text(cond, height=9, wrap="word", bg="#fbfcfe", relief="solid", borderwidth=1)
        help_box.grid(row=6, column=0, sticky="nsew", pady=(6, 0))
        help_box.insert("1.0", HELP_RULE)
        help_box.configure(state="disabled")
        cond.columnconfigure(0, weight=1)
        cond.rowconfigure(6, weight=1)

        # --- 默认填写内容 ------------------------------------------------ #
        ans = ttk.Frame(nb, padding=12)
        nb.add(ans, text=" 默认填写内容（可选覆盖） ")
        ttk.Label(
            ans,
            text="通常留空即可：程序会按题目内容到「答案库」检索答案（题目顺序变化也能匹配）。\n"
                 "只有需要临时覆盖某题时才在这里按字段名写，例如 q3 = 自定义答案；留空则完全使用答案库。",
            foreground="#555", justify="left",
        ).pack(anchor="w")
        self.txt_answers = tk.Text(ans, height=14, wrap="none", font=("Consolas", 10))
        self.txt_answers.pack(fill="both", expand=True, pady=(6, 4))
        bar = ttk.Frame(ans)
        bar.pack(fill="x")
        ttk.Button(bar, text="读取该问卷的题目", command=self._probe_questions).pack(side="left")
        ttk.Button(bar, text="插入模板", command=self._insert_template).pack(side="left", padx=6)
        ttk.Button(bar, text="检查格式", command=self._check_answers).pack(side="left")
        self.answer_status = ttk.Label(bar, text="")
        self.answer_status.pack(side="left", padx=8)

        # --- 底部按钮 ---------------------------------------------------- #
        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=(12, 0))
        self.preview = ttk.Label(bottom, text="", foreground="#555")
        self.preview.pack(side="left")
        ttk.Button(bottom, text="取消", command=self._cancel).pack(side="right")
        ttk.Button(bottom, text="保存", command=self._save).pack(side="right", padx=8)

    # ------------------------------------------------------------------ #
    def _load(self, task: Task) -> None:
        self.var_name.set(task.name)
        self.var_url.set(task.url)
        self.var_start.set(task.start_at or _default_start())
        self.var_repeat.set(task.repeat)
        self.var_enabled.set(task.enabled)
        self.var_window.set(task.window_minutes)
        self.var_reminder.set(task.reminder_minutes)
        self.var_prepare.set(task.prepare_minutes)
        self.var_preopen.set(task.preopen)
        self.var_confirm.set(task.requires_confirm_after_reminder)
        self.var_retry_full.set(task.retry_on_full)
        self.var_retry_interval.set(task.retry_interval_seconds)
        self.var_rule.set(task.start_rule)
        self.var_group.set(task.group)
        self.var_priority.set(task.priority)
        self.txt_answers.delete("1.0", "end")
        self.txt_answers.insert("1.0", task.answer_text or config_mod.format_answer_text(task.answers) or TEMPLATE_ANSWERS)
        self._update_preview()

    def _collect(self) -> Task:
        task = self.task
        task.name = self.var_name.get().strip() or "未命名任务"
        task.url = self.var_url.get().strip()
        task.start_at = self.var_start.get().strip()
        task.repeat = bool(self.var_repeat.get())
        task.enabled = bool(self.var_enabled.get())
        task.window_minutes = int(self.var_window.get() or 30)
        task.reminder_minutes = int(self.var_reminder.get() or 0)
        task.prepare_minutes = int(self.var_prepare.get() or 1)
        task.preopen = bool(self.var_preopen.get())
        task.requires_confirm_after_reminder = bool(self.var_confirm.get())
        task.retry_on_full = int(self.var_retry_full.get() or 0)
        task.retry_interval_seconds = int(self.var_retry_interval.get() or 60)
        task.start_rule = self.var_rule.get().strip()
        task.group = self.var_group.get().strip()
        task.priority = int(self.var_priority.get() or 0)
        task.answer_text = self.txt_answers.get("1.0", "end").strip()
        task.answers = config_mod.parse_answer_text(task.answer_text)
        if task.status_enum == TaskStatus.SUCCESS and not task.repeat:
            pass
        return task

    # ------------------------------------------------------------------ #
    def _save(self) -> None:
        task = self._collect()
        if not task.url:
            messagebox.showwarning("缺少网址", "请填写问卷网址。", parent=self)
            return
        if not task.url.lower().startswith(("http://", "https://")):
            messagebox.showwarning("网址格式", "网址需以 http:// 或 https:// 开头。", parent=self)
            return
        start = task.start_datetime
        if start is None:
            messagebox.showwarning("时间格式", "时间格式应为 YYYY-MM-DD HH:MM:SS。", parent=self)
            return
        err = rules.validate_rule(task.start_rule, self.app.cfg.tasks)
        if err:
            if not messagebox.askyesno("条件语法有误", f"{err}\n\n仍要保存吗？（保存后该任务会被放行执行）", parent=self):
                return
        self.result = task
        self.grab_release()
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.grab_release()
        self.destroy()

    def _open_url(self) -> None:
        url = self.var_url.get().strip()
        if url:
            threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()

    def _quick_time(self, minutes: Optional[int]) -> None:
        base = datetime.now()
        target = base + timedelta(days=1) if minutes is None else base + timedelta(minutes=minutes)
        self.var_start.set(target.strftime("%Y-%m-%d %H:%M:%S"))

    def _insert_template(self) -> None:
        self.txt_answers.delete("1.0", "end")
        self.txt_answers.insert("1.0", TEMPLATE_ANSWERS)

    def _probe_questions(self) -> None:
        """读取该问卷题目，并在下面显示答案库检索结果（方便确认要覆盖哪些字段）。"""
        url = self.var_url.get().strip()
        if not url:
            messagebox.showwarning("缺少网址", "请先填写问卷网址。", parent=self)
            return
        self.answer_status.configure(text="正在读取题目（首次可能启动浏览器）…", foreground="#555")

        def worker() -> None:
            from . import autorun

            try:
                res = autorun.probe_questions(self.app.cfg, url, prefer_browser=True,
                                             bank=self.app.scheduler.answer_bank())
                text = res.report()
                self.after(0, lambda: self._show_probe_result(text, res.ok))
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                self.after(0, lambda: self._show_probe_result(message, False))

        threading.Thread(target=worker, name="wjx-probe", daemon=True).start()

    def _show_probe_result(self, text: str, ok: bool) -> None:
        self.answer_status.configure(text="题目读取完成" if ok else "题目读取失败",
                                     foreground=OK_COLOR if ok else BAD_COLOR)
        win = tk.Toplevel(self)
        win.title("题目与答案库检索结果")
        win.geometry("820x520")
        win.transient(self)
        box = tk.Text(win, wrap="word", font=("Consolas", 10))
        box.pack(fill="both", expand=True, padx=8, pady=8)
        box.insert("1.0", text)
        box.configure(state="disabled")
        ttk.Button(win, text="关闭", command=win.destroy).pack(pady=(0, 8))

    def _check_answers(self) -> None:
        text = self.txt_answers.get("1.0", "end").strip()
        parsed = config_mod.parse_answer_text(text)
        if not parsed:
            self.answer_status.configure(text="未解析到任何答案", foreground=BAD_COLOR)
            return
        self.answer_status.configure(
            text=f"解析到 {len(parsed)} 个字段: {', '.join(list(parsed)[:6])}", foreground=OK_COLOR
        )

    def _validate_rule(self) -> None:
        expr = self.var_rule.get().strip()
        if not expr:
            self.rule_status.configure(text="无条件：到点即执行", foreground="#777")
        else:
            err = rules.validate_rule(expr, self.app.cfg.tasks)
            if err:
                self.rule_status.configure(text=f"✘ {err}", foreground=BAD_COLOR)
            else:
                ok, _ = rules.safe_evaluate_rule(expr, self.app.cfg.tasks)
                self.rule_status.configure(text=f"✔ 语法正确，当前评估为 {'真(会执行)' if ok else '假(会跳过)'}",
                                           foreground=OK_COLOR if ok else WARN_COLOR)
        self._update_preview()

    def _update_preview(self) -> None:
        task = self.task
        try:
            start = datetime.strptime(self.var_start.get().strip(), "%Y-%m-%d %H:%M:%S")
            delta = start - datetime.now()
            secs = int(delta.total_seconds())
            if secs < 0:
                text = f"⚠ 该时间已过去 {abs(secs) // 60} 分钟"
            else:
                text = f"将在 {secs // 3600}小时{(secs % 3600) // 60}分{secs % 60}秒后执行"
        except ValueError:
            text = "时间格式待修正"
        self.preview.configure(text=text)


def _label(parent, text: str, row: int) -> None:
    ttk.Label(parent, text=text).grid(row=row, column=0, sticky="w", pady=(6, 0))


def _default_start() -> str:
    return (datetime.now() + timedelta(minutes=10)).replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _short(text: str, width: int = 34) -> str:
    text = str(text).replace("\n", " ")
    return text if len(text) <= width else text[: width - 1] + "…"


def confirm_delete(parent, kind: str, items: List[str], extra: str = "") -> bool:
    """批量删除确认：列出将要删除的条目，默认焦点在「取消」上。"""
    if not items:
        messagebox.showinfo("提示", f"请先选择要删除的{kind}。", parent=parent)
        return False
    preview = "\n".join(f"  • {_short(x, 46)}" for x in items[:12])
    if len(items) > 12:
        preview += f"\n  … 以及另外 {len(items) - 12} 条"
    message = (f"即将删除 {len(items)} 个{kind}：\n\n{preview}\n"
               + (f"\n{extra}\n" if extra else "")
               + "\n此操作会立即写入配置文件，无法撤销。确定继续吗？")
    return messagebox.askyesno(f"⚠ 批量删除{kind}", message, parent=parent, default="no")


class BatchDeleteDialog(tk.Toplevel):
    """批量删除的二次确认窗口（比 messagebox 更醒目，默认不删）。"""

    def __init__(self, master, kind: str, items: List[str], extra: str = "", width: int = 54):
        super().__init__(master)
        self.confirmed = False
        self.title(f"⚠ 确认批量删除{kind}")
        self.transient(master)
        self.resizable(False, False)
        self.configure(bg="#fff7f7")

        frame = ttk.Frame(self, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"⚠ 即将删除 {len(items)} 个{kind}",
                  font=("Microsoft YaHei UI", 11, "bold"), foreground=BAD_COLOR).pack(anchor="w")
        if extra:
            ttk.Label(frame, text=extra, foreground="#7a4a00", justify="left",
                      wraplength=width * 8).pack(anchor="w", pady=(4, 0))
        box = tk.Text(frame, height=min(12, max(4, len(items))), width=width, wrap="none",
                      bg="#fffdfd", relief="solid", borderwidth=1)
        box.pack(fill="both", expand=True, pady=8)
        for i, item in enumerate(items):
            box.insert("end", f"{i + 1:>3}. {_short(item, 70)}\n")
        box.configure(state="disabled")
        ttk.Label(frame, text="此操作会立即写入配置文件，无法撤销。",
                  foreground="#666").pack(anchor="w")
        btns = ttk.Frame(frame)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="取消（默认）", command=self._cancel).pack(side="right", padx=(8, 0))
        ttk.Button(btns, text=f"确认删除 {len(items)} 项", command=self._ok).pack(side="right")
        self.bind("<Escape>", lambda _e: self._cancel())
        self.bind("<Return>", lambda _e: self._cancel())      # 回车=取消，防误删
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.grab_set()

    def _ok(self) -> None:
        self.confirmed = True
        self.grab_release()
        self.destroy()

    def _cancel(self) -> None:
        self.confirmed = False
        self.grab_release()
        self.destroy()


# --------------------------------------------------------------------------- #
# 设置对话框
# --------------------------------------------------------------------------- #
class SettingsDialog(tk.Toplevel):
    def __init__(self, master: "MainWindow"):
        super().__init__(master)
        self.app = master
        self.saved = False
        self.title("设置")
        self.transient(master)
        self.configure(bg=BG)
        self.resizable(False, False)
        s = master.cfg.settings

        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)

        self.var_backend = tk.StringVar(value=s.backend)
        self.var_browser = tk.StringVar(value=s.browser)
        self.var_headless = tk.BooleanVar(value=s.headless)
        self.var_driver = tk.StringVar(value=s.driver_path)
        self.var_sound = tk.BooleanVar(value=s.sound_enabled)
        self.var_popup = tk.BooleanVar(value=s.popup_enabled)
        self.var_tray = tk.BooleanVar(value=s.tray_enabled)
        self.var_poll = tk.IntVar(value=s.poll_seconds)
        self.var_mock = tk.StringVar(value=s.mock_base_url)
        self.var_submit_wait = tk.IntVar(value=s.submit_wait_seconds)
        self.var_fill_delay = tk.IntVar(value=s.fill_delay_ms)

        r = 0
        ttk.Label(frm, text="填写后端").grid(row=r, column=0, sticky="w", pady=6)
        backend_box = ttk.Combobox(frm, textvariable=self.var_backend, width=28, state="readonly",
                                   values=["mock", "selenium", "auto"])
        backend_box.grid(row=r, column=1, sticky="w")
        r += 1
        note = "selenium 已安装" if selenium_available() else "未安装 selenium（真实问卷需先安装，见 README）"
        ttk.Label(frm, text=note, foreground=OK_COLOR if selenium_available() else WARN_COLOR).grid(
            row=r, column=1, sticky="w")
        r += 1
        ttk.Label(frm, text="浏览器").grid(row=r, column=0, sticky="w", pady=6)
        ttk.Combobox(frm, textvariable=self.var_browser, width=12, state="readonly",
                     values=["edge", "chrome"]).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Label(frm, text="浏览器驱动路径（可留空自动查找）").grid(row=r, column=0, sticky="w", pady=6)
        ttk.Entry(frm, textvariable=self.var_driver, width=42).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Checkbutton(frm, text="无头模式运行（后台静默，不显示浏览器窗口；"
                                  "若问卷要求安全校验可关闭此项）",
                        variable=self.var_headless).grid(row=r, column=1, sticky="w")
        r += 1
        self.var_auto_answer = tk.BooleanVar(value=s.auto_answer)
        ttk.Checkbutton(frm, text="自动读取题目并在答案库中检索答案（推荐开启）",
                        variable=self.var_auto_answer).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Label(frm, text="模拟问卷服务地址（mock 后端）").grid(row=r, column=0, sticky="w", pady=6)
        ttk.Entry(frm, textvariable=self.var_mock, width=42).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Checkbutton(frm, text="提醒时弹窗（Windows 通知）", variable=self.var_popup).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Checkbutton(frm, text="提醒时播放提示音", variable=self.var_sound).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Checkbutton(frm, text="最小化到系统托盘（后台静默低占用运行）", variable=self.var_tray).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Label(frm, text="调度轮询间隔（秒，越大越省电）").grid(row=r, column=0, sticky="w", pady=6)
        ttk.Spinbox(frm, from_=1, to=60, textvariable=self.var_poll, width=8).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Label(frm, text="点击提交后判定等待（秒）").grid(row=r, column=0, sticky="w", pady=6)
        ttk.Spinbox(frm, from_=2, to=60, textvariable=self.var_submit_wait, width=8).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Label(frm, text="每题填写间隔（毫秒，模拟人工）").grid(row=r, column=0, sticky="w", pady=6)
        ttk.Spinbox(frm, from_=0, to=3000, increment=20, textvariable=self.var_fill_delay, width=8).grid(row=r, column=1, sticky="w")
        r += 1

        buttons = ttk.Frame(frm)
        buttons.grid(row=r, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="保存", command=self._save).pack(side="right", padx=8)

        self.grab_set()

    def _save(self) -> None:
        s = self.app.cfg.settings
        s.backend = self.var_backend.get()
        s.browser = self.var_browser.get()
        s.headless = bool(self.var_headless.get())
        s.driver_path = self.var_driver.get().strip()
        s.sound_enabled = bool(self.var_sound.get())
        s.popup_enabled = bool(self.var_popup.get())
        s.tray_enabled = bool(self.var_tray.get())
        s.poll_seconds = int(self.var_poll.get() or 5)
        s.mock_base_url = self.var_mock.get().strip() or s.mock_base_url
        s.submit_wait_seconds = int(self.var_submit_wait.get() or 6)
        s.fill_delay_ms = int(self.var_fill_delay.get() or 0)
        s.auto_answer = bool(self.var_auto_answer.get())
        self.saved = True
        self.grab_release()
        self.destroy()


# --------------------------------------------------------------------------- #
# 答案库管理对话框
# --------------------------------------------------------------------------- #
class BankDialog(tk.Toplevel):
    """默认答案库维护：按题目内容维护"题目 → 答案"的规则。"""

    def __init__(self, master: "MainWindow"):
        super().__init__(master)
        self.app = master
        self.title("默认答案库")
        self.transient(master)
        self.geometry("1000x620")
        self.minsize(880, 520)
        self.configure(bg=BG)

        from .answers import AnswerBank

        self.bank = AnswerBank.load()

        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text=f"文件：{self.bank.path}").pack(anchor="w")
        ttk.Label(
            outer,
            text="规则按题目内容匹配：text=题目文字（含关键词即可）、regex=正则、expr=算术题自动计算。"
                 "答案里写选项文字或序号均可；多选题用逗号分隔。",
            foreground="#555",
        ).pack(anchor="w", pady=(0, 6))

        mid = ttk.Frame(outer)
        mid.pack(fill="both", expand=True)

        cols = ("enabled", "match", "pattern", "answer", "note")
        heads = {"enabled": ("启用", 50), "match": ("匹配", 60), "pattern": ("题目（文本/正则）", 300),
                 "answer": ("答案", 220), "note": ("备注", 240)}
        self.tree = ttk.Treeview(mid, columns=cols, show="headings",
                                 selectmode="extended")   # 支持多选/批量删除
        for c in cols:
            text, width = heads[c]
            self.tree.heading(c, text=text)
            self.tree.column(c, width=width, anchor="w", stretch=(c in ("pattern", "answer", "note")))
        vsb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._load_selected())
        # 删除快捷键：Del / BackSpace（默认 Del），支持 Ctrl+A 全选
        self.tree.bind("<Delete>", lambda _e: self._delete_selected())
        self.tree.bind("<BackSpace>", lambda _e: self._delete_selected())
        self.tree.bind("<Control-a>", self._select_all)
        self.tree.bind("<Button-3>", self._popup_menu)
        self._menu = tk.Menu(self, tearoff=0)
        self._menu.add_command(label="删除选中规则 (Del)", command=self._delete_selected)
        self._menu.add_command(label="全选 (Ctrl+A)", command=lambda: self._select_all(None))
        self._menu.add_separator()
        self._menu.add_command(label="启用/停用选中", command=self._toggle_selected)

        # 编辑区
        form = ttk.LabelFrame(outer, text="新增 / 修改", padding=8)
        form.pack(fill="x", pady=(8, 4))
        self.var_match = tk.StringVar(value="text")
        self.var_pattern = tk.StringVar()
        self.var_answer = tk.StringVar()
        self.var_note = tk.StringVar()

        ttk.Label(form, text="匹配方式").grid(row=0, column=0, sticky="w")
        ttk.Combobox(form, textvariable=self.var_match, width=8, state="readonly",
                     values=["text", "regex", "expr"]).grid(row=0, column=1, sticky="w", padx=(4, 12))
        ttk.Label(form, text="题目文字/正则").grid(row=0, column=2, sticky="w")
        ttk.Entry(form, textvariable=self.var_pattern, width=42).grid(row=0, column=3, sticky="we", padx=(4, 12))
        ttk.Label(form, text="答案").grid(row=0, column=4, sticky="w")
        ttk.Entry(form, textvariable=self.var_answer, width=22).grid(row=0, column=5, sticky="we", padx=4)
        ttk.Label(form, text="备注").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(form, textvariable=self.var_note, width=30).grid(row=1, column=1, columnspan=3,
                                                                  sticky="we", padx=(4, 12), pady=(6, 0))
        form.columnconfigure(3, weight=1)
        form.columnconfigure(5, weight=1)

        btns = ttk.Frame(outer)
        btns.pack(fill="x", pady=(4, 4))
        for text, cmd in (
            ("新增规则", self._add),
            ("保存修改", self._update_selected),
            ("删除选中(Del)", self._delete_selected),
            ("启用/停用", self._toggle_selected),
            ("重新载入", self._reload),
            ("保存到文件", self._save),
            ("一键去重", self._dedupe),
            ("清理未配置占位规则", self._clean_placeholders),
        ):
            ttk.Button(btns, text=text, command=cmd).pack(side="left", padx=3)
        ttk.Button(btns, text="关闭", command=self.destroy).pack(side="right")
        self.status = ttk.Label(outer, text="", foreground=OK_COLOR)
        self.status.pack(anchor="w", pady=(0, 4))
        ttk.Label(outer, text="提示：可按住 Ctrl / Shift 多选，按 Delete 键批量删除；右键有菜单。",
                  foreground="#666").pack(anchor="w")

        # 未命中题目
        pend = ttk.LabelFrame(outer, text="答案库未命中的题目（可复制题干后新增规则）", padding=6)
        pend.pack(fill="both", expand=False)
        self.pending_text = tk.Text(pend, height=7, wrap="word", bg="#fffdf5")
        self.pending_text.pack(fill="both", expand=True)
        ttk.Button(pend, text="把选中的题干填入上方『题目文字』", command=self._use_pending).pack(anchor="e", pady=(4, 0))

        self._refresh()
        self.grab_set()

    # ------------------------------------------------------------------ #
    def _refresh(self) -> None:
        selected = self.tree.selection()
        selected_id = selected[0] if selected else None
        self.tree.delete(*self.tree.get_children())
        for rule in self.bank.rules:
            # id 兜底：极端情况下（历史文件 id 重复）也不让界面炸掉
            iid = rule.id
            n = 2
            while self.tree.exists(iid):
                iid = f"{rule.id}#{n}"
                n += 1
            try:
                self.tree.insert("", "end", iid=iid, values=(
                    "是" if rule.enabled else "否", rule.match,
                    rule.pattern, str(rule.answer), rule.note,
                ))
            except tk.TclError:
                continue
        if selected_id and self.tree.exists(selected_id):
            self.tree.selection_set(selected_id)
        self._refresh_pending()

    def _refresh_pending(self) -> None:
        from .answers import load_pending

        pending = load_pending()
        self.pending_text.configure(state="normal")
        self.pending_text.delete("1.0", "end")
        if not pending:
            self.pending_text.insert("1.0", "（暂无。运行任务后未命中的题目会自动记录到这里）")
        for it in pending:
            self.pending_text.insert("end", f"[{it.get('qtype','')}] {it.get('question','')}\n")
            if it.get("options"):
                self.pending_text.insert("end", "     选项: " + " / ".join(
                    str(o.get("label")) for o in it["options"][:10]) + "\n")
        self.pending_text.configure(state="disabled")

    def _say(self, text: str, ok: bool = True) -> None:
        self.status.configure(text=text, foreground=OK_COLOR if ok else BAD_COLOR)
        self.app._log_line(f"答案库：{text}", OK_COLOR if ok else BAD_COLOR)

    # ------------------------------------------------------------------ #
    def _selected_rule(self):
        sel = self.tree.selection()
        if not sel:
            return None
        key = sel[0].split("#")[0]
        for rule in self.bank.rules:
            if rule.id == key:
                return rule
        return None

    def _load_selected(self) -> None:
        rule = self._selected_rule()
        if rule:
            self.var_match.set(rule.match)
            self.var_pattern.set(rule.pattern)
            self.var_answer.set(str(rule.answer))
            self.var_note.set(rule.note)

    def _add(self) -> None:
        """新增规则。任何异常都会被显示出来，绝不静默失败。"""
        try:
            pattern = self.var_pattern.get().strip()
            if not pattern:
                messagebox.showwarning("缺少题目", "请先填写题目文字（或正则表达式）。", parent=self)
                return
            match = self.var_match.get()
            answer = self.var_answer.get().strip()
            if not answer and match != "expr":
                messagebox.showwarning("缺少答案", "请填写答案（匹配方式为 expr 时不需要）。", parent=self)
                return
            rule, created = self.bank.add_rule_safe(
                pattern, answer, match=match, note=self.var_note.get().strip())
            self.bank.save()
            self.app.scheduler.reload_bank()
            self._refresh()
            action = "已新增" if created else "已更新（同一题目的重复规则已合并）"
            self._say(f"{action}规则 [{rule.id}] {rule.match} {rule.pattern} → {rule.answer}")
            self.tree.selection_set(rule.id)
        except tk.TclError as exc:
            self._say(f"界面刷新失败：{exc}", ok=False)
        except Exception as exc:
            import traceback

            traceback.print_exc()
            self._say(f"新增失败：{type(exc).__name__}: {exc}", ok=False)
            messagebox.showerror("新增规则失败", f"{type(exc).__name__}: {exc}", parent=self)

    def _update_selected(self) -> None:
        try:
            rule = self._selected_rule()
            if rule is None:
                messagebox.showinfo("提示", "请先在列表中选择要修改的规则。", parent=self)
                return
            match = self.var_match.get()
            pattern = self.var_pattern.get().strip()
            answer = self.var_answer.get().strip()
            if not pattern:
                messagebox.showwarning("缺少题目", "题目文字不能为空。", parent=self)
                return
            if match == "regex":
                import re as _re

                try:
                    rule._regex = _re.compile(pattern, _re.I)
                except _re.error as exc:
                    messagebox.showerror("正则无效", str(exc), parent=self)
                    return
            elif match == "text":
                import re as _re

                rule._regex = _re.compile(_re.escape(pattern), _re.I)
            rule.match, rule.pattern, rule.answer = match, pattern, answer
            rule.note = self.var_note.get().strip()
            self.bank.save()
            self.app.scheduler.reload_bank()
            self._refresh()
            self._say(f"已保存修改：{rule.pattern} → {rule.answer}")
        except Exception as exc:
            import traceback

            traceback.print_exc()
            self._say(f"保存失败：{type(exc).__name__}: {exc}", ok=False)
            messagebox.showerror("保存修改失败", f"{type(exc).__name__}: {exc}", parent=self)

    def _remove_selected(self) -> None:
        """兼容旧入口：现在走批量删除。"""
        self._delete_selected()

    def _select_all(self, _event=None):
        self.tree.selection_set(self.tree.get_children())
        self._say(f"已选中 {len(self.tree.selection())} 条规则")
        return "break"

    def _popup_menu(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if row and row not in self.tree.selection():
            self.tree.selection_set(row)
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._menu.grab_release()

    def _delete_selected(self) -> None:
        """批量删除选中规则（Del 快捷键 / 按钮 / 右键菜单）。

        三重保护：
          1. 只按 id 精确删除，绝不会因 pattern 相同而连带删除其它规则；
          2. 删除前列出全部将删条目并要求确认（默认选中「取消」）；
          3. 若某规则正被任务的启动条件引用，另行提示。
        """
        try:
            selected = list(self.tree.selection())
            if not selected:
                self._say("没有选中任何规则（可按住 Ctrl/Shift 多选，或 Ctrl+A 全选）", ok=False)
                return
            rules = [r for r in (self._rule_by_iid(i) for i in selected) if r is not None]
            if not rules:
                self._say("选中的条目已不存在，正在刷新列表", ok=False)
                self._refresh()
                return

            items = [f"[{r.match}] {r.pattern}  →  {r.answer}" for r in rules]
            extra = ""
            # 提示：这些规则是否正在被任务条件引用
            try:
                expr_blob = " ".join((t.start_rule or "") for t in self.app.cfg.tasks)
                hit = [r.pattern for r in rules if r.pattern and r.pattern in expr_blob]
                if hit:
                    extra = ("注意：以下规则的题目文字出现在任务启动条件里："
                             + "、".join(hit[:5]) + "，删除后条件可能失效。")
            except Exception:
                pass

            dlg = BatchDeleteDialog(self, "答案库规则", items, extra=extra)
            self.wait_window(dlg)
            if not dlg.confirmed:
                self._say("已取消删除")
                return

            removed = self.bank.remove_rules([r.id for r in rules])
            self.bank.save()
            self.app.scheduler.reload_bank()
            self._refresh()
            self._say(f"已删除 {removed} 条规则（剩余 {len(self.bank.rules)} 条）")
        except Exception as exc:
            import traceback

            traceback.print_exc()
            self._say(f"删除失败：{type(exc).__name__}: {exc}", ok=False)
            messagebox.showerror("删除失败", f"{type(exc).__name__}: {exc}", parent=self)

    def _rule_by_iid(self, iid: str):
        key = iid.split("#")[0]
        for rule in self.bank.rules:
            if rule.id == key:
                return rule
        return None

    def _dedupe(self) -> None:
        """一键去重：把"匹配方式+题目+答案"完全相同的重复规则合并。"""
        try:
            groups = self.bank.duplicate_groups()
            if not groups:
                self._say("没有发现重复规则")
                return
            total = sum(len(g) - 1 for _p, g in groups)
            items = [f"{p}（{len(g)} 条重复，保留 1 条）" for p, g in groups[:30]]
            dlg = BatchDeleteDialog(self, "重复规则",
                                    items, extra=f"共 {len(groups)} 组、将删除 {total} 条重复项。")
            self.wait_window(dlg)
            if not dlg.confirmed:
                self._say("已取消去重")
                return
            removed = self.bank.deduplicate()
            self.bank.save()
            self.app.scheduler.reload_bank()
            self._refresh()
            self._say(f"去重完成：删除 {removed} 条重复规则（剩余 {len(self.bank.rules)} 条）")
        except Exception as exc:
            self._say(f"去重失败：{type(exc).__name__}: {exc}", ok=False)

    def _toggle_selected(self) -> None:
        try:
            selected = list(self.tree.selection())
            rules = [r for r in (self._rule_by_iid(i) for i in selected) if r is not None]
            if not rules:
                messagebox.showinfo("提示", "请先在列表中选择规则。", parent=self)
                return
            # 以第一条的状态取反，其余统一跟随（便于批量启用/停用）
            target = not rules[0].enabled
            for rule in rules:
                rule.enabled = target
            self.bank.save()
            self.app.scheduler.reload_bank()
            self._refresh()
            self._say(f"已{'启用' if target else '停用'} {len(rules)} 条规则")
        except Exception as exc:
            self._say(f"操作失败：{type(exc).__name__}: {exc}", ok=False)

    def _reload(self) -> None:
        try:
            from .answers import AnswerBank

            self.bank = AnswerBank.load()
            self._refresh()
            self._say(f"已重新载入：{len(self.bank.rules)} 条规则")
        except Exception as exc:
            self._say(f"重新载入失败：{type(exc).__name__}: {exc}", ok=False)

    def _save(self) -> None:
        self.bank.save()
        self.app.scheduler.reload_bank()
        self.app._log_line(f"答案库已保存到 {self.bank.path}")
        messagebox.showinfo("已保存", f"答案库已保存：\n{self.bank.path}", parent=self)

    def _clean_placeholders(self) -> None:
        removed = [r for r in self.bank.rules if isinstance(r.answer, str) and r.answer.startswith("未配置")]
        if not removed:
            messagebox.showinfo("无需清理", "没有未配置的占位规则。", parent=self)
            return
        if not messagebox.askyesno("清理占位规则", f"删除 {len(removed)} 条尚未配置答案的示例规则？", parent=self):
            return
        for r in removed:
            self.bank.remove_rule(r.id)
        self.bank.save()
        self.app.scheduler.reload_bank()
        self._refresh()

    def _use_pending(self) -> None:
        try:
            sel = self.pending_text.get("sel.first", "sel.last")
        except tk.TclError:
            messagebox.showinfo("提示", "请先在下面的列表里选中一段题干文字。", parent=self)
            return
        line = sel.strip().lstrip("[").split("]", 1)
        text = (line[1] if len(line) > 1 else sel).strip()
        self.var_pattern.set(text[:200])
        self.var_match.set("text")


# --------------------------------------------------------------------------- #
# 主窗口
# --------------------------------------------------------------------------- #
class MainWindow(tk.Tk):
    def __init__(self, cfg: AppConfig, config_path=None, guard=None):
        super().__init__()
        self.cfg = cfg
        self.config_path = config_path
        self._guard = guard
        self.events: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.scheduler = Scheduler(cfg, on_event=self.events.put, config_path=config_path)
        self._tray = None
        self._hidden = False

        self.title(f"{paths.APP_TITLE}  v{__import__('wjx').__version__}")
        self.geometry("1320x780")
        self.minsize(1000, 620)
        self.configure(bg=BG)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._apply_icon()

        # 单实例互斥：daemon 已在跑调度器时，本窗口只做查看/编辑，不重复执行任务
        self.lock = paths.AppLock()
        self.owns_scheduler = self.lock.acquire()

        self._build()
        self._install_notify_listener()
        if self.owns_scheduler:
            self.scheduler.start()
        else:
            self._log_line(
                f"检测到 {self.lock.describe_holder()} 已在运行调度器：本窗口仅用于查看/编辑，"
                "不会重复执行任务（避免重复提交）。", WARN_COLOR)
        self.after(500, self._pump)
        self.after(1000, self._refresh)
        # 重复启动的另一个进程会写下激活请求，这里轮询并把窗口叫到前台
        self.after(800, self._poll_activation)
        self._setup_tray()
        # 开机自启（--minimized）：窗口建好后直接收进托盘，不打扰用户
        if self.cfg.settings.start_minimized and self._tray is not None:
            self.after(600, self.hide_to_tray)
            self.cfg.settings.start_minimized = False   # 只在本次启动生效

    def _apply_icon(self) -> None:
        """窗口标题栏/任务栏图标（icon.ico 与打包 exe、安装包用同一个文件）。"""
        icon = paths.ICON_FILE
        if not icon.exists():
            return
        try:
            self.iconbitmap(default=str(icon))
            return
        except tk.TclError:
            pass
        try:
            self.iconbitmap(str(icon))
        except tk.TclError:
            pass

    def _poll_activation(self) -> None:
        """另一个进程双击启动时：把本窗口显示出来并置前（含已收进托盘的情况）。"""
        try:
            if instance.take_activation_request():
                self.show_window()
                self._log_line("检测到重复启动：已切换到当前窗口（不重复开新窗口）。")
        except tk.TclError:
            return
        except Exception:
            pass
        try:
            self.after(800, self._poll_activation)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ #
    # 界面
    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=24)

        # 工具栏
        bar = ttk.Frame(self, padding=(10, 8))
        bar.pack(fill="x")
        buttons = [
            ("＋ 新建任务", self.add_task),
            ("✎ 编辑", self.edit_task),
            ("－ 删除(Del)", self.delete_task),
            ("⏻ 启用/停用", self.toggle_task),
            ("▶ 立即执行", self.run_task_now),
            ("↗ 打开网址", self.open_url),
            ("🧪 预演(不提交)", self.preview_task),
            ("📚 答案库", self.open_bank),
            ("⚙ 设置", self.open_settings),
            ("⤓ 托盘运行", self.hide_to_tray),
        ]
        for text, cmd in buttons:
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=3)
        self.lbl_next = ttk.Label(bar, text="", foreground=ACCENT)
        self.lbl_next.pack(side="right")

        # 任务表格 + 详情
        paned = ttk.Panedwindow(self, orient="vertical")
        paned.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        top = ttk.Frame(paned)
        cols = ("status", "name", "start", "countdown", "group", "priority", "rule", "last")
        headings = {
            "status": ("状态", 110), "name": ("名称", 150), "start": ("计划时间", 150),
            "countdown": ("倒计时", 110), "group": ("分组", 90), "priority": ("优先级", 60),
            "rule": ("启动条件", 260), "last": ("上次结果", 260),
        }
        self.tree = ttk.Treeview(top, columns=cols, show="headings",
                                 selectmode="extended")   # 支持多选/批量删除
        for c in cols:
            text, width = headings[c]
            self.tree.heading(c, text=text, command=lambda cc=c: self._sort_by(cc))
            self.tree.column(c, width=width, anchor="w", stretch=(c in ("rule", "last")))
        vsb = ttk.Scrollbar(top, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._show_detail())
        self.tree.bind("<Double-1>", lambda _e: self.edit_task())
        # 批量删除：Del / BackSpace、Ctrl+A 全选、右键菜单
        self.tree.bind("<Delete>", lambda _e: self.delete_task())
        self.tree.bind("<BackSpace>", lambda _e: self.delete_task())
        self.tree.bind("<Control-a>", self._select_all_tasks)
        self.tree.bind("<Button-3>", self._popup_menu)
        self._menu = tk.Menu(self, tearoff=0)
        self._menu.add_command(label="删除选中任务 (Del)", command=self.delete_task)
        self._menu.add_command(label="全选 (Ctrl+A)", command=lambda: self._select_all_tasks(None))
        self._menu.add_separator()
        self._menu.add_command(label="启用/停用", command=self.toggle_task)
        self._menu.add_command(label="立即执行", command=self.run_task_now)
        self._menu.add_command(label="编辑", command=self.edit_task)
        self.tree.tag_configure("success", foreground=OK_COLOR)
        self.tree.tag_configure("failed", foreground=BAD_COLOR)
        self.tree.tag_configure("skipped", foreground="#888")
        self.tree.tag_configure("running", foreground=ACCENT)
        self.tree.tag_configure("disabled", foreground="#aaa")
        paned.add(top, weight=3)

        bottom = ttk.Frame(paned)
        self.detail = tk.Text(bottom, height=9, wrap="word", bg="#ffffff", relief="solid", borderwidth=1)
        self.detail.pack(fill="both", expand=True)
        self.detail.configure(state="disabled")
        paned.add(bottom, weight=2)

        # 日志
        logframe = ttk.LabelFrame(self, text="运行日志", padding=6)
        logframe.pack(fill="both", expand=False, padx=10, pady=(0, 6))
        self.log_text = tk.Text(logframe, height=8, wrap="word", bg="#111", fg="#d7ffd7",
                                insertbackground="#d7ffd7", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True)

        # 状态栏
        status = ttk.Frame(self, padding=(10, 4))
        status.pack(fill="x")
        self.lbl_status = ttk.Label(status, text="", foreground="#444")
        self.lbl_status.pack(side="left")
        self.lbl_clock = ttk.Label(status, text="", foreground="#666")
        self.lbl_clock.pack(side="right")

        self._refresh()

    # ------------------------------------------------------------------ #
    def _install_notify_listener(self) -> None:
        def listener(payload: Dict[str, str]) -> None:
            self.events.put({"kind": "notify", **payload})

        notify.add_listener(listener)

    def _setup_tray(self) -> None:
        if not self.cfg.settings.tray_enabled:
            return
        try:
            import pystray  # type: ignore
            from PIL import Image, ImageDraw  # type: ignore
        except Exception:
            self._log_line("未安装 pystray/Pillow，托盘功能不可用（可用窗口最小化代替）")
            return

        def make_icon():
            # 优先用项目自带的 icon.ico，找不到时才画一个占位图标
            icon_path = paths.ICON_FILE
            if icon_path.exists():
                try:
                    return Image.open(str(icon_path)).convert("RGBA")
                except Exception:
                    pass
            img = Image.new("RGB", (64, 64), "#2b7de9")
            d = ImageDraw.Draw(img)
            d.rectangle([14, 18, 50, 46], outline="white", width=4)
            d.line([20, 26, 44, 26], fill="white", width=3)
            d.line([20, 34, 44, 34], fill="white", width=3)
            return img

        def on_show(_icon, _item):
            self.after(0, self.show_window)

        def on_run_now(_icon, _item):
            self.after(0, self.run_task_now)

        def on_quit(_icon, _item):
            self.after(0, self.quit_app)

        try:
            menu = pystray.Menu(
                pystray.MenuItem("显示主窗口", on_show, default=True),
                pystray.MenuItem("立即执行选中任务", on_run_now),
                pystray.MenuItem("退出", on_quit),
            )
            self._tray = pystray.Icon("wjx-auto", make_icon(), paths.APP_TITLE, menu)
            threading.Thread(target=self._tray.run, name="wjx-tray", daemon=True).start()
            self._log_line("托盘图标已启用，关闭窗口后将静默驻留后台。")
        except Exception as exc:
            self._tray = None
            self._log_line(f"托盘启用失败: {exc}")

    # ------------------------------------------------------------------ #
    # 定时刷新
    # ------------------------------------------------------------------ #
    def _pump(self) -> None:
        # 调度事件
        drained = 0
        while drained < 50:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                break
            drained += 1
            kind = ev.get("kind")
            if kind == "reminder":
                self._log_line(f"⏰ 提醒：{ev.get('task_name')} {ev.get('text')}", WARN_COLOR)
            elif kind == "prepared":
                self._log_line(f"🌐 已提前打开：{ev.get('task_name')}")
            elif kind == "task_start":
                self._log_line(f"▶ 开始执行：{ev.get('task_name')}（{ev.get('reason')}）", ACCENT)
            elif kind == "task_done":
                color = OK_COLOR if ev.get("ok") else BAD_COLOR
                symbol = "✔" if ev.get("ok") else "✘"
                self._log_line(f"{symbol} {ev.get('task_name')}：{ev.get('message')}", color)
            elif kind == "prepared_stale":
                self._log_line(f"♻ {ev.get('task_name')} 预打开的浏览器已失效，将重新启动", WARN_COLOR)
            elif kind == "backoff":
                self._log_line(f"⏸ {ev.get('task_name')} 连续失败 {ev.get('streak')} 次，"
                               f"{ev.get('seconds')} 秒内不再重试", WARN_COLOR)
            elif kind == "not_open":
                self._log_line(f"⏳ {ev.get('task_name')}：{ev.get('message')}", WARN_COLOR)
            elif kind == "task_skipped":
                self._log_line(f"— 跳过 {ev.get('task_name')}：{ev.get('reason')}")
            elif kind == "unresolved":
                self._log_line(f"❓ {ev.get('task_name')}：{ev.get('message')}", WARN_COLOR)
            elif kind == "preview":
                color = OK_COLOR if ev.get("ok") else BAD_COLOR
                self._log_line(f"🔍 预演结果（{ev.get('task_name')}）：", color)
                for line in str(ev.get("report", "")).splitlines():
                    self._log_line("    " + line, color)
            elif kind == "notify":
                self._log_line(f"🔔 {ev.get('title')}：{ev.get('message')}", WARN_COLOR)
            elif kind == "error":
                self._log_line(f"! {ev.get('message')}", BAD_COLOR)

        # 日志模块队列
        for line in logging_setup.drain_events(50):
            self._log_line(line)

        self.after(600, self._pump)

    def _refresh(self) -> None:
        selected = self.tree.selection()
        selected_id = selected[0] if selected else None
        now = self.scheduler.now()
        self.tree.delete(*self.tree.get_children())
        for task in self.cfg.tasks:
            tag = "normal"
            status = STATUS_TEXT.get(task.status_enum, task.status)
            if not task.enabled:
                tag, status = "disabled", "已停用"
            elif self.scheduler.is_running(task.id):
                tag, status = "running", "执行中…"
            elif task.status_enum == TaskStatus.SUCCESS:
                tag = "success"
            elif task.status_enum in (TaskStatus.FAILED, TaskStatus.ERROR):
                tag = "failed"
            elif task.status_enum == TaskStatus.SKIPPED:
                tag = "skipped"

            start = task.next_start(now)
            if start is None:
                start_text, countdown = "仅手动", "-"
            else:
                delta = int((start - now).total_seconds())
                start_text = start.strftime("%m-%d %H:%M:%S")
                if self.scheduler._already_done(task):
                    countdown = "已完成"
                elif delta < 0:
                    countdown = f"已过 {-delta // 60}分"
                else:
                    countdown = f"{delta // 3600}h{(delta % 3600) // 60}m{delta % 60}s"

            last = task.last_run
            last_text = last.summary() if last else "—"
            if task.id in self.scheduler._skip_reason and task.status_enum == TaskStatus.SKIPPED:
                last_text = f"跳过: {self.scheduler._skip_reason[task.id]}"

            self.tree.insert("", "end", iid=task.id, tags=(tag,), values=(
                status, task.name, start_text, countdown,
                task.group or "-", task.priority,
                task.start_rule or "-", last_text,
            ))
        if selected_id and self.tree.exists(selected_id):
            self.tree.selection_set(selected_id)
        self.lbl_next.configure(text=self.scheduler.countdown_text())
        self.lbl_status.configure(
            text=f"任务 {len(self.cfg.tasks)} 个 | 后端 {self.cfg.settings.backend} | "
                 f"调度器 {'运行中(本窗口)' if self.owns_scheduler else '由其它实例运行中'} | "
                 f"轮询 {self.cfg.settings.poll_seconds}s"
        )
        self.lbl_clock.configure(text=now.strftime("%Y-%m-%d %H:%M:%S"))
        self._show_detail()
        self.after(1000, self._refresh)

    # ------------------------------------------------------------------ #
    def _show_detail(self) -> None:
        task = self.selected_task()
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        if task is None:
            self.detail.insert("1.0", "选中一个任务查看详情；双击可编辑。\n\n" + HELP_RULE)
        else:
            lines = [
                f"名称：{task.name}    状态：{STATUS_TEXT.get(task.status_enum, task.status)}",
                f"网址：{task.url}",
                f"计划：{task.start_at or '(仅手动)'}    重复：{'每天' if task.repeat else '否'}"
                f"    超时作废：{task.window_minutes} 分钟",
                f"提醒：提前 {task.reminder_minutes} 分钟    预打开：提前 {task.prepare_minutes} 分钟"
                f"（{'开' if task.preopen else '关'}）",
                f"分组：{task.group or '-'}    优先级：{task.priority}    重试(人数已满)：{task.retry_on_full} 次"
                f"/间隔 {task.retry_interval_seconds}s",
                f"启动条件：{task.start_rule or '(无条件，到点执行)'}",
                "",
                "默认填写内容：",
            ]
            lines.append(config_mod.format_answer_text(task.answers) or "（未设置）")
            if task.history:
                lines += ["", f"最近执行记录（共 {len(task.history)} 次）："]
                for rec in task.history[-5:][::-1]:
                    lines.append(
                        f"  [{rec.finished_at or rec.started_at}] {RESULT_TEXT.get(rec.result_enum, rec.result)}"
                        f"｜{rec.message[:80]}"
                    )
                    if rec.screenshot:
                        lines.append(f"      截图: {rec.screenshot}")
            self.detail.insert("1.0", "\n".join(lines))
        self.detail.configure(state="disabled")

    def selected_task(self) -> Optional[Task]:
        sel = self.tree.selection()
        if not sel:
            return None
        return self.cfg.find(sel[0])

    # ------------------------------------------------------------------ #
    # 任务操作
    # ------------------------------------------------------------------ #
    def add_task(self) -> None:
        dlg = TaskDialog(self, None)
        self.wait_window(dlg)
        if dlg.result:
            self.cfg.tasks.append(dlg.result)
            self._save_cfg()
            self._log_line(f"已新建任务：{dlg.result.name}")
            self.scheduler.notify_change()

    def edit_task(self) -> None:
        task = self.selected_task()
        if task is None:
            messagebox.showinfo("提示", "请先在列表中选择一个任务。", parent=self)
            return
        dlg = TaskDialog(self, task)
        self.wait_window(dlg)
        if dlg.result:
            for i, t in enumerate(self.cfg.tasks):
                if t.id == dlg.result.id:
                    self.cfg.tasks[i] = dlg.result
                    break
            self._save_cfg()
            self._log_line(f"已修改任务：{dlg.result.name}")
            self.scheduler.notify_change()

    def delete_task(self) -> None:
        """批量删除选中任务（Del 快捷键 / 按钮 / 右键菜单），删除前逐条列出并要求确认。"""
        selected = list(self.tree.selection())
        tasks = [self.cfg.find(i) for i in selected]
        tasks = [t for t in tasks if t is not None]
        if not tasks:
            messagebox.showinfo("提示", "请先在任务列表中选择要删除的任务"
                                        "（可按住 Ctrl/Shift 多选，或 Ctrl+A 全选）。", parent=self)
            return

        items = []
        for t in tasks:
            state = STATUS_TEXT.get(t.status_enum, t.status)
            items.append(f"{t.name}｜{t.start_at or '仅手动'}｜{state}"
                         + (f"｜{t.start_rule}" if t.start_rule else ""))

        extra = ""
        # 若被删任务正被其它任务的启动条件/分组依赖，明确警示
        remaining = [t for t in self.cfg.tasks if t.id not in {x.id for x in tasks}]
        expr_blob = " ".join((t.start_rule or "") for t in remaining)
        group_blob = {t.group for t in remaining if t.group}
        refs = [t.name for t in tasks if t.name and t.name in expr_blob]
        group_refs = [t.name for t in tasks if t.group and t.group in group_blob]
        if refs:
            extra += "注意：以下任务名出现在其它任务的启动条件里：" + "、".join(refs[:5]) + "。"
        if group_refs:
            extra += ("注意：以下任务与其它任务同组，删除会改变『互斥/优先级』行为："
                      + "、".join(group_refs[:5]) + "。")
        running = [t.name for t in tasks if self.scheduler.is_running(t.id)]
        if running:
            extra += "以下任务正在执行中：" + "、".join(running) + "（删除不会中断当前执行）。"

        dlg = BatchDeleteDialog(self, "任务", items, extra=extra)
        self.wait_window(dlg)
        if not dlg.confirmed:
            self._log_line("已取消删除任务")
            return

        names = [t.name for t in tasks]
        ids = {t.id for t in tasks}
        self.cfg.tasks = [t for t in self.cfg.tasks if t.id not in ids]
        self._save_cfg()
        self._log_line(f"已删除 {len(names)} 个任务：{'、'.join(names[:6])}"
                       + ("…" if len(names) > 6 else ""))
        self.scheduler.notify_change()
        self._refresh_once()

    def _select_all_tasks(self, _event=None):
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def _popup_menu(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if row and row not in self.tree.selection():
            self.tree.selection_set(row)
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._menu.grab_release()

    def toggle_task(self) -> None:
        task = self.selected_task()
        if task is None:
            return
        task.enabled = not task.enabled
        if task.enabled and task.status_enum == TaskStatus.SKIPPED:
            task.set_status(TaskStatus.IDLE)
            self.scheduler._skip_reason.pop(task.id, None)
        self._save_cfg()
        self._log_line(f"{'启用' if task.enabled else '停用'}任务：{task.name}")
        self.scheduler.notify_change()

    def run_task_now(self) -> None:
        task = self.selected_task()
        if task is None:
            messagebox.showinfo("提示", "请先在列表中选择一个任务。", parent=self)
            return
        if task.status_enum == TaskStatus.SKIPPED:
            task.set_status(TaskStatus.IDLE)
            self.scheduler._skip_reason.pop(task.id, None)
        self.scheduler.run_now(task)

    def open_url(self) -> None:
        task = self.selected_task()
        if task is None or not task.url:
            return
        threading.Thread(target=lambda: webbrowser.open(task.url), daemon=True).start()

    def test_fill(self) -> None:
        """用当前后端立即跑一次"填写+提交"，结果记入历史（用于验证默认内容是否正确）。"""
        task = self.selected_task()
        if task is None:
            return
        if not messagebox.askyesno("测试填写", f"立即对「{task.name}」执行一次填写并提交？\n{task.url}", parent=self):
            return
        self._log_line(f"🧪 测试填写：{task.name}")
        self.scheduler.run_now(task)

    def preview_task(self) -> None:
        """预演：读取题目 + 检索答案库，展示将会填写的内容（不提交）。"""
        task = self.selected_task()
        if task is None:
            return
        self._log_line(f"🔍 预演：{task.name}（读取题目并检索答案库）")

        def worker() -> None:
            from . import autorun

            try:
                res = autorun.probe_questions(self.cfg, task.url, prefer_browser=True,
                                              bank=self.scheduler.answer_bank())
                self.events.put({"kind": "preview", "task_name": task.name,
                                 "report": res.report(), "ok": res.ok,
                                 "unresolved": [q.label for q in res.unresolved]})
            except Exception as exc:
                self.events.put({"kind": "error", "message": f"{type(exc).__name__}: {exc}"})

        threading.Thread(target=worker, name="wjx-preview", daemon=True).start()

    def open_bank(self) -> None:
        BankDialog(self)

    def open_settings(self) -> None:
        dlg = SettingsDialog(self)
        self.wait_window(dlg)
        if dlg.saved:
            self._save_cfg()
            self.scheduler.reset_driver()
            self.scheduler.notify_change()
            self._log_line("设置已保存")
            if self.cfg.settings.tray_enabled and self._tray is None:
                self._setup_tray()

    # ------------------------------------------------------------------ #
    # 窗口 / 托盘
    # ------------------------------------------------------------------ #
    def hide_to_tray(self) -> None:
        if self._tray is None:
            self.iconify()
            return
        self.withdraw()
        self._hidden = True
        self._log_line("已最小化到托盘，程序继续在后台静默运行。")

    def show_window(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()
        self._hidden = False

    def on_close(self) -> None:
        if self._tray is not None and self.cfg.settings.tray_enabled:
            self.hide_to_tray()
            notify.toast(paths.APP_TITLE, "已转入后台静默运行，任务到点会自动填写并提醒。")
            return
        if messagebox.askyesno("退出", "退出后到点将不再自动填写，确定退出？", parent=self):
            self.quit_app()

    def quit_app(self) -> None:
        try:
            self.scheduler.stop()
        finally:
            if self._tray is not None:
                try:
                    self._tray.stop()
                except Exception:
                    pass
            self._save_cfg()
            self.lock.release()
            self.destroy()
    # ------------------------------------------------------------------ #
    def _save_cfg(self) -> None:
        try:
            config_mod.save_config(self.cfg, self.config_path)
        except Exception as exc:
            self._log_line(f"保存配置失败：{exc}", BAD_COLOR)

    def _log_line(self, text: str, color: str = "#d7ffd7") -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{ts}] {text}\n", ("c",))
        self.log_text.tag_configure("c", foreground=color)
        self.log_text.see("end")
        # 控制行数，避免长时间运行内存增长
        if int(self.log_text.index("end-1c").split(".")[0]) > 500:
            self.log_text.delete("1.0", "100.0")

    def _sort_by(self, col: str) -> None:
        key_map = {
            "name": lambda t: t.name,
            "start": lambda t: t.start_at,
            "group": lambda t: t.group,
            "priority": lambda t: -t.priority,
            "status": lambda t: t.status,
        }
        key = key_map.get(col)
        if key:
            self.cfg.tasks.sort(key=key)
            self._save_cfg()
            self._refresh_once()

    def _refresh_once(self) -> None:
        now = self.scheduler.now()
        self.tree.delete(*self.tree.get_children())
        for task in self.cfg.tasks:
            self.tree.insert("", "end", iid=task.id, values=(
                STATUS_TEXT.get(task.status_enum, task.status), task.name,
                task.next_start(now).strftime("%m-%d %H:%M:%S") if task.next_start(now) else "仅手动",
                "", task.group or "-", task.priority, task.start_rule or "-",
                task.last_run.summary() if task.last_run else "—",
            ))


def run_gui(cfg: AppConfig, config_path=None) -> int:
    logging_setup.setup(gui=True)

    # 单实例：已有窗口在跑时不再开第二个窗口，只把它叫到前台。
    # 多窗口会各自缓存一份任务列表，后保存的覆盖先保存的（丢失编辑），
    # "立即执行"也可能被重复触发，所以必须保证只有一个 GUI。
    guard = instance.GuiInstanceGuard()
    if not guard.acquire():
        if cfg.settings.start_minimized:
            # 开机自启/静默启动：不抢焦点，静默退出即可
            print("[wjx-auto] 程序已在运行，本次启动忽略。")
            return 0
        instance.request_activation()
        instance.focus_existing_window()
        print("[wjx-auto] 程序已在运行，已切换到已有窗口（不再重复打开）。")
        return 0

    try:
        app = MainWindow(cfg, config_path, guard=guard)
        app.mainloop()
    finally:
        guard.release()
    return 0
