"""一键准备真实浏览器后端：安装 selenium + 下载匹配的浏览器驱动。

用法：
    python tools/setup_browser.py            # 默认 Edge
    python tools/setup_browser.py --chrome

会在项目内创建：
    vendor/           selenium 及其依赖
    drivers/          msedgedriver.exe / chromedriver.exe（与本机浏览器版本匹配）
"""

from __future__ import annotations

import argparse
import io
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BROWSER_PATHS = {
    "edge": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ],
    "chrome": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ],
}
DRIVER_NAMES = {"edge": "msedgedriver.exe", "chrome": "chromedriver.exe"}


def find_browser(browser: str) -> tuple[str, str]:
    for path in BROWSER_PATHS[browser]:
        if os.path.exists(path):
            version = _file_version(path)
            return path, version
    raise SystemExit(f"未找到 {browser} 浏览器，请先安装或改用另一个浏览器")


def _file_version(path: str) -> str:
    """读取 exe 的产品版本（优先 PowerShell，回退到文件名匹配）。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Item '{path}').VersionInfo.ProductVersion"],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        version = (out.stdout or "").strip()
        if re.match(r"^\d+(\.\d+)+", version):
            return version
    except Exception:
        pass
    return ""


def install_selenium() -> None:
    vendor = ROOT / "vendor"
    vendor.mkdir(exist_ok=True)
    if (vendor / "selenium").is_dir():
        print("selenium 已安装（vendor/），跳过")
        return
    tmp = ROOT / ".piptmp"
    tmp.mkdir(exist_ok=True)
    os.environ["TEMP"] = str(tmp)
    os.environ["TMP"] = str(tmp)
    print("正在安装 selenium 到 vendor/ …")
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
         "--no-input", "--target", str(vendor), "selenium"],
        text=True,
    )
    if proc.returncode != 0:
        raise SystemExit("selenium 安装失败，请检查网络或代理")


def download_driver(browser: str, version: str) -> Path:
    drivers = ROOT / "drivers"
    drivers.mkdir(exist_ok=True)
    target = drivers / DRIVER_NAMES[browser]
    if target.exists():
        print(f"驱动已存在：{target}")
        return target

    build = ".".join(version.split(".")[:4]) or version
    if browser == "edge":
        urls = [
            f"https://msedgedriver.microsoft.com/{build}/edgedriver_win64.zip",
            f"https://msedgedriver.azureedge.net/{build}/edgedriver_win64.zip",
        ]
    else:
        urls = [
            f"https://storage.googleapis.com/chrome-for-testing-public/{build}/win64/chromedriver-win64.zip",
            f"https://npmmirror.com/mirrors/chrome-for-testing/{build}/win64/chromedriver-win64.zip",
        ]

    last_error = ""
    for url in urls:
        try:
            print(f"下载驱动：{url}")
            with urllib.request.urlopen(url, timeout=180) as resp:
                blob = resp.read()
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                for name in zf.namelist():
                    if name.lower().endswith(DRIVER_NAMES[browser].lower()):
                        with zf.open(name) as src, open(target, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        print(f"驱动已就绪：{target}")
                        return target
            last_error = f"{url} 中没有找到 {DRIVER_NAMES[browser]}"
        except Exception as exc:
            last_error = f"{url} -> {type(exc).__name__}: {exc}"
            continue
    raise SystemExit(
        "驱动下载失败，请手动下载后放到 drivers/ 目录。\n"
        f"  最后错误：{last_error}\n"
        "  Edge:   https://developer.microsoft.com/microsoft-edge/tools/webdriver/\n"
        "  Chrome: https://googlechromelabs.github.io/chrome-for-testing/"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="准备真实浏览器后端")
    parser.add_argument("--browser", default="edge", choices=["edge", "chrome"])
    args = parser.parse_args()

    path, version = find_browser(args.browser)
    print(f"{args.browser}: {path}\n版本: {version or '未知'}")
    install_selenium()
    download_driver(args.browser, version)
    print("\n完成。现在可以运行：")
    print('  python main.py resolve --url "https://v.wjx.cn/vm/你的问卷.aspx"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
