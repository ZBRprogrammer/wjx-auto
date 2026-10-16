"""提醒：Windows 弹窗（toast）、提示音、控制台蜂鸣。

原则：提醒路径尽量不引入常驻内存——只在真正需要提醒时才调用系统命令/加载模块。
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import threading
from typing import Callable, Dict, List, Optional

from .paths import APP_TITLE

_IS_WINDOWS = sys.platform.startswith("win")

# 通知器：注册回调后可把提醒同时推给 GUI（用于界面内提示与日志）
_LISTENERS: List[Callable[[Dict[str, str]], None]] = []


def add_listener(fn: Callable[[Dict[str, str]], None]) -> None:
    if fn not in _LISTENERS:
        _LISTENERS.append(fn)


def _emit(kind: str, title: str, message: str) -> None:
    payload = {"kind": kind, "title": title, "message": message}
    for fn in list(_LISTENERS):
        try:
            fn(payload)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 弹窗
# --------------------------------------------------------------------------- #
_TOAST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType=WindowsRuntime]
$template = @"
<toast duration="long">
  <visual>
    <binding template="ToastGeneric">
      <text>{title}</text>
      <text>{message}</text>
    </binding>
  </visual>
</toast>
"@
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($template)
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{app}').Show($toast)
"""


def _xml_escape(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def toast(title: str, message: str, timeout: int = 10) -> bool:
    """Windows 通知中心气泡；失败返回 False（由调用方决定是否退化为其它方式）。"""
    if not _IS_WINDOWS:
        return False
    script = _TOAST_SCRIPT.replace("{title}", _xml_escape(title)).replace(
        "{message}", _xml_escape(message)
    ).replace("{app}", APP_TITLE)
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
            capture_output=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return completed.returncode == 0
    except Exception:
        return False


def beep(style: str = "alarm") -> bool:
    """提示音。style: alarm | info | error"""
    if not _IS_WINDOWS:
        return False
    try:
        import winsound  # 标准库，Windows 独有，按需导入

        if style == "alarm":
            for freq, dur in ((880, 220), (660, 220), (880, 320)):
                winsound.Beep(freq, dur)
        elif style == "error":
            winsound.MessageBeep(winsound.MB_ICONHAND)
        else:
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        return True
    except Exception:
        return False


def flash_window(title: str = APP_TITLE) -> bool:
    """任务栏闪烁（后台静默运行时提醒用户）。"""
    if not _IS_WINDOWS:
        return False
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, title)
        if not hwnd:
            return False
        class FLASHWINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint),
                ("hwnd", ctypes.c_void_p),
                ("dwFlags", ctypes.c_uint),
                ("uCount", ctypes.c_uint),
                ("dwTimeout", ctypes.c_uint),
            ]

        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 0x00000003 | 0x0000000C, 5, 0)
        user32.FlashWindowEx(ctypes.byref(info))
        return True
    except Exception:
        return False


def alert(title: str, message: str, sound: bool = True, popup: bool = True, style: str = "alarm") -> None:
    """通用提醒：托盘/tk 弹窗由 GUI 监听器负责，这里负责系统层提醒。"""
    _emit("alert", title, message)

    def _worker() -> None:
        if sound:
            beep(style)
        if popup:
            if not toast(title, message):
                # 退化方案：Windows 自带消息框（MB_SYSTEMMODAL 保证置顶）
                try:
                    ctypes.windll.user32.MessageBoxW(None, message, title, 0x40 | 0x1000)
                except Exception:
                    pass

    threading.Thread(target=_worker, name="wjx-notify", daemon=True).start()


def info(title: str, message: str) -> None:
    _emit("info", title, message)
