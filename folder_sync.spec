# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：单文件（onefile）+ 无控制台（windowed）。

用法：
    pyinstaller folder_sync.spec        # 或：python build.py

产物：
    dist/folder_sync(.exe)  —— 单文件、双击无黑窗（GUI）；
    命令行 --list / --sync / --help 在 windowed 下以弹窗展示结果。

说明：
  - console=False 即 windowed（Windows 下无控制台窗口，Linux 下无影响）。
  - 图标仅 Windows 生效（icon 参数），Linux 下忽略。
  - 可选依赖 xxhash 缺失时 PyInstaller 仅告警、不会失败（代码有 hashlib 回退）。
  - version=version_info.txt 挂 Windows 版本资源（由 build.py 依据
    core/meta.py 的 APP_VERSION 自动重写，勿手改）。
"""

import os
import sys

# spec 文件所在目录（SPECPATH 为 PyInstaller 注入的全局）：datas/icon/version
# 用绝对路径，避免"直接 pyinstaller folder_sync.spec（其他目录）"时相对路径
# 解析失败。注意 PyInstaller 5.x 的 SPECPATH 是 spec 所在**目录**而非 spec
# 文件路径——再套 dirname 会错算到上级目录（曾致 main.py 找不到）；
# 兼容旧语义（spec 文件路径）做双分支
_SP = os.path.abspath(SPECPATH)
SPEC_DIR = _SP if os.path.isdir(_SP) else os.path.dirname(_SP)
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
    # Windows 版本资源：由 build.py 依据 core/meta.py 的 APP_VERSION 自动重写。
    # version 参数在非 Windows 平台同样被 PyInstaller 接受（Linux 产物无影响）。
    version=os.path.join(SPEC_DIR, 'version_info.txt'),
)
