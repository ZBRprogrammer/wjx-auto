# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：一次构建出**两个** exe，共用同一分发目录。

    pyinstaller --clean --noconfirm packaging/wjx-auto.spec

产出 dist/wjx-auto/：
    wjx-auto-gui.exe   图形界面，双击即用（无控制台窗口）
    wjx-auto.exe       命令行版（selftest / probe / fill / autostart / daemon …）
    drivers/           浏览器驱动
    _internal/         Python 运行时与依赖

两个 exe 共用同一份 binaries/datas（Analysis 只做一次），因此体积几乎不翻倍。
"""

import sys
from pathlib import Path

PROJECT = Path(SPECPATH).resolve().parent          # noqa: F821

sys.path.insert(0, str(PROJECT / "packaging"))
from common import DIST_NAME, analysis_kwargs      # noqa: E402

a = Analysis(**analysis_kwargs(PROJECT))            # noqa: F821
pyz = PYZ(a.pure)                                   # noqa: F821

# 程序图标：exe 内嵌图标 + 运行时窗口/托盘图标（随 datas 打进 _internal/）
ICON = str(PROJECT / "icon.ico")                    # noqa: F821

gui_exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="wjx-auto-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                 # 双击不弹黑窗
    disable_windowed_traceback=False,
    icon=ICON,
)

cli_exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="wjx-auto",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,                  # 命令行版保留控制台
    disable_windowed_traceback=False,
    icon=ICON,
)

coll = COLLECT(                                     # noqa: F821
    gui_exe, cli_exe, a.binaries, a.datas,
    strip=False, upx=False, name=DIST_NAME,
)
