"""对齐安装包的"候选目录可写性探测"逻辑，先在本机跑一遍（开发用，不入库）。

安装包里的 DirLooksWritable / CandidateDir 是 Pascal 版本，
这里用 Python 复刻同样的判定，用来确认策略顺序是合理、且能给出可写目录。
"""

import os
import shutil
from pathlib import Path

APP_DIR = "wjx-auto"


def candidates() -> list:
    local = os.environ.get("LOCALAPPDATA", "")
    roaming = os.environ.get("APPDATA", "")
    userdocs = str(Path.home() / "Documents")
    tmp = os.environ.get("TEMP", "")
    return [
        Path(local) / APP_DIR,
        Path(local) / "Programs" / APP_DIR,
        Path(roaming) / APP_DIR,
        Path(userdocs) / APP_DIR,
        Path(tmp) / APP_DIR,
    ]


def dir_looks_writable(target: Path) -> tuple:
    """复刻安装包的判定：把路径逐层建出来 + 写临时子目录，最后清理掉自建层。"""
    missing = []
    probe = target
    guard = 0
    while probe != probe.parent and not probe.exists() and guard < 64:
        missing.append(probe)
        probe = probe.parent
        guard += 1
    if not probe.exists():
        return False, "找不到已存在的祖先目录"

    created = []
    try:
        for d in reversed(missing):
            try:
                d.mkdir()
                created.append(d)
            except OSError as exc:
                return False, f"无法创建 {d}：{exc.strerror or exc}"
        sub = target / ".wjx_write_test"
        try:
            if sub.exists():
                shutil.rmtree(sub, ignore_errors=True)
            sub.mkdir()
            sub.rmdir()
        except OSError as exc:
            return False, f"无法在 {target} 写入：{exc.strerror or exc}"
        return True, "可写"
    finally:
        for d in created:
            try:
                d.rmdir()
            except OSError:
                pass


def main() -> int:
    print("=" * 72)
    print("安装候选目录可写性探测（与安装包里的判定逻辑一致）")
    print("=" * 72)
    best = None
    for i, path in enumerate(candidates()):
        ok, why = dir_looks_writable(path)
        mark = "✔" if ok else "✘"
        print(f"  [{i}] {mark} {path}\n        {why}")
        if ok and best is None:
            best = path
    print("-" * 72)
    if best:
        print(f"安装包会选用：{best}")
        return 0
    print("所有候选目录都不可写 —— 需要检查该环境的用户目录权限/策略")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
