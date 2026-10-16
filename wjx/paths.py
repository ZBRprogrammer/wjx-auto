"""项目内路径统一出口，所有运行期数据都落在项目目录下（便携、免管理员权限）。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def ensure_vendor_on_path() -> bool:
    """把项目内 vendor/ 加入模块搜索路径（可选依赖用 pip --target vendor 安装）。"""
    vendor = ROOT / "vendor"
    if vendor.is_dir():
        entry = str(vendor)
        if entry not in sys.path:
            sys.path.append(entry)
        return True
    return False


def setup_environment() -> Path:
    """设置运行期目录与第三方工具的环境变量（必须在导入 selenium 前调用）。

    * ``vendor/`` 加入模块路径（selenium 等可选依赖）
    * selenium-manager 缓存目录指向项目内（避免系统目录权限问题）
    * ``TMP``/``TEMP`` 指向项目内：Chromium 需要在临时目录解包扩展/写 profile，
      某些受限环境禁止访问系统临时目录，会报 "cannot create temp dir"。
    """
    ensure_dirs()
    ensure_vendor_on_path()
    cache = ROOT / ".selenium-cache"
    cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("SE_CACHE_PATH", str(cache))
    os.environ.setdefault("SE_AVOID_STATS", "true")
    tmp = RUN_DIR / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    os.environ["TMP"] = str(tmp)
    os.environ["TEMP"] = str(tmp)
    return cache

DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
RUN_DIR = ROOT / "run"
SHOT_DIR = LOG_DIR / "screenshots"

CONFIG_FILE = DATA_DIR / "tasks.json"
LOG_FILE = LOG_DIR / "wjx.log"
LOCK_FILE = RUN_DIR / "app.lock"

# 浏览器驱动默认查找位置（用户可放入项目 drivers/ 目录）
DRIVER_DIR = ROOT / "drivers"
DRIVER_CANDIDATES = {
    "edge": ("msedgedriver.exe", "msedgedriver"),
    "chrome": ("chromedriver.exe", "chromedriver"),
}

APP_TITLE = "问卷星自动填写助手"


def ensure_dirs() -> None:
    for d in (DATA_DIR, LOG_DIR, RUN_DIR, SHOT_DIR):
        d.mkdir(parents=True, exist_ok=True)


def find_bundled_driver(browser: str = "edge") -> str:
    """在项目 drivers/ 目录里找与浏览器匹配的驱动，找不到返回空字符串。"""
    names = DRIVER_CANDIDATES.get((browser or "edge").lower(), ())
    for name in names:
        candidate = DRIVER_DIR / name
        if candidate.exists():
            return str(candidate)
    if DRIVER_DIR.is_dir():
        for f in DRIVER_DIR.iterdir():
            if f.is_file() and f.name.lower().startswith(("msedgedriver", "chromedriver")):
                return str(f)
    return ""


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")
