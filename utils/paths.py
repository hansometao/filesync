"""路径工具：Windows 长路径（>260 字符）封装与跨平台兼容。

Windows 7 / 旧 Win32 API 受 MAX_PATH(260) 限制。对超过限制的路径加
双反斜杠问号前缀（``\\\\?\\\\``）即可绕过（需使用绝对路径）。非 Windows
平台直接原样返回。
"""

import os
import sys


def app_dir():
    """应用根目录：源码运行=项目根；PyInstaller 打包后=exe 所在目录。

    onefile 打包时 ``__file__`` 指向临时解压目录（进程退出即删），
    故 frozen 下改用 ``sys.executable`` 所在目录，让 config/logs 落在用户可见处。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def is_longpath_supported():
    """仅 Windows 需要处理长路径前缀。"""
    return sys.platform == "win32"


def longpath(path):
    """把路径转换为可绕过 MAX_PATH 限制的 Windows 扩展路径。

    非 Windows 平台返回原路径；已带 ``\\\\?\\`` 前缀则原样返回。
    """
    if not is_longpath_supported():
        return path
    if path is None:
        return path
    p = os.path.abspath(path)
    if p.startswith("\\\\?\\"):
        return p
    # UNC 路径：\\server\share -> \\?\UNC\server\share
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def ensure_dir(path):
    """确保目录存在（使用长路径前缀以兼容深层目录）。"""
    lp = longpath(path)
    if not os.path.isdir(lp):
        os.makedirs(lp, exist_ok=True)


def join_rel(root, rel):
    """把相对路径（'/' 分隔）拼接回系统路径。"""
    parts = rel.split("/")
    return os.path.join(root, *parts)
