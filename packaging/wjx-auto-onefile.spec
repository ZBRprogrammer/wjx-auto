# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：单文件便携版（一次产出两个）。

    pyinstaller --clean --noconfirm packaging/wjx-auto-onefile.spec

产出：
    dist/wjx-auto-onefile.exe      双击即用（无控制台窗口）
    dist/wjx-auto-cli-onefile.exe  命令行版（有控制台，可跑 selftest/probe/fill/…）

单文件版每次运行会把资源解包到临时目录，启动比 onedir 慢一些；
如果所在环境的系统临时目录不可写，请改用 onedir 版。
"""

import sys
from pathlib import Path

PROJECT = Path(SPECPATH).resolve().parent          # noqa: F821

sys.path.insert(0, str(PROJECT / "packaging"))
from common import analysis_kwargs   # noqa: E402

a = Analysis(**analysis_kwargs(PROJECT))            # noqa: F821
pyz = PYZ(a.pure)                                   # noqa: F821

gui_exe = EXE(
    pyz, a.scripts, a.binaries, a.datas,
    [],
    name="wjx-auto-onefile",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,                 # 双击不弹控制台
    disable_windowed_traceback=False,
    icon=None,
)

cli_exe = EXE(
    pyz, a.scripts, a.binaries, a.datas,
    [],
    name="wjx-auto-cli-onefile",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,                  # 命令行版
    disable_windowed_traceback=False,
    icon=None,
)
