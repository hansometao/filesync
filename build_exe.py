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

HERE = os.path.dirname(os.path.abspath(__file__))


def _ensure_icon():
    # type: () -> str
    ico = os.path.join(HERE, "app.ico")
    if not os.path.exists(ico):
        import make_icon
        make_icon.make_icon(ico)
    return ico


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

    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "folder_sync.spec"]
    print("执行: %s" % " ".join(cmd))
    rc = subprocess.call(cmd, cwd=HERE)
    if rc != 0:
        print("\n打包失败（退出码 %d）" % rc)
        sys.exit(rc)

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
