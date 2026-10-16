"""项目内路径统一出口，所有运行期数据都落在项目目录下（便携、免管理员权限）。"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
RUN_DIR = ROOT / "run"
SHOT_DIR = LOG_DIR / "screenshots"

CONFIG_FILE = DATA_DIR / "tasks.json"
LOG_FILE = LOG_DIR / "wjx.log"
LOCK_FILE = RUN_DIR / "app.lock"

APP_TITLE = "问卷星自动填写助手"


def ensure_dirs() -> None:
    for d in (DATA_DIR, LOG_DIR, RUN_DIR, SHOT_DIR):
        d.mkdir(parents=True, exist_ok=True)


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")
