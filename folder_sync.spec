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

import sys

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    # 把 app.ico 作为数据文件打进 onefile 包：运行时解压到 _MEIPASS 临时目录，
    # tray.py 优先从那里加载托盘图标（exe 图标仅打包期生效，运行时还需文件）
    datas=[('app.ico', '.')] if __import__('os').path.exists('app.ico') else [],
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
    icon='app.ico' if sys.platform == 'win32' else None,
)
