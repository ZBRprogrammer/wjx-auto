"""项目内路径统一出口，所有运行期数据都落在项目目录下（便携、免管理员权限）。

打包成 exe 后（PyInstaller）：
* ``sys.frozen`` 为真，只读资源在 ``sys._MEIPASS``（onefile 时是临时解包目录）；
* 可写数据（data/ logs/ run/）放在 **exe 所在目录**旁，保证用户能直接查看/编辑配置、
  且程序升级换 exe 不会丢数据。
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def _detect_root() -> Path:
    """确定"数据根目录"：源码运行时是项目根，打包后是 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        # 优先用 exe 同级目录；不可写（例如装在 Program Files）时退回用户目录
        if os.access(str(exe_dir), os.W_OK):
            return exe_dir
        fallback = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "wjx-auto"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback
    return Path(__file__).resolve().parent.parent


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """只读资源根目录（打包后是解包目录，源码运行时是项目根）。"""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return ROOT


ROOT = _detect_root()
BUNDLE_DIR = resource_root()


def ensure_vendor_on_path() -> bool:
    """把 vendor/ 加入模块搜索路径（可选依赖用 pip --target vendor 安装）。

    打包成 exe 后依赖已内置，vendor 目录可能不存在，此时直接跳过。
    """
    for base in (ROOT, BUNDLE_DIR):
        vendor = base / "vendor"
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


def find_bundled_driver(browser: str = "edge", strict: bool = False) -> str:
    """查找与本机浏览器匹配的驱动。

    查找顺序：exe/项目同级 ``drivers/`` → 打包内置资源目录 → 同目录直接放驱动。
    ``strict=True`` 时只认该浏览器自己的驱动名（不拿另一个浏览器的驱动顶替）。
    """
    names = DRIVER_CANDIDATES.get((browser or "edge").lower(), ())
    search_dirs = []
    for base in (ROOT, BUNDLE_DIR):
        if base not in search_dirs:
            search_dirs.append(base)
    for base in search_dirs:
        driver_dir = base / "drivers"
        for name in names:
            candidate = driver_dir / name
            if candidate.exists():
                return str(candidate)
        # 允许把驱动直接放在 exe 旁边
        for name in names:
            candidate = base / name
            if candidate.exists():
                return str(candidate)
    if strict:
        return ""
    # 兜底：drivers/ 里有任何驱动就拿去试（selenium-manager 风格）
    for base in search_dirs:
        driver_dir = base / "drivers"
        if driver_dir.is_dir():
            for f in driver_dir.iterdir():
                if f.is_file() and f.name.lower().startswith(("msedgedriver", "chromedriver")):
                    return str(f)
    return ""


# --------------------------------------------------------------------------- #
# 单实例互斥：避免 GUI 与 daemon 同时跑调度器造成重复提交
# --------------------------------------------------------------------------- #
class AppLock:
    """基于"独占创建文件"的跨进程互斥锁（Windows 上 os.open O_EXCL 可靠）。"""

    def __init__(self, path: Optional[Path] = None):
        self.path = path or LOCK_FILE
        self.acquired = False
        self.holder = ""

    def acquire(self) -> bool:
        ensure_dirs()
        payload = f"{os.getpid()}\n{datetime.now().isoformat(timespec='seconds')}\n"
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            self.acquired = True
            return True
        except FileExistsError:
            self.holder = self._read_holder()
            if self.holder and not _pid_alive(self.holder):
                # 上一次没退干净：接管这把锁
                try:
                    self.path.unlink()
                except OSError:
                    pass
                return self.acquire()
            return False

    def _read_holder(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8").splitlines()[0].strip()
        except (OSError, IndexError):
            return ""

    def release(self) -> None:
        if self.acquired:
            try:
                self.path.unlink()
            except OSError:
                pass
            self.acquired = False

    def describe_holder(self) -> str:
        pid = self._read_holder()
        return f"另一个实例（pid={pid}）" if pid else "另一个实例"


def _pid_alive(pid: str) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {value}", "/NH"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return str(value) in (out.stdout or "")
    except Exception:
        return True     # 判断不了就当作还活着，宁可不抢锁


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")
