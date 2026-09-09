# -*- coding: utf-8 -*-
"""应用元信息：版本号与应用名称的唯一来源。

GUI 标题栏、帮助文案、打包版本资源（version_info.txt）、
Inno Setup 安装包版本均取自此处，避免多处硬编码漂移。

发版流程：仅修改本文件 APP_VERSION，再运行 `python build.py`，
build.py 会据此自动重写 version_info.txt 并打入 exe 版本资源。
"""

# 应用展示名（GUI 标题、安装包名、版本资源 ProductName）
APP_NAME = "filesync"

# 应用版本号：x.y.z 三段（Windows 版本资源自动补齐为四段 x.y.z.0）。
# 原 main.APP_VERSION="1.1" 在此升级为 "1.1.0"，语义不变但满足
# Windows 版本资源的四段制要求（filevers/prodvers 需 4 个整数）。
APP_VERSION = "1.1.0"
