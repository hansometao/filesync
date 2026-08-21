# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：单文件（onefile）+ 无控制台（windowed）。

用法：
    pyinstaller folder_sync.spec        # 或：python build_exe.py

产物：
    dist/folder_sync(.exe)  —— 单文件、双击无黑窗（GUI）；
    命令行 --list / --sync / --help 在 windowed 下以弹窗展示结果。

说明：
  - console=False 即 windowed（Windows 下无控制台窗口，Linux 下无影响）。
  - 图标仅 Windows 生效（icon 参数），Linux 下忽略。
  - 可选依赖 xxhash 缺失时 PyInstaller 仅告警、不会失败（代码有 hashlib 回退）。
"""

import os
import sys

# spec 文件所在目录（SPECPATH 为 PyInstaller 注入的全局）：datas/icon 用
# 绝对路径，避免"直接 pyinstaller folder_sync.spec（其他目录）"时相对
# 路径解析失败（此前 pathex=[] 且 app.ico 依赖 cwd，仅 build_exe 的
# cwd=HERE 掩盖了该假设）
SPEC_DIR = os.path.dirname(os.path.abspath(SPECPATH))
ICO_PATH = os.path.join(SPEC_DIR, "app.ico")

block_cipher = None

a = Analysis(
    [os.path.join(SPEC_DIR, 'main.py')],
    pathex=[SPEC_DIR],
    binaries=[],
    # 把 app.ico 作为数据文件打进 onefile 包：运行时解压到 _MEIPASS 临时目录，
    # tray.py 优先从那里加载托盘图标（exe 图标仅打包期生效，运行时还需文件）
    datas=[(ICO_PATH, '.')] if os.path.exists(ICO_PATH) else [],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='folder_sync',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    icon=ICO_PATH if sys.platform == 'win32' else None,
)
