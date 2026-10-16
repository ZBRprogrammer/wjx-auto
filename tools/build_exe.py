"""一键打包成可执行文件（Windows）。

用法：
    python tools/build_exe.py              # onedir：dist/wjx-auto/（推荐分发）
    python tools/build_exe.py --onefile    # 额外生成 dist/wjx-auto-onefile.exe（单文件）
    python tools/build_exe.py --clean      # 先清空 build/ 与 dist/

产出（onedir）：
    dist/wjx-auto/wjx-auto-gui.exe    双击即用（无控制台）
    dist/wjx-auto/wjx-auto.exe        命令行（selftest/probe/fill/autostart/daemon…）
    dist/wjx-auto/drivers/            浏览器驱动
    dist/wjx-auto/_internal/          Python 运行时与依赖
运行后会在 exe 同级生成 data/ logs/ run/（配置、答案库、日志都在这里）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPECS = {
    "both": ROOT / "packaging" / "wjx-auto.spec",
    "onefile": ROOT / "packaging" / "wjx-auto-onefile.spec",
}


def run(cmd: list, **kw) -> int:
    print("»", " ".join(str(c) for c in cmd), flush=True)
    env = os.environ.copy()
    # PyInstaller 默认缓存目录可能在受限环境不可写，统一放到项目内
    cache = ROOT / ".pyinstaller-cache"
    cache.mkdir(exist_ok=True)
    env["PYINSTALLER_CONFIG_DIR"] = str(cache)
    env.setdefault("TEMP", str(ROOT / ".piptmp"))
    env.setdefault("TMP", str(ROOT / ".piptmp"))
    env.setdefault("PYTHONUTF8", "1")
    return subprocess.call([str(c) for c in cmd], cwd=str(ROOT), env=env, **kw)


def ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401

        return
    except ImportError:
        pass
    print("未检测到 PyInstaller，正在安装 …")
    tmp = ROOT / ".piptmp"
    tmp.mkdir(exist_ok=True)
    os.environ["TEMP"] = str(tmp)
    os.environ["TMP"] = str(tmp)
    if run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
            "--no-input", "pyinstaller"]) != 0:
        raise SystemExit("PyInstaller 安装失败")


def clean() -> None:
    for name in ("build", "dist"):
        path = ROOT / name
        if path.exists():
            print(f"清理 {path}")
            shutil.rmtree(path, ignore_errors=True)


def build(spec: Path) -> int:
    return run([sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
                "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build"),
                str(spec)])


def report() -> None:
    dist = ROOT / "dist" / "wjx-auto"
    print("\n" + "=" * 68)
    if not dist.exists():
        print("未找到 dist/wjx-auto，请检查上面的构建输出")
        return
    # 随包附带使用说明（PyInstaller 每次会重建该目录，说明文件在这里补回）
    readme_src = ROOT / "packaging" / "使用说明.txt"
    if readme_src.exists():
        shutil.copy2(readme_src, dist / "使用说明.txt")
    total = sum(f.stat().st_size for f in dist.rglob("*") if f.is_file())
    print(f"构建完成：{dist}")
    print(f"目录大小：{total / 1024 / 1024:.1f} MB")
    for exe in sorted(dist.glob("*.exe")):
        print(f"  {exe.name:22} {exe.stat().st_size / 1024 / 1024:6.1f} MB")
    for exe in sorted((ROOT / "dist").glob("*-onefile.exe")):
        print(f"  {exe.name:22} {exe.stat().st_size / 1024 / 1024:6.1f} MB  (单文件)")
    drivers = dist / "drivers"
    if drivers.is_dir():
        print(f"  内置驱动：{', '.join(p.name for p in drivers.iterdir())}")
    print("\n分发方式：整个 dist/wjx-auto 文件夹拷给对方，双击 wjx-auto-gui.exe 即可。")
    print("命令行用法：wjx-auto.exe selftest | probe --url ... | autostart --url ...")
    print("=" * 68)


def main() -> int:
    parser = argparse.ArgumentParser(description="打包 wjx-auto 为可执行文件")
    parser.add_argument("--onefile", action="store_true", help="额外构建单文件 exe")
    parser.add_argument("--only-onefile", action="store_true", help="只构建单文件 exe")
    parser.add_argument("--clean", action="store_true", help="先清空 build/ 与 dist/")
    parser.add_argument("--skip-selftest", action="store_true", help="打包前不跑自测")
    parser.add_argument("--no-driver", action="store_true",
                        help="不内置浏览器驱动（体积小 ~23MB；用户需自行放入 drivers/）")
    args = parser.parse_args()

    started = time.time()
    if args.no_driver:
        # 通过环境变量告诉 common.py 跳过驱动收集（放在 dist 里由用户自行提供）
        os.environ["WJX_PACK_NO_DRIVER"] = "1"
    ensure_pyinstaller()
    if args.clean:
        clean()

    if not args.skip_selftest:
        print("打包前先跑一次自测（源码状态）…")
        if run([sys.executable, str(ROOT / "main.py"), "selftest"],
               stdout=subprocess.DEVNULL) != 0:
            print("自测未全部通过，仍继续打包（可加 --skip-selftest 跳过此检查）")

    if not args.only_onefile:
        code = build(SPECS["both"])
        if code != 0:
            print(f"构建失败（退出码 {code}）")
            return code

    if args.onefile or args.only_onefile:
        code = build(SPECS["onefile"])
        if code != 0:
            print(f"单文件构建失败（退出码 {code}）")
            return code

    report()
    print(f"耗时 {time.time() - started:.0f} 秒")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
