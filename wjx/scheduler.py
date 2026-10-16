"""调度器：提前提醒 + 提前打开 + 到点自动填写提交 + 条件/优先级裁决。

线程模型
--------
* ``Scheduler`` 自己起一个守护线程，按 ``poll_seconds`` 醒来一次；
* 所有回调（``on_event``）只把事件放进队列，GUI 在主线程取用；
* 每个任务同时只允许一个执行线程，避免重复提交。

关键行为（对应需求）
--------------------
1. 提前 ``reminder_minutes`` 分钟弹窗+响铃提醒；
2. 提前 ``prepare_minutes`` 分钟预打开问卷页面（真实后端）；
3. 到点执行：填写默认内容 -> 点击提交 -> 依据"是否跳转/是否弹窗人数过多"判定；
4. 每次运行前用规则引擎评估 ``start_rule``：不满足则跳过（问卷A提交成功后自动跳过问卷B）；
5. 同 ``group`` 且都可用时按 ``priority`` 启动最高的一个；一旦其中一个提交成功，
   该组其它任务立即标记为跳过（互斥，避免重复提交）。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from . import config as config_mod
from . import notify
from .drivers import DriverError, DriverUnavailable, FillOutcome, create_driver
from .logging_setup import get_logger
from .models import AppConfig, RunRecord, RunResult, STATUS_TEXT, Task, TaskStatus
from .rules import RuleContext, decide, safe_evaluate_rule

log = get_logger()

EventCallback = Callable[[Dict[str, Any]], None]


class Scheduler:
    # ------------------------------------------------------------------ #
    def __init__(self, cfg: AppConfig, on_event: Optional[EventCallback] = None,
                 config_path=None):
        self.cfg = cfg
        self.on_event = on_event
        self.config_path = config_path
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._reminded: Dict[str, str] = {}     # task_id -> 已提醒的计划时间
        self._prepared: Dict[str, Any] = {}     # task_id -> (driver, handle, 计划时间)
        self._running: Dict[str, threading.Thread] = {}
        self._last_start: Dict[str, float] = {}
        self._skip_reason: Dict[str, str] = {}
        self.clock_scale: float = 1.0           # >1 表示加速（仅用于自测）
        self._t0 = time.time()
        self._t0_virtual = datetime.now()
        self._driver = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="wjx-scheduler", daemon=True)
        self._thread.start()
        log.info("调度器已启动（后端=%s，轮询=%ss）", self.cfg.settings.backend, self.cfg.settings.poll_seconds)

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        self._close_all_prepared()
        log.info("调度器已停止")

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def notify_change(self) -> None:
        """配置变更后唤醒调度器重新计算。"""
        self._wake.set()

    # ------------------------------------------------------------------ #
    # 时间与事件
    # ------------------------------------------------------------------ #
    def now(self) -> datetime:
        if self.clock_scale > 1.0:
            return self._t0_virtual + timedelta(seconds=(time.time() - self._t0) * self.clock_scale)
        return datetime.now()

    def _emit(self, kind: str, task: Optional[Task] = None, **extra: Any) -> None:
        payload = {"kind": kind, "time": self.now().strftime("%H:%M:%S")}
        if task is not None:
            payload.update({"task_id": task.id, "task_name": task.name})
        payload.update(extra)
        if self.on_event:
            try:
                self.on_event(payload)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # 守护线程绝不能因异常退出
                log.exception("调度循环异常: %s", exc)
            interval = max(1, int(self.cfg.settings.poll_seconds))
            self._wake.wait(timeout=interval)
            self._wake.clear()

    def _tick(self) -> None:
        now = self.now()
        with self._lock:
            tasks = list(self.cfg.tasks)

        # 1) 提醒 + 预打开
        for task in tasks:
            if not task.enabled or task.next_start(now) is None:
                continue
            start, expired = task.fill_window(now)
            if start is None or expired:
                continue
            if task.status_enum == TaskStatus.SUCCESS and not task.repeat:
                continue
            self._maybe_remind(task, start, now)
            self._maybe_prepare(task, start, now)

        # 2) 找出"到点可执行"的任务，按条件/优先级裁决
        due: List[Task] = []
        for task in tasks:
            if not task.enabled:
                continue
            start, expired = task.fill_window(now)
            if start is None or expired or start > now:
                continue
            if task.id in self._running:
                continue
            if self._in_cooldown(task, now):
                continue
            if self._already_done(task):
                continue
            due.append(task)

        if not due:
            return
        ctx = self.context()
        decision = decide(due, ctx)
        for tid, reason in decision.reasons.items():
            self._skip_reason[tid] = reason
        for tid in decision.skip:
            task = self.cfg.find(tid)
            if task is not None and self._eligible(task, now):
                self._mark_skipped(task, decision.reasons.get(tid, "条件不满足"))

        for tid in decision.run:
            task = self.cfg.find(tid)
            if task is None or not self._eligible(task, now):
                continue
            ok, err = safe_evaluate_rule(task.start_rule, ctx, default=True)
            if not ok:
                self._mark_skipped(task, decision.reasons.get(tid, "条件不满足"))
                continue
            self._start_task(task, reason=decision.reasons.get(task.id, "到点执行"))

    # ------------------------------------------------------------------ #
    # 提醒 / 预打开
    # ------------------------------------------------------------------ #
    def _maybe_remind(self, task: Task, start: datetime, now: datetime) -> None:
        """到达"提前提醒时间"或"提前准备时间"中较早的那个时，提醒一次（每个计划时间只提醒一次）。"""
        remind_at = start - timedelta(minutes=max(0, int(task.reminder_minutes)))
        prepare_at = start - timedelta(minutes=max(0, int(task.prepare_minutes)))
        trigger = min(remind_at, prepare_at)
        if now < trigger:          # 还没到提醒时刻
            return
        key = start.strftime("%Y-%m-%d %H:%M")
        if self._reminded.get(task.id) == key:
            return
        self._reminded[task.id] = key
        seconds = max(0, int((start - now).total_seconds()))
        minutes = seconds // 60
        text = f"{minutes} 分 {seconds % 60} 秒后开始" if seconds >= 60 else f"{seconds} 秒后开始"
        if task.requires_confirm_after_reminder:
            text += "（需手动确认后才会提交）"
        log.info("提醒：%s 将在%s自动填写（%s）", task.name, text, task.url)
        if self.cfg.settings.sound_enabled or self.cfg.settings.popup_enabled:
            notify.alert(
                f"问卷提醒：{task.name}",
                f"{task.name} 将于 {task.start_at} 自动填写并提交（{text}）。\n"
                f"网址：{task.url}\n如需调整请现在处理。",
                sound=self.cfg.settings.sound_enabled,
                popup=self.cfg.settings.popup_enabled,
            )
        self._emit("reminder", task, seconds=seconds, text=text)

    def _maybe_prepare(self, task: Task, start: datetime, now: datetime) -> None:
        """真实后端：到"提前 prepare_minutes 分钟"时先把页面打开（后台预加载）。"""
        if not task.preopen or self.cfg.settings.backend != "selenium":
            return
        prepare_at = start - timedelta(minutes=max(1, int(task.prepare_minutes)))
        if now < prepare_at:
            return
        entry = self._prepared.get(task.id)
        key = start.strftime("%Y-%m-%d %H:%M")
        if entry and entry[2] == key:
            return
        try:
            driver = self._get_driver()
            handle = driver.prepare(task)
            self._prepared[task.id] = (driver, handle, key)
            log.info("已提前打开页面：%s", task.url)
            self._emit("prepared", task)
        except DriverUnavailable as exc:
            log.error("预打开失败（后端不可用）：%s", exc)
            self._emit("error", task, message=str(exc))
        except Exception as exc:
            log.warning("预打开失败：%s", exc)

    def _close_all_prepared(self) -> None:
        for tid, (driver, handle, _key) in list(self._prepared.items()):
            try:
                driver.close_prepared(handle)
            except Exception:
                pass
            self._prepared.pop(tid, None)

    # ------------------------------------------------------------------ #
    # 执行
    # ------------------------------------------------------------------ #
    def _already_done(self, task: Task) -> bool:
        """非重复任务：已执行过就算结束（成功/失败/异常都不再自动重跑，避免窗口内反复提交）。

        重复任务：当天已有终态结果则视为今日完成。
        """
        rec = task.last_run
        if rec is None:
            return False
        if not task.repeat:
            return True
        if not rec.finished_at:
            return False
        try:
            done_day = datetime.strptime(rec.finished_at[:19], "%Y-%m-%d %H:%M:%S").date()
        except ValueError:
            return False
        if done_day != self.now().date():
            return False
        # 当天遇到"人数已满"且还有重试次数时，允许继续重试
        if rec.result == RunResult.FULL.value and task.attempts < int(task.retry_on_full) + 1:
            return False
        return True

    def _eligible(self, task: Task, now: datetime) -> bool:
        """是否仍可能（再次）执行——用于判断同组落选者该"待定"还是"已结束"。"""
        if task.status_enum in (TaskStatus.RUNNING, TaskStatus.SUCCESS, TaskStatus.ERROR):
            return False
        if self._already_done(task):
            return False
        rec = task.last_run
        if rec is not None and rec.result == RunResult.FULL.value:
            return task.attempts < int(task.retry_on_full) + 1
        return True

    def _in_cooldown(self, task: Task, now: datetime) -> bool:
        last = self._last_start.get(task.id)
        if last is None:
            return False
        if time.time() - last < max(0, int(self.cfg.settings.respect_min_interval)):
            return True
        # 人数已满后的重试间隔
        rec = task.last_run
        if rec and rec.result == RunResult.FULL.value:
            if task.attempts > 0 and task.retry_on_full >= 0:
                try:
                    finished = datetime.strptime(rec.finished_at[:19], "%Y-%m-%d %H:%M:%S")
                except (ValueError, TypeError):
                    return False
                if now < finished + timedelta(seconds=max(5, int(task.retry_interval_seconds))):
                    return True
        return False

    def _start_task(self, task: Task, reason: str = "") -> None:
        with self._lock:
            if task.id in self._running:
                return
            self._last_start[task.id] = time.time()
            thread = threading.Thread(target=self._run_task, args=(task, reason),
                                      name=f"wjx-task-{task.id}", daemon=True)
            self._running[task.id] = thread
        thread.start()

    def _run_task(self, task: Task, reason: str = "") -> None:
        task.set_status(TaskStatus.RUNNING)
        self._emit("task_start", task, reason=reason)
        log.info("开始执行：%s（%s）", task.name, reason or "到点执行")
        rec = RunRecord(started_at=self.now().strftime("%Y-%m-%d %H:%M:%S"))

        if task.requires_confirm_after_reminder and not task.answers:
            rec.result = RunResult.UNKNOWN.value
            rec.message = "已设置为'提醒后需人工确认'，本次未提交"
            self._finish(task, rec)
            return

        prepared = self._take_prepared(task)
        outcome = FillOutcome(result=RunResult.UNKNOWN.value)
        try:
            driver = self._get_driver()
            outcome = driver.submit(task, prepared)
        except DriverUnavailable as exc:
            rec.result = RunResult.DRIVER_ERROR.value
            rec.message = str(exc)
            log.error("后端不可用：%s", exc)
        except DriverError as exc:
            rec.result = RunResult.DRIVER_ERROR.value
            rec.message = str(exc)
            log.error("填写失败：%s", exc)
        except Exception as exc:
            text = f"{type(exc).__name__}: {exc}"
            # 打不开页面类的异常归入 navigation_error，便于条件规则区分处理
            if any(k in text for k in ("ConnectionError", "无法连接", "无法打开", "refused",
                                       "timed out", "超时", "URLError", "NameNotResolved")):
                rec.result = RunResult.NAVIGATION_ERROR.value
                rec.message = f"无法打开网址: {exc}"
                log.error("无法打开网址：%s", exc)
            else:
                rec.result = RunResult.DRIVER_ERROR.value
                rec.message = text
                log.exception("填写异常")

        if outcome.result:
            rec.result = outcome.result
        rec.url_before = outcome.url_before or task.url
        rec.url_after = outcome.url_after
        rec.dialog_text = outcome.dialog_text
        rec.message = outcome.message
        rec.finished_at = self.now().strftime("%Y-%m-%d %H:%M:%S")
        if outcome.unfilled:
            rec.message = f"{rec.message}｜未填: {', '.join(outcome.unfilled[:5])}"

        self._finish(task, rec)
        self._maybe_retry(task, rec)

    def _finish(self, task: Task, rec: RunRecord) -> None:
        task.record(rec)
        self._running.pop(task.id, None)
        self._persist()
        result = rec.result_enum
        if result == RunResult.SUCCESS:
            log.info("✔ %s 提交成功（%s -> %s）", task.name, rec.url_before, rec.url_after)
            self._emit("task_done", task, ok=True, message=rec.summary())
            notify.info("提交成功", f"{task.name} 已成功提交。")
            self._enforce_group_exclusive(task)
        elif result == RunResult.FULL:
            log.warning("✘ %s 提交失败：填写人数过多", task.name)
            self._emit("task_done", task, ok=False, message=rec.summary())
        else:
            log.warning("✘ %s 提交未成功：%s", task.name, rec.summary())
            self._emit("task_done", task, ok=False, message=rec.summary())

    def _maybe_retry(self, task: Task, rec: RunRecord) -> None:
        if rec.result != RunResult.FULL.value:
            return
        if task.attempts >= int(task.retry_on_full) + 1:
            return
        delay = max(5, int(task.retry_interval_seconds))
        log.info("%s 遇到人数已满，%s 秒后重试（第 %s 次）", task.name, delay, task.attempts)
        task.set_status(TaskStatus.WAITING)

        def _later() -> None:
            if self._stop.wait(delay):
                return
            self._start_task(task, reason="人数已满重试")

        threading.Thread(target=_later, name=f"wjx-retry-{task.id}", daemon=True).start()

    def _enforce_group_exclusive(self, winner: Task) -> None:
        """同组互斥：一个成功后，其它成员立即跳过（问卷A成功后不再填问卷B）。"""
        group = (winner.group or "").strip()
        if not group:
            return
        for other in self.cfg.find_all(group):
            if other.id == winner.id:
                continue
            if other.status_enum in (TaskStatus.SUCCESS, TaskStatus.SKIPPED):
                continue
            if not self._eligible(other, self.now()):
                continue
            self._mark_skipped(other, f"同组「{group}」中「{winner.name}」已提交成功，跳过")
            self._emit("task_skipped", other, reason=f"同组「{winner.name}」已成功")

    def _mark_skipped(self, task: Task, reason: str) -> None:
        if task.status_enum == TaskStatus.SKIPPED and self._skip_reason.get(task.id) == reason:
            return
        task.set_status(TaskStatus.SKIPPED)
        self._skip_reason[task.id] = reason
        log.info("跳过 %s：%s", task.name, reason)
        self._persist()

    def _take_prepared(self, task: Task):
        entry = self._prepared.pop(task.id, None)
        if not entry:
            return None
        driver, handle, _key = entry
        return handle

    # ------------------------------------------------------------------ #
    # 驱动 / 持久化 / 上下文
    # ------------------------------------------------------------------ #
    def _get_driver(self):
        if self._driver is None:
            self._driver = create_driver(self.cfg.settings)
        return self._driver

    def reset_driver(self) -> None:
        self._driver = None

    def _persist(self) -> None:
        try:
            config_mod.save_config(self.cfg, self.config_path)
        except Exception as exc:
            log.warning("保存配置失败：%s", exc)

    def context(self) -> RuleContext:
        return RuleContext(list(self.cfg.tasks))

    # ------------------------------------------------------------------ #
    # 手动操作 / 状态查询
    # ------------------------------------------------------------------ #
    def run_now(self, task: Task) -> None:
        self._start_task(task, reason="手动触发")

    def skip_reason(self, task_id: str) -> str:
        return self._skip_reason.get(task_id, "")

    def is_running(self, task_id: str) -> bool:
        return task_id in self._running

    def upcoming(self, limit: int = 5) -> List[Dict[str, Any]]:
        now = self.now()
        rows = []
        for task in self.cfg.tasks:
            if not task.enabled:
                continue
            if self._already_done(task):
                continue
            start = task.next_start(now)
            if start is None:
                continue
            rows.append({
                "task": task,
                "start": start,
                "seconds": int((start - now).total_seconds()),
                "prepare_in": int((start - now).total_seconds()) - task.prepare_minutes * 60,
            })
        rows.sort(key=lambda r: r["seconds"])
        return rows[:limit]

    def countdown_text(self) -> str:
        rows = self.upcoming(1)
        if not rows:
            return "无待执行任务"
        row = rows[0]
        secs = row["seconds"]
        sign = "后" if secs >= 0 else "前"
        secs = abs(secs)
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        span = f"{h}小时{m}分" if h else (f"{m}分{s}秒" if m else f"{s}秒")
        return f"下一任务：{row['task'].name} {span}{sign}（{row['start'].strftime('%m-%d %H:%M:%S')}）"
