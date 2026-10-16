"""一键打包成可执行文件 / 安装包（Windows）。

用法：
    python tools/build_exe.py              # onedir：dist/wjx-auto/（推荐分发）
    python tools/build_exe.py --setup      # 再打一个安装包（Inno Setup，setup 解包安装）
    python tools/build_exe.py --only-setup # 只打安装包（用已有的 dist/wjx-auto）
    python tools/build_exe.py --clean      # 先清空 build/ 与 dist/

产出：
    dist/wjx-auto/wjx-auto-gui.exe     双击即用（无控制台）
    dist/wjx-auto/wjx-auto.exe         命令行（selftest/probe/fill/autostart/daemon…）
    dist/wjx-auto/drivers/             浏览器驱动
    dist/wjx-auto/_internal/           Python 运行时与依赖
    dist/setup/wjx-auto-<版本>-setup.exe   安装包（--setup 时生成）

安装包特性：按用户安装（无需管理员）、可选桌面图标与开机自启、
卸载时保留 data/（任务与答案库不丢）。
"""

from __future__ import annotations

import argparse
import os
import re
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
ISS = ROOT / "packaging" / "wjx-auto.iss"
SETUP_OUT = ROOT / "dist" / "setup"


def app_version() -> str:
    """从 wjx/__init__.py 读版本号，供安装包命名与显示。"""
    try:
        text = (ROOT / "wjx" / "__init__.py").read_text(encoding="utf-8")
        m = re.search(r'__version__\s*=\s*["\']([^"\']+)', text)
        if m:
            return m.group(1)
    except OSError:
        pass
    return "0.0.0"


def find_iscc() -> str:
    """查找 Inno Setup 命令行编译器。"""
    candidates = [
        os.environ.get("ISCC", ""),
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe"),
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Inno Setup 6" / "ISCC.exe"),
        r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        r"C:\Program Files\Inno Setup 6\ISCC.exe",
    ]
    for path in candidates:
        if path and Path(path).exists():
            return path
    found = shutil.which("iscc") or shutil.which("ISCC")
    if found:
        return found
    # 兜底：在常见安装根目录里递归找
    for base in (Path(os.environ.get("LOCALAPPDATA", "")), Path("C:/Program Files (x86)"),
                 Path("C:/Program Files")):
        if not base.exists():
            continue
        try:
            for p in base.glob("**/ISCC.exe"):
                return str(p)
        except OSError:
            continue
    return ""


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


def build_installer(skip_build: bool = False) -> int:
    """用 Inno Setup 打包安装程序（setup 解包安装）。"""
    dist = ROOT / "dist" / "wjx-auto"
    if not skip_build and not (dist / "wjx-auto-gui.exe").exists():
        print("未找到 dist/wjx-auto，请先构建 onedir 版本")
        return 1
    if not (dist / "wjx-auto-gui.exe").exists():
        print(f"dist/wjx-auto 不完整（缺少 wjx-auto-gui.exe）：{dist}")
        return 1

    iscc = find_iscc()
    if not iscc:
        print("未找到 Inno Setup 编译器 ISCC.exe。\n"
              "安装方式（任选）：\n"
              "  winget install --id JRSoftware.InnoSetup --scope user\n"
              "  choco install innosetup -y\n"
              "安装后重新运行本脚本，或设置环境变量 ISCC 指向 ISCC.exe。")
        return 1

    SETUP_OUT.mkdir(parents=True, exist_ok=True)
    cmd = [iscc, f"/DMyAppVersion={app_version()}", str(ISS)]
    print("»", " ".join(str(c) for c in cmd), flush=True)
    code = subprocess.call([str(c) for c in cmd], cwd=str(ROOT / "packaging"))
    if code == 0:
        setups = sorted(SETUP_OUT.glob("*.exe"))
        for s in setups:
            print(f"安装包：{s}  ({s.stat().st_size / 1024 / 1024:.1f} MB)")
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description="打包 wjx-auto 为可执行文件/安装包")
    parser.add_argument("--onefile", action="store_true", help="额外构建单文件 exe")
    parser.add_argument("--only-onefile", action="store_true", help="只构建单文件 exe")
    parser.add_argument("--setup", action="store_true", help="构建 onedir 后再打安装包")
    parser.add_argument("--only-setup", action="store_true", help="只打安装包（跳过 exe 构建）")
    parser.add_argument("--clean", action="store_true", help="先清空 build/ 与 dist/")
    parser.add_argument("--skip-selftest", action="store_true", help="打包前不跑自测")
    parser.add_argument("--no-driver", action="store_true",
                        help="不内置浏览器驱动（体积小 ~23MB；用户需自行放入 drivers/）")
    args = parser.parse_args()

    started = time.time()
    if args.no_driver:
        # 通过环境变量告诉 common.py 跳过驱动收集
        os.environ["WJX_PACK_NO_DRIVER"] = "1"
    ensure_pyinstaller()
    if args.clean:
        clean()

    if args.only_setup:
        code = build_installer(skip_build=True)
        print(f"\n耗时 {time.time() - started:.0f} 秒")
        return code

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

    if args.setup:
        print("\n正在构建安装包 …")
        code = build_installer()
        if code != 0:
            return code

    print(f"耗时 {time.time() - started:.0f} 秒")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
