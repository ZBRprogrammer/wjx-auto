"""配置与任务持久化（JSON 原子写，带 .bak 备份）。"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import paths
from .models import AppConfig, AppSettings, RunRecord, Task, TaskStatus


def atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        if path.exists():
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def load_config(path: Optional[Path] = None) -> AppConfig:
    path = path or paths.CONFIG_FILE
    if not path.exists():
        cfg = AppConfig()
        cfg.tasks = default_tasks(cfg.settings)
        return cfg
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (json.JSONDecodeError, OSError):
        backup = path.with_suffix(path.suffix + ".bak")
        if backup.exists():
            with open(backup, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        else:
            return AppConfig()
    cfg = AppConfig.from_dict(raw)
    for task in cfg.tasks:
        task.answers = parse_answer_text(task.answer_text) if task.answer_text else task.answers
    return cfg


def save_config(cfg: AppConfig, path: Optional[Path] = None) -> None:
    path = path or paths.CONFIG_FILE
    atomic_write_json(path, cfg.to_dict())


# --------------------------------------------------------------------------- #
# 默认填写内容：文本 <-> 字典
# --------------------------------------------------------------------------- #
def parse_answer_text(text: str) -> Dict[str, Any]:
    """解析 GUI 中的多行文本为答案字典。

    两种写法（可混用，逐行解析）::

        # JSON（推荐，支持多选列表）
        {"q1": "A", "q2": ["A", "C"], "q3": "文本"}

        # 简单键值行
        q1 = A
        q2 = A, C
        q3 = 张三
    """
    text = (text or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return {str(k): v for k, v in data.items()}
        except json.JSONDecodeError:
            pass
    answers: Dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
        elif ":" in line:
            key, value = line.split(":", 1)
        else:
            continue
        key, value = key.strip(), value.strip()
        if not key:
            continue
        if "," in value or "，" in value:
            parts = [p.strip() for p in value.replace("，", ",").split(",") if p.strip()]
            answers[key] = parts if len(parts) > 1 else (parts[0] if parts else "")
        else:
            answers[key] = value
    return answers


def format_answer_text(answers: Dict[str, Any]) -> str:
    if not answers:
        return ""
    if all(isinstance(v, (str, int, float)) for v in answers.values()):
        return "\n".join(f"{k} = {v}" for k, v in answers.items())
    return json.dumps(answers, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# 示例任务（首次启动时生成，直接对应需求里的 A/B 场景）
# --------------------------------------------------------------------------- #
def default_tasks(settings: Optional[AppSettings] = None) -> List[Task]:
    """首次启动生成的示例任务：直接对应需求中的 A/B 场景。

    A 无启动条件、优先级更高；B 的条件是"A 未提交成功才填"。
    两者同组，因此 A 成功后 B 会被自动跳过。
    """
    base = (settings or AppSettings()).mock_base_url.rstrip("/")
    start_a = datetime.now() + timedelta(minutes=3)
    start_b = datetime.now() + timedelta(minutes=33)
    a = Task(
        name="问卷A",
        url=f"{base}/vm/A.aspx",
        start_at=start_a.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S"),
        answers={"q1": "1", "q2": "这是问卷A的默认填写内容"},
        group="今日问卷",
        priority=10,
        start_rule="",
        reminder_minutes=5,
        prepare_minutes=5,
    )
    b = Task(
        name="问卷B",
        url=f"{base}/vm/B.aspx",
        start_at=start_b.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S"),
        answers={"q1": "2", "q2": "这是问卷B的默认填写内容"},
        group="今日问卷",
        priority=5,
        start_rule="not submitted('问卷A')",
        reminder_minutes=5,
        prepare_minutes=5,
    )
    return [a, b]


def _today_at(h: int, m: int, s: int = 0) -> str:
    now = datetime.now()
    return now.replace(hour=h, minute=m, second=s, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- #
# 便捷操作
# --------------------------------------------------------------------------- #
def upsert_task(cfg: AppConfig, task: Task, path: Optional[Path] = None) -> Task:
    for i, existing in enumerate(cfg.tasks):
        if existing.id == task.id:
            cfg.tasks[i] = task
            save_config(cfg, path)
            return task
    cfg.tasks.append(task)
    save_config(cfg, path)
    return task


def delete_task(cfg: AppConfig, task_id: str, path: Optional[Path] = None) -> bool:
    before = len(cfg.tasks)
    cfg.tasks = [t for t in cfg.tasks if t.id != task_id]
    if len(cfg.tasks) != before:
        save_config(cfg, path)
        return True
    return False


def sample_config_demo() -> AppConfig:
    """文档/测试用的完整示例（含条件与优先级）。"""
    cfg = AppConfig()
    base = cfg.settings.mock_base_url.rstrip("/")
    a = Task(name="问卷A", url=f"{base}/vm/A.aspx", start_at="2026-01-01 12:00:00",
             answers={"q1": "1", "q2": "A组答案"}, group="今日问卷", priority=10)
    b = Task(name="问卷B", url=f"{base}/vm/B.aspx", start_at="2026-01-01 12:30:00",
             answers={"q1": "2", "q2": "B组答案"}, group="今日问卷", priority=5,
             start_rule="not submitted('问卷A')")
    cfg.tasks = [a, b]
    return cfg
