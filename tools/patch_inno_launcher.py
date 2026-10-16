"""给 Inno Setup 的安装包外壳打上"要求管理员权限"的清单（Windows 会在最外层弹 UAC）。

为什么需要
----------
Inno Setup 生成的 setup.exe 其实是一个"自解压外壳"(SetupLdr) + 压缩负载：

    setup.exe 启动 → [可选] 申请提权 → 解包自身到 %TEMP%\\is-XXXX.tmp → 运行真正的安装程序

`PrivilegesRequired=admin` 只影响**内层**安装程序，外层的 SetupLdr 清单仍是
`asInvoker`。于是当 %TEMP% 被安全软件/受控文件夹访问拦住时，会先报
「安装程序无法创建目录 …\\Temp\\is-XXXX.tmp / 错误 5: 拒绝访问」——**连 UAC 都没弹**。

把外层 SetupLdr 的清单改成 `requireAdministrator` 后，Windows 会在**启动安装包的第一时间**
弹 UAC；用户确认后安装包全程以管理员权限运行，`%TEMP%` 写入不再受限。

做法（不改动 Inno Setup 自身的安装目录）
----------------------------------------
1. 从 Inno 安装目录复制 SetupLdr.e32 / SetupLdr.e64 到本项目的
   ``packaging/setupldr/``；
2. 在**副本**里把清单中的 ``level="asInvoker"`` 替换为
   ``level="requireAdministrator"``；
3. spec 里用 ``[Setup] SetupLdr=`` 指向这份副本（并关掉 Inno 对预编译文件的签名校验）。

清单中 ``level="asInvoker"`` 与 ``uiAccess="false"/>`` 之间有 12 个空格，
把整段一起替换即可做到**逐字节等长**，文件大小完全不变（Inno 会校验外壳大小，
长度一变编译出的安装包就无法运行）。

用法：
    python tools/patch_inno_launcher.py            # 生成/更新 packaging/setupldr 下的副本
    python tools/patch_inno_launcher.py --check     # 只显示状态
    python tools/patch_inno_launcher.py --restore    # 删掉副本（回到未打补丁状态）
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

BACKUP_SUFFIX = ".backup-wjx-auto"

# 逐字节等长替换：47 -> 47
OLD_REGION = (
    b'level="asInvoker"'
    b'            '          # 12 个空格（Inno 6.7.3 实测）
    b'uiAccess="false"/>'
)
NEW_REGION = (
    b'level="requireAdministrator"'
    b' '                     # 只留 1 个空格
    b'uiAccess="false"/>'
)
assert len(OLD_REGION) == len(NEW_REGION), "补丁区段必须等长"

PROJECT = Path(__file__).resolve().parent.parent
OUT_DIR = PROJECT / "packaging" / "setupldr"


def find_inno_dir() -> Path:
    """定位 Inno Setup 安装目录。"""
    candidates = [
        os.environ.get("INNO_DIR", ""),
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6"),
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Inno Setup 6"),
        r"C:\Program Files (x86)\Inno Setup 6",
        r"C:\Program Files\Inno Setup 6",
    ]
    for c in candidates:
        if c and (Path(c) / "ISCC.exe").exists():
            return Path(c)
    raise SystemExit("未找到 Inno Setup 安装目录，可用环境变量 INNO_DIR 指定")


def patch_inno_inplace(inno: Path) -> int:
    """就地给 Inno 安装目录里的 SetupLdr 打补丁（ISCC 编译时只会从这里读外壳）。

    * 先备份到 ``同名.backup-wjx-auto``（已存在则不覆盖）；
    * 逐字节等长替换，文件大小完全不变，因此编译出的安装包不会因尺寸校验失败；
    * 用 ``--restore`` 可完整还原。
    """
    changed = 0
    for name in ("SetupLdr.e32", "SetupLdr.e64"):
        target = inno / name
        if not target.exists():
            continue
        data = target.read_bytes()
        if b"requireAdministrator" in data:
            print(f"  {name}: 已是 requireAdministrator，跳过")
            continue
        idx = data.find(OLD_REGION)
        if idx < 0:
            print(f"  {name}: 未找到可替换的清单区段，跳过（Inno 版本可能不同）")
            continue
        patched = data[:idx] + NEW_REGION + data[idx + len(OLD_REGION):]
        if len(patched) != len(data):
            print(f"  {name}: 补丁后长度不一致（{len(patched)} != {len(data)}），已跳过")
            continue
        backup = target.with_name(name + BACKUP_SUFFIX)
        if not backup.exists():
            shutil.copy2(target, backup)
            print(f"  {name}: 已备份 -> {backup.name}")
        target.write_bytes(patched)
        print(f"  {name}: asInvoker → requireAdministrator（等长 {len(patched)} 字节）")
        changed += 1
    return changed


def patch_copy(src: Path, dst: Path, dry_run: bool = False) -> bool:
    """把 src 复制成已打补丁的 dst（不动 src）。

    兼容两种源文件状态：
      * 原始（asInvoker）→ 直接替换；
      * 已被早期脚本就地改过（requireAdministrator）→ 先按等长反向还原再替换。
    """
    if dst.exists() and b"requireAdministrator" in dst.read_bytes():
        print(f"  {dst.name}: 副本已存在且已打补丁，跳过")
        return True

    data = src.read_bytes()
    idx = data.find(OLD_REGION)
    if idx >= 0:
        patched = data[:idx] + NEW_REGION + data[idx + len(OLD_REGION):]
    else:
        # 源文件已经是补丁后的状态：反向还原再正向替换（等长操作）
        idx = data.find(NEW_REGION)
        if idx < 0:
            print(f"  {src.name}: 未找到可替换的清单区段，跳过（Inno 版本可能不同）")
            return False
        patched = data[:idx] + NEW_REGION + data[idx + len(NEW_REGION):]

    if len(patched) != len(data):
        print(f"  {src.name}: 补丁后长度不一致（{len(patched)} != {len(data)}），已跳过")
        return False

    if not dry_run:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(patched)
    print(f"  {src.name} -> {dst.relative_to(PROJECT)}"
          f"（level=requireAdministrator，等长 {len(patched)} 字节）")
    return True


def restore_inno_originals(inno: Path) -> int:
    """把 Inno 自身安装目录里的 SetupLdr 还原为 asInvoker（早期脚本曾就地改过）。"""
    restored = 0
    for name in ("SetupLdr.e32", "SetupLdr.e64"):
        target = inno / name
        if not target.exists():
            continue
        backup = target.with_name(name + BACKUP_SUFFIX)
        if backup.exists():
            if b"requireAdministrator" in target.read_bytes():
                shutil.copy2(backup, target)
                print(f"  {name}: 已从备份还原 Inno 自身文件")
                restored += 1
            backup.unlink()
            print(f"  {name}: 备份文件已清理")
            continue
        # 没有备份但文件已被改过 → 用等长反向替换还原
        data = target.read_bytes()
        idx = data.find(NEW_REGION)
        if idx >= 0:
            target.write_bytes(data[:idx] + OLD_REGION + data[idx + len(NEW_REGION):])
            print(f"  {name}: 已就地还原 Inno 自身文件（asInvoker）")
            restored += 1
    return restored


def main() -> int:
    parser = argparse.ArgumentParser(description="生成要求管理员权限的 Inno 安装包外壳副本")
    parser.add_argument("--check", action="store_true", help="只显示状态")
    parser.add_argument("--restore", action="store_true", help="删除副本（回到未打补丁状态）")
    parser.add_argument("--dry-run", action="store_true", help="只演示，不写文件")
    args = parser.parse_args()

    if args.restore:
        if OUT_DIR.exists():
            shutil.rmtree(OUT_DIR)
            print(f"已删除 {OUT_DIR.relative_to(PROJECT)}")
        else:
            print("没有需要删除的副本")
        return 0

    if args.check:
        if not OUT_DIR.exists():
            print(f"副本目录不存在：{OUT_DIR.relative_to(PROJECT)}（尚未打补丁）")
            return 1
        for f in sorted(OUT_DIR.iterdir()):
            data = f.read_bytes()
            state = "requireAdministrator ✔" if b"requireAdministrator" in data else "asInvoker ✘"
            print(f"  {f.name}: {state}（{len(data)} 字节）")
        return 0

    inno = find_inno_dir()
    print(f"Inno Setup 目录: {inno}")
    changed = patch_inno_inplace(inno)
    print(f"就地打补丁的文件数: {changed}")
    print("\n结果：")
    for name in ("SetupLdr.e32", "SetupLdr.e64"):
        target = inno / name
        if target.exists():
            state = "requireAdministrator ✔" if b"requireAdministrator" in target.read_bytes() else "asInvoker ✘"
            print(f"  {name}: {state}（{len(target.read_bytes())} 字节）")

    print("\n接下来用 ISCC 编译（build_exe.py --setup 会自动处理）：")
    print("  编译出的 setup.exe 外壳带 requireAdministrator，双击即弹 UAC（图标带盾牌）。")
    return 0


def restore_inno_originals(inno: Path) -> int:
    restored = 0
    for name in ("SetupLdr.e32", "SetupLdr.e64"):
        target = inno / name
        backup = target.with_name(name + BACKUP_SUFFIX)
        if not backup.exists():
            continue
        if b"requireAdministrator" in target.read_bytes():
            shutil.copy2(backup, target)
            print(f"  {name}: 已把 Inno 自身文件还原为原始版本")
            restored += 1
        backup.unlink()
        print(f"  {name}: 备份文件已清理")
    return restored


if __name__ == "__main__":
    raise SystemExit(main())
