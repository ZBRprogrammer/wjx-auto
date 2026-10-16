"""数据模型：任务、状态、执行结果。

设计要点
--------
* 一次任务执行产生一条 RunRecord，任务的"最终状态"由最后一次执行结果推导。
* submit 结果与业务结果分开：
    - ``result``  描述业务结局（成功 / 人数已满 / 校验失败 ...）
    - ``status``  是给规则引擎用的聚合状态（success/failed/skipped/...）
* 条件规则（rules）与优先级（priority）分离：
    - ``start_rule`` 为假 => 该任务本次运行被跳过（这就是"完成 A 后不再填写 B"）
    - ``group`` 内多个任务同时可用时，按 ``priority`` 只启动其中一个（A/B 二选一）
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional


# --------------------------------------------------------------------------- #
# 状态与结果枚举
# --------------------------------------------------------------------------- #
class TaskStatus(str, Enum):
    IDLE = "idle"            # 尚未执行
    WAITING = "waiting"      # 已到时间但被推迟（例如等更高优先级任务出结果）
    RUNNING = "running"
    SUCCESS = "success"      # 提交成功（跳转到结果页）
    FAILED = "failed"        # 提交失败（仍在原页 / 弹窗人数已满）
    SKIPPED = "skipped"      # 条件不满足或同组优先级落选，本次不填
    ERROR = "error"          # 程序异常（打不开网页等）


class RunResult(str, Enum):
    SUCCESS = "success"                 # 提交后跳转到新页面
    FULL = "full"                       # 弹窗：问卷填写人数过多
    VALIDATION_FAILED = "validation"    # 停留在原页面（必填未通过等）
    UNKNOWN = "unknown"                 # 停留原页但没有识别到弹窗
    BLOCKED = "blocked"                 # 未登录 / 问卷已关闭
    NAVIGATION_ERROR = "navigation_error"
    DRIVER_ERROR = "driver_error"


STATUS_TEXT = {
    TaskStatus.IDLE: "待执行",
    TaskStatus.WAITING: "待定(等前一任务结果)",
    TaskStatus.RUNNING: "执行中",
    TaskStatus.SUCCESS: "✔ 已提交",
    TaskStatus.FAILED: "✘ 提交失败",
    TaskStatus.SKIPPED: "— 已跳过",
    TaskStatus.ERROR: "! 异常",
}

RESULT_TEXT = {
    RunResult.SUCCESS: "提交成功（已跳转）",
    RunResult.FULL: "横幅提示：填写人数过多",
    RunResult.VALIDATION_FAILED: "仍停留原页面（校验未通过）",
    RunResult.UNKNOWN: "仍停留原页面（原因未知）",
    RunResult.BLOCKED: "问卷不可用（已结束/需登录）",
    RunResult.NAVIGATION_ERROR: "无法打开网址",
    RunResult.DRIVER_ERROR: "填写后端异常",
}


# --------------------------------------------------------------------------- #
# 执行记录
# --------------------------------------------------------------------------- #
@dataclass
class RunRecord:
    started_at: str = ""
    finished_at: str = ""
    result: str = RunResult.UNKNOWN.value
    url_before: str = ""
    url_after: str = ""
    dialog_text: str = ""          # 弹窗/横幅文本（例如"填写人数过多"）
    message: str = ""              # 附加说明
    screenshot: str = ""           # 失败截图路径
    dry_run: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RunRecord":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in (d or {}).items() if k in known})

    @property
    def result_enum(self) -> RunResult:
        try:
            return RunResult(self.result)
        except ValueError:
            return RunResult.UNKNOWN

    def summary(self) -> str:
        text = RESULT_TEXT.get(self.result_enum, self.result)
        return f"{text}{('｜' + self.dialog_text) if self.dialog_text else ''}"


# --------------------------------------------------------------------------- #
# 任务
# --------------------------------------------------------------------------- #
# 答案键约定（"默认填写内容"）:
#   {"q1": "A",                  # 单选：选项序号(1 起) 或 选项文案
#    "q2": ["A", "C"],           # 多选：序号/文案列表
#    "q3": "张三",               # 填空
#    "q4": {"index": 2},         # 下拉
#    "q5": {"exclude": true}}    # 特殊：矩阵题等由驱动自行处理
DEFAULT_TEMPLATE_ANSWERS: Dict[str, Any] = {"q1": "A", "q2": "示例文本"}


@dataclass
class Task:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str = ""
    url: str = ""
    enabled: bool = True

    # 触发时间
    start_at: str = ""             # "YYYY-MM-DD HH:MM:SS"，空表示仅手动
    repeat: bool = False           # 每天同一时刻重复
    window_minutes: int = 30       # 到点后仍未执行则视为过期（分钟）

    # 提前准备
    prepare_minutes: int = 5       # 提前 N 分钟打开页面并提醒（1~5）
    reminder_minutes: int = 5      # 提前 N 分钟弹窗/响铃提醒
    preopen: bool = True           # 是否提前打开浏览器加载页面

    # 默认填写内容
    answers: Dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_TEMPLATE_ANSWERS))
    answer_text: str = ""          # 原始文本形式（GUI 编辑用），保存时解析进 answers

    # 条件与优先级
    start_rule: str = ""           # 空 = 无条件；为假则本次跳过
    group: str = ""                # 同组互斥/二选一
    priority: int = 0              # 数值越大优先级越高
    depends_on: List[str] = field(default_factory=list)  # 依赖的任务 id（仅用于 GUI 提示与排序）

    # 执行策略
    retry_on_full: int = 0         # 遇到"人数过多"后最多重试次数
    retry_interval_seconds: int = 60
    submit_after_fill: bool = True
    requires_confirm_after_reminder: bool = False  # 提醒后需人工确认才提交

    # 运行期状态（持久化，便于重启恢复）
    status: str = TaskStatus.IDLE.value
    attempts: int = 0
    last_run: Optional[RunRecord] = None
    history: List[RunRecord] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["last_run"] = self.last_run.to_dict() if self.last_run else None
        d["history"] = [r.to_dict() for r in self.history[-20:]]
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Task":
        d = dict(d or {})
        last = d.pop("last_run", None)
        history = d.pop("history", []) or []
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        task = cls(**{k: v for k, v in d.items() if k in known})
        task.last_run = RunRecord.from_dict(last) if last else None
        task.history = [RunRecord.from_dict(r) for r in history]
        return task

    # ------------------------------------------------------------------ #
    @property
    def status_enum(self) -> TaskStatus:
        try:
            return TaskStatus(self.status)
        except ValueError:
            return TaskStatus.IDLE

    def set_status(self, status: TaskStatus) -> None:
        self.status = status.value

    @property
    def start_datetime(self) -> Optional[datetime]:
        if not self.start_at:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
            try:
                return datetime.strptime(self.start_at, fmt)
            except ValueError:
                continue
        return None

    def next_start(self, now: Optional[datetime] = None) -> Optional[datetime]:
        """考虑每日重复后的下一次触发时间。"""
        now = now or datetime.now()
        base = self.start_datetime
        if base is None:
            return None
        if not self.repeat:
            return base
        candidate = base
        while candidate < now - timedelta(seconds=1):
            candidate += timedelta(days=1)
        return candidate

    def fill_window(self, now: Optional[datetime] = None):
        """返回 (开始时间, 是否已过期)。"""
        now = now or datetime.now()
        start = self.next_start(now)
        if start is None:
            return None, False
        expired = now > start + timedelta(minutes=self.window_minutes)
        return start, expired

    def start_rule_ok(self, context: Dict[str, Any], dry_run: bool = False) -> bool:
        """评估启动条件；解析失败时保守放行并由调用方记录警告。"""
        from .rules import evaluate_rule  # 延迟导入避免循环

        if not self.start_rule.strip():
            return True
        return evaluate_rule(self.start_rule, context)

    def record(self, rec: RunRecord) -> None:
        self.last_run = rec
        self.history.append(rec)
        self.history = self.history[-20:]
        self.attempts += 1
        if rec.result == RunResult.SUCCESS.value:
            self.set_status(TaskStatus.SUCCESS)
        elif rec.result == RunResult.BLOCKED.value:
            self.set_status(TaskStatus.ERROR)
        else:
            self.set_status(TaskStatus.FAILED)

    def reset_state(self) -> None:
        self.status = TaskStatus.IDLE.value
        self.attempts = 0
        self.last_run = None
        self.history.clear()

    def clone(self) -> "Task":
        copy = Task.from_dict(self.to_dict())
        copy.id = self.id          # 编辑副本时保留 id
        return copy

    def describe_condition(self) -> str:
        bits = []
        if self.start_rule.strip():
            bits.append(f"条件: {self.start_rule.strip()}")
        if self.group.strip():
            bits.append(f"组: {self.group}(优先级 {self.priority})")
        return " | ".join(bits)


@dataclass
class AppSettings:
    backend: str = "mock"              # mock | selenium
    headless: bool = True
    browser: str = "edge"              # edge | chrome
    driver_path: str = ""              # msedgedriver/chromedriver，留空则用 selenium-manager
    sound_enabled: bool = True
    popup_enabled: bool = True
    tray_enabled: bool = True          # 最小化到系统托盘，后台低内存运行
    start_minimized: bool = False
    poll_seconds: int = 5              # 调度轮询间隔（秒）
    mock_base_url: str = "http://127.0.0.1:8765"
    page_load_timeout: int = 30
    fill_delay_ms: int = 120           # 每题之间的人工延迟
    submit_wait_seconds: int = 6       # 点击提交后等待判定跳转
    respect_min_interval: int = 3      # 同一任务两次执行的最小间隔（秒）

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AppSettings":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


@dataclass
class AppConfig:
    version: int = 1
    settings: AppSettings = field(default_factory=AppSettings)
    tasks: List[Task] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "settings": self.settings.to_dict(),
            "tasks": [t.to_dict() for t in self.tasks],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AppConfig":
        d = d or {}
        return cls(
            version=int(d.get("version", 1)),
            settings=AppSettings.from_dict(d.get("settings", {})),
            tasks=[Task.from_dict(t) for t in d.get("tasks", [])],
        )

    # -- 便捷查询 ---------------------------------------------------------- #
    def find(self, key: str) -> Optional[Task]:
        """按 id 或名称查找任务（名称匹配不区分大小写）。"""
        if not key:
            return None
        key_l = key.strip().lower()
        for t in self.tasks:
            if t.id == key or t.name.strip().lower() == key_l:
                return t
        return None

    def find_all(self, group: str) -> List[Task]:
        return [t for t in self.tasks if t.group == group]
