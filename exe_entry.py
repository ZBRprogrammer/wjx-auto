"""打包入口（PyInstaller 用）。

双击 exe 时等价于 ``main.py gui``：打开图形界面，并自动拉起模拟问卷服务。
命令行传参时等价于 ``main.py <子命令>``，例如::

    wjx-auto.exe selftest
    wjx-auto.exe probe --url https://v.wjx.cn/vm/xxxx.aspx
    wjx-auto.exe fill  --url https://v.wjx.cn/vm/xxxx.aspx
    wjx-auto.exe autostart --url https://v.wjx.cn/vm/xxxx.aspx --name 我的问卷
"""

from __future__ import annotations

import multiprocessing
import os
import sys
from pathlib import Path


def _bootstrap() -> None:
    """冻结环境下把可写数据目录切到 exe 旁边，并确保 vendor/ 可导入。"""
    # 代码目录（源码运行时=项目根；打包后=_MEIPASS）
    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))

    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        for extra in (exe_dir, exe_dir / "vendor"):
            if extra.is_dir() and str(extra) not in sys.path:
                sys.path.append(str(extra))
        # 保证 exe 旁的数据目录存在
        for name in ("data", "logs", "run"):
            try:
                (exe_dir / name).mkdir(parents=True, exist_ok=True)
            except OSError:
                pass


def main() -> int:
    _bootstrap()
    from main import main as cli_main

    argv = sys.argv[1:]
    if not argv:
        # 双击运行：进入图形界面
        argv = ["gui"]
    return cli_main(argv)


if __name__ == "__main__":
    multiprocessing.freeze_support()   # 打包后避免多进程重复启动
    raise SystemExit(main())
