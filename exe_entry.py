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


def _apply_startup_flags(argv: list) -> list:
    """兼容安装包写入的启动参数。

    * ``--minimized``（开机自启用）：GUI 启动后直接最小化到托盘，不打扰用户；
    * 其余参数原样透传。
    """
    if "--minimized" not in argv:
        return argv
    argv = [a for a in argv if a != "--minimized"]
    if not argv:
        argv = ["gui"]
    if argv and argv[0] == "gui" and "--minimized" not in argv:
        argv.append("--minimized")
    return argv


def main() -> int:
    _bootstrap()
    from main import main as cli_main

    argv = _apply_startup_flags(sys.argv[1:])
    if not argv:
        # 双击运行：进入图形界面
        argv = ["gui"]
    return cli_main(argv)


if __name__ == "__main__":
    multiprocessing.freeze_support()   # 打包后避免多进程重复启动
    raise SystemExit(main())