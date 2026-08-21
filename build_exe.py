# -*- coding: utf-8 -*-
"""一键打包脚本（跨平台）。

用法：
    python build_exe.py            # 生成图标(若无) + 调 PyInstaller 打包
    python build_exe.py --clean    # 先清空 build/ 与 dist/ 再打包

产物：
    dist/folder_sync(.exe)  —— 单文件、无控制台（windowed）。

重要：
    - PyInstaller 不能跨平台交叉编译：在哪个系统上运行本脚本，就生成哪个系统
      的可执行文件。要得到 Windows .exe，需在 Windows 上运行本脚本。
    - Windows 7 目标机请使用 Python 3.8，并建议安装 PyInstaller 5.x：
          pip install "pyinstaller==5.13.2"
      （PyInstaller 官方仅保证 Windows 8+，6.x 不保证 Win7。）
"""

import os
import sys
import shutil
import subprocess
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))


def _ensure_icon():
    # type: () -> str
    ico = os.path.join(HERE, "app.ico")
    if not os.path.exists(ico):
        try:
            import make_icon
            make_icon.make_icon(ico)
        except ImportError:
            # 缺 Pillow 时 make_icon 惰性导入抛 ImportError，给出安装指引而非裸崩溃
            print("缺少 app.ico 且未安装 Pillow（生成占位图标需要）:")
            print("  方案一: pip install pillow  再重试本脚本")
            print("  方案二: 自行放置 app.ico 到项目根目录后重试")
            print("  （app.ico 用于 exe 图标与 Windows 托盘图标，缺失不影响打包逻辑）")
            sys.exit(1)
        except OSError as e:
            # make_icon 的 I/O 失败（如目标目录不可写）：同样给出指引而非裸崩溃
            print("生成 app.ico 失败: %s" % e)
            print("  请检查项目目录是否可写，或自行放置 app.ico 到项目根目录后重试")
            sys.exit(1)
    return ico


def _backup_old_artifact():
    # type: () -> Optional[str]
    """打包前备份旧产物（dist/folder_sync(.exe)），打包失败时恢复。

    PyInstaller 失败时 dist/ 可能残留半成品/覆盖可用版本：先改名备份，
    成功则删除备份，失败则改回，保证用户手里始终有一个可用的 exe。
    """
    name = "folder_sync.exe" if sys.platform == "win32" else "folder_sync"
    out = os.path.join(HERE, "dist", name)
    if not os.path.exists(out):
        return None
    bak = out + ".bak"
    try:
        if os.path.exists(bak):
            os.remove(bak)
        os.rename(out, bak)
        return bak
    except OSError as e:
        print("警告: 旧产物备份失败（%s），打包失败时可能无可用版本" % e)
        return None


def main():
    # type: () -> None
    if "--clean" in sys.argv[1:]:
        for d in ("build", "dist"):
            p = os.path.join(HERE, d)
            if os.path.isdir(p):
                shutil.rmtree(p)
                print("已清理 %s" % p)

    ico = _ensure_icon()
    print("图标: %s" % ico)

    bak = _backup_old_artifact()
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "folder_sync.spec"]
    print("执行: %s" % " ".join(cmd))
    rc = subprocess.call(cmd, cwd=HERE)
    if rc != 0:
        print("\n打包失败（退出码 %d）" % rc)
        # 失败回滚：恢复旧产物，避免用户拿到半成品/无可用版本
        if bak is not None:
            name = "folder_sync.exe" if sys.platform == "win32" else "folder_sync"
            out = os.path.join(HERE, "dist", name)
            try:
                if os.path.exists(out):
                    os.remove(out)  # 删除半成品
                os.rename(bak, out)
                print("已恢复旧产物: %s" % out)
            except OSError as e:
                print("警告: 旧产物恢复失败（%s），可在 %s 手动恢复" % (e, bak))
        sys.exit(rc)
    if bak is not None:
        try:
            os.remove(bak)  # 打包成功：清理备份
        except OSError:
            pass

    name = "folder_sync.exe" if sys.platform == "win32" else "folder_sync"
    out = os.path.join(HERE, "dist", name)
    print("\n打包完成: %s" % out)
    print("提示：")
    print("  - 当前产物为 %s 平台可执行文件；要得到 Windows exe 请在 Windows 上运行本脚本。" % sys.platform)
    print("  - config/ 与 logs/ 会生成在可执行文件同目录。")
    if sys.platform != "win32":
        print("  - Win7 目标机请用 Python 3.8 + PyInstaller 5.x（见文件头注释）。")


if __name__ == "__main__":
    main()
