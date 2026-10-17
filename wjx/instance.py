"""GUI 单实例控制：同一时刻只允许一个主窗口，避免多开导致的任务管理混乱。

为什么需要
----------
早期实现只用一个 ``run/app.lock`` 控制"谁跑调度器"。重复双击会得到**多个窗口**：
虽然只有一个窗口真正调度任务，但多个窗口各自缓存了一份任务列表，后保存的会覆盖
先保存的（丢失刚刚的编辑），"立即执行"也可能被重复触发。为此增加一个**真正的
单实例守卫**：

* 用 Windows 命名互斥量（``CreateMutexW``）判定是否已有窗口在运行。互斥量随进程
  退出自动释放，不会像锁文件那样因为崩溃而永久残留。
* 名字里带上"数据根目录"的摘要，所以两个**不同位置**的绿色版副本可以各自运行，
  互不干扰。
* 第二次启动不再创建窗口，而是通过"激活请求文件 + 置前窗口"把已有窗口叫到前台。

非 Windows 平台退回"独占创建锁文件"的方式，行为一致。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes  # noqa: F401  （ctypes 不会自动导入子模块）
import hashlib
import os
import sys
import time
from pathlib import Path
from typing import Optional

from . import paths

# 第二次启动时写这个文件，正在运行的实例轮询到就自己弹到前台
ACTIVATION_FILE = paths.RUN_DIR / "gui.activate"

ERROR_ALREADY_EXISTS = 183  # CreateMutexW: 同名互斥量已存在

_OK = "ok"
_BUSY = "busy"
_UNSUPPORTED = "unsupported"


def _scope_digest() -> str:
    """按数据根目录生成摘要：不同安装位置各自独立，同一位置互斥。"""
    try:
        key = str(paths.ROOT).lower().encode("utf-8", "replace")
    except Exception:  # pragma: no cover - 极端情况下退化为固定名字
        key = b"wjx-auto"
    return hashlib.sha1(key).hexdigest()[:12]


def mutex_name() -> str:
    return f"Local\\wjx-auto-gui-{_scope_digest()}"


class GuiInstanceGuard:
    """保证只有一个 GUI 实例。``acquire()`` 返回 False 表示已有一个在运行。"""

    def __init__(self, name: Optional[str] = None, lock_path: Optional[Path] = None):
        self.name = name or mutex_name()
        self.acquired = False
        self._handle = None
        self._lock_path = lock_path or (paths.RUN_DIR / f"gui-{_scope_digest()}.lock")

    # -- 获取 --------------------------------------------------------------- #
    def acquire(self) -> bool:
        state = self._acquire_mutex()
        if state == _OK:
            self.acquired = True
            return True
        if state == _BUSY:
            return False
        # 互斥量不可用（非 Windows 或调用失败）→ 退回锁文件
        return self._acquire_file()

    def _acquire_mutex(self) -> str:
        if sys.platform != "win32":
            return _UNSUPPORTED
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.restype = ctypes.wintypes.HANDLE
            kernel32.CreateMutexW.argtypes = [
                ctypes.wintypes.LPVOID, ctypes.wintypes.BOOL, ctypes.wintypes.LPCWSTR
            ]
            handle = kernel32.CreateMutexW(None, False, self.name)
            if not handle:
                return _UNSUPPORTED
            if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
                kernel32.CloseHandle(handle)
                return _BUSY
            self._handle = handle
            return _OK
        except Exception:
            return _UNSUPPORTED

    def _acquire_file(self) -> bool:
        try:
            paths.ensure_dirs()
            fd = os.open(str(self._lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(str(os.getpid()))
            self.acquired = True
            return True
        except FileExistsError:
            # 上一次没退干净（进程已不存在）→ 接管
            try:
                pid = int(self._lock_path.read_text(encoding="utf-8").strip() or 0)
            except (OSError, ValueError):
                pid = 0
            if pid and not paths._pid_alive(str(pid)):
                try:
                    self._lock_path.unlink()
                except OSError:
                    return False
                return self._acquire_file()
            return False
        except OSError:
            # 无法建锁文件时不阻断启动（宁可多开也不要开不起来）
            self.acquired = True
            return True

    # -- 释放 --------------------------------------------------------------- #
    def release(self) -> None:
        if self._handle is not None:
            try:
                ctypes.WinDLL("kernel32").CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None
        try:
            if self._lock_path.exists():
                self._lock_path.unlink()
        except OSError:
            pass
        self.acquired = False

    # 便于 with 语句使用
    def __enter__(self) -> "GuiInstanceGuard":
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()


# --------------------------------------------------------------------------- #
# 把"已有窗口"叫到前台
# --------------------------------------------------------------------------- #
def request_activation() -> None:
    """请正在运行的实例把窗口显示到前台（由它自己轮询处理）。"""
    try:
        paths.ensure_dirs()
        ACTIVATION_FILE.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def take_activation_request() -> bool:
    """运行中的实例调用：有无待处理的激活请求（取走即删）。"""
    try:
        if ACTIVATION_FILE.exists():
            ACTIVATION_FILE.unlink()
            return True
    except OSError:
        pass
    return False


def focus_existing_window(title_prefix: str = paths.APP_TITLE) -> bool:
    """尽力把标题以 ``title_prefix`` 开头的窗口恢复并置前。

    真正"最小化到托盘"的窗口用 Tk 的 withdraw 隐藏，单纯置前不一定可见，
    所以调用方还应配合 ``request_activation()`` 让程序自己 deiconify。
    """
    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        found: list = []
        enum_proc = ctypes.WINFUNCTYPE(
            ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM
        )
        user32.EnumWindows.argtypes = [enum_proc, ctypes.wintypes.LPARAM]

        def _callback(hwnd, _lparam):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if buf.value.startswith(title_prefix):
                    found.append(hwnd)
            return True

        user32.EnumWindows(enum_proc(_callback), 0)
        if not found:
            return False
        hwnd = found[0]
        SW_RESTORE = 9
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        return True
    except Exception:
        return False
