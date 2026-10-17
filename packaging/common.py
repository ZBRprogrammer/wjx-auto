"""打包配置公共部分（被两个 spec 共用）。

只收集**真正需要**的第三方包：
* selenium 及其运行依赖（urllib3 / certifi / trio / websocket-client / wsproto /
  attrs / outcome / sortedcontainers / sniffio / h11 / idna / cffi / pycparser / PySocks）
* 可选托盘支持（pystray / Pillow），装了才收
排除 numpy/pandas/matplotlib 等体积大又用不到的包，以及 selenium-manager 的
多平台二进制（本机已随包提供 drivers/msedgedriver.exe）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Tuple

DIST_NAME = "wjx-auto"

# selenium 运行所需的第三方包（缺一不可，否则打包后 import 失败）
SELENIUM_DEPS = [
    "selenium", "urllib3", "certifi", "idna", "attrs", "sortedcontainers",
    "outcome", "sniffio", "h11", "wsproto", "trio", "trio_websocket",
    "websocket", "cffi", "pycparser", "socks", "OpenSSL",
]
OPTIONAL_PKGS = ["pystray", "PIL"]

WJX_MODULES = [
    "wjx", "wjx.gui", "wjx.scheduler", "wjx.answers", "wjx.questionnaire",
    "wjx.autorun", "wjx.notify", "wjx.mockserver", "wjx.selftest",
    "wjx.drivers", "wjx.drivers.base", "wjx.drivers.mock",
    "wjx.drivers.selenium_driver", "wjx.logging_setup", "wjx.manualtest",
    "wjx.models", "wjx.config", "wjx.paths", "wjx.rules", "wjx.instance",
]
TK_MODULES = [
    "tkinter", "tkinter.ttk", "tkinter.messagebox", "tkinter.filedialog",
    "tkinter.font", "tkinter.constants",
]

EXCLUDES = [
    "numpy", "pandas", "matplotlib", "scipy", "openpyxl", "docx", "pptx",
    "lxml", "bs4",  # bs4 仅在纯 HTTP 解析时需要，缺失时程序会自动跳过
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
    "IPython", "jupyter", "pytest", "setuptools._distutils",
    "sqlite3", "pydoc_data", "lib2to3", "unittest",
]


def _vendor_dir(project: Path) -> Path:
    return project / "vendor"


def _module_in_vendor(project: Path, name: str) -> bool:
    """第三方包是否存在于 vendor/ 或当前解释器（决定要不要收集）。"""
    import importlib.util

    vendor = _vendor_dir(project)
    if vendor.is_dir():
        for candidate in (vendor / f"{name}.py", vendor / name):
            if candidate.exists():
                return True
    return importlib.util.find_spec(name) is not None


def vendor_datas(project: Path) -> List[Tuple[str, str]]:
    """把 vendor/ 下需要的包作为数据一起打包（放到 _internal/ 根，与顶层包名一致）。"""
    datas: List[Tuple[str, str]] = []
    vendor = _vendor_dir(project)
    if not vendor.is_dir():
        return datas
    wanted = {n.lower() for n in SELENIUM_DEPS + OPTIONAL_PKGS}
    for entry in sorted(vendor.iterdir()):
        low = entry.name.lower()
        if low.startswith((".", "__pycache__")):
            continue
        # selenium-manager 的多平台二进制体积很大且本机用不到（用 drivers/ 里的驱动）
        if low.startswith("selenium_manager") or "selenium-manager" in low:
            continue
        base = low.split("-")[0].split(".")[0]
        if base in wanted or low.endswith(".pth"):
            datas.append((str(entry), entry.name))
    return datas


def driver_datas(project: Path) -> List[Tuple[str, str]]:
    """随包提供浏览器驱动（用户可自行替换成与自己浏览器版本匹配的）。

    只收驱动可执行文件，跳过 zip 包与说明文件（省 10+ MB）。
    环境变量 ``WJX_PACK_NO_DRIVER=1`` 时不内置驱动（体积再小约 23MB，
    用户需自己把驱动放进 exe 同级 drivers\\ 目录）。
    """
    import os

    if os.environ.get("WJX_PACK_NO_DRIVER") == "1":
        print("[common] 按要求不内置浏览器驱动（--no-driver）")
        return []
    drivers = project / "drivers"
    if not drivers.is_dir():
        return []
    keep = [p for p in drivers.iterdir()
            if p.is_file() and p.name.lower().endswith(".exe")]
    if not keep:
        return []
    out_dir = project / "build" / "_drivers_dist"
    out_dir.mkdir(parents=True, exist_ok=True)
    for src in keep:
        dst = out_dir / src.name
        if not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
            import shutil

            shutil.copy2(src, dst)
    return [(str(out_dir), "drivers")]


def icon_datas(project: Path) -> List[Tuple[str, str]]:
    """把程序图标一起打包（窗口/托盘运行时读取 ``_internal/icon.ico``）。"""
    icon = project / "icon.ico"
    if not icon.exists():
        print("[common] 未找到 icon.ico，跳过图标打包")
        return []
    return [(str(icon), ".")]


def analysis_kwargs(project: Path) -> dict:
    """返回 Analysis(...) 的关键字参数（Analysis 类只在 spec 命名空间里可用）。"""
    datas = vendor_datas(project) + driver_datas(project) + icon_datas(project)
    hidden = build_hiddenimports(project)
    return dict(
        scripts=[str(project / "exe_entry.py")],
        pathex=[str(project)],
        binaries=[],
        datas=datas,
        hiddenimports=hidden,
        hookspath=[],
        runtime_hooks=[],
        excludes=EXCLUDES,
        noarchive=False,
    )


def build_hiddenimports(project: Path) -> List[str]:
    from importlib.util import find_spec

    hidden = list(WJX_MODULES) + list(TK_MODULES)
    for name in SELENIUM_DEPS + OPTIONAL_PKGS:
        if find_spec(name) is not None or _module_in_vendor(project, name):
            hidden.append(name)
    hidden += ["_tkinter", "tkinter._fix"]
    return sorted(set(hidden))


def collect_extras():
    """COLLECT 允许追加额外项（当前没有，保留扩展位）。"""
    return ()
