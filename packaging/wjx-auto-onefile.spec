# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：单文件 exe（便携，双击即用；启动稍慢）。

    pyinstaller --clean --noconfirm packaging/wjx-auto-onefile.spec

产出 dist/wjx-auto-onefile.exe —— 单个文件即可分发；
首次运行会把内置资源解包到临时目录，data/ logs/ 仍在 exe 同级生成。
"""

import sys
from pathlib import Path

PROJECT = Path(SPECPATH).resolve().parent          # noqa: F821

sys.path.insert(0, str(PROJECT / "packaging"))
from common import analysis_kwargs   # noqa: E402

a = Analysis(**analysis_kwargs(PROJECT))            # noqa: F821
pyz = PYZ(a.pure)                                   # noqa: F821

exe = EXE(
    pyz, a.scripts, a.binaries, a.datas,
    [],
    name="wjx-auto-onefile",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,                 # 双击不弹控制台；需要命令行时用 onedir 版
    disable_windowed_traceback=False,
    icon=None,
)
