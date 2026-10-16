"""手动/脚本化测试辅助：不需要第三方 WebDriver，直接用后端跑一次填写提交。"""

from __future__ import annotations

from typing import Any, Optional

from .drivers import create_driver
from .models import AppConfig, RunRecord, Task
from .scheduler import Scheduler


def fill_once(cfg: AppConfig, task: Task, record: bool = True) -> RunRecord:
    """同步执行一次"填写→提交→判定"，返回执行记录。"""
    driver = create_driver(cfg.settings)
    rec = RunRecord(started_at=_now())
    try:
        outcome = driver.submit(task, None)
        rec.result = outcome.result
        rec.url_before = outcome.url_before
        rec.url_after = outcome.url_after
        rec.dialog_text = outcome.dialog_text
        rec.message = outcome.message
    except Exception as exc:
        rec.result = "driver_error"
        rec.message = f"{type(exc).__name__}: {exc}"
    rec.finished_at = _now()
    if record:
        task.record(rec)
    return rec


def simulate(cfg: AppConfig, task: Task, force: str = "") -> RunRecord:
    """按指定结局执行一次（force 仅在 mock 后端生效: ok / full / fail）。"""
    backup = task.url
    if force:
        sep = "&" if "?" in task.url else "?"
        task.url = f"{backup}{sep}force={force}"
    try:
        return fill_once(cfg, task)
    finally:
        task.url = backup


def check_mock_server(settings: Any, timeout: float = 2.0) -> bool:
    import json
    import urllib.request

    url = str(settings.mock_base_url).rstrip("/") + "/stats"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            json.loads(resp.read().decode("utf-8", "replace"))
        return True
    except Exception:
        return False


def _now() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
