"""开机自启注册（全平台，纯标准库，零第三方依赖）。

平台机制：
  Windows: HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run 注册表键
           （winreg，仅当前用户，无需管理员权限；Win7 兼容）
  Linux:   ~/.config/autostart/folder-sync.desktop（XDG autostart 规范）
  macOS:   ~/Library/LaunchAgents/com.foldersync.plist（LaunchAgent，RunAtLoad）

自启命令行统一追加 ``--autostart`` 标志：main.py 据此以"后台运行"形态启动
（Windows 隐藏到托盘；非 Windows 最小化到任务栏），而不是弹主窗口。

所有函数均不抛异常：注册失败返回 False，由 GUI 提示；幂等可重复调用。
home 参数仅测试注入用（默认取真实用户目录）。
"""

import os
import sys
from typing import Any, Optional

from utils.paths import app_dir

APP_NAME = "FolderSync"
_REG_VALUE = "FolderSync"        # Windows 注册表值名
_LINUX_FILE = "folder-sync.desktop"
_MAC_LABEL = "com.foldersync"


def is_supported():
    # type: () -> bool
    """是否支持开机自启（Windows / Linux / macOS 均支持）。"""
    return sys.platform in ("win32", "linux", "darwin")


def _quote(path):
    # type: (str) -> str
    """路径含空格时加双引号包裹（仅用于注册表值写入 / .desktop 文件写入，
    不被 shell 解析，因此无需处理单引号、$、反斜杠等 shell 元字符；
    不要将此函数的输出拼入 shell 命令字符串，否则存在注入风险）。"""
    if " " in path and not path.startswith('"'):
        return '"%s"' % path
    return path


def build_command():
    # type: () -> str
    """生成自启命令行（含 --autostart）。

    frozen（PyInstaller 单文件 exe）：直接 exe 路径 + --autostart；
    源码运行：python 解释器 + main.py 绝对路径 + --autostart。
    """
    if getattr(sys, "frozen", False):
        return _quote(os.path.abspath(sys.executable)) + " --autostart"
    py = _quote(os.path.abspath(sys.executable))
    script = _quote(os.path.join(app_dir(), "main.py"))
    return "%s %s --autostart" % (py, script)


def _program_args():
    # type: () -> list
    """argv 列表形式（macOS LaunchAgent 的 ProgramArguments 用）。"""
    if getattr(sys, "frozen", False):
        return [os.path.abspath(sys.executable), "--autostart"]
    return [os.path.abspath(sys.executable),
            os.path.join(app_dir(), "main.py"), "--autostart"]


# ---------- Windows：注册表 ----------
def _winreg():
    # type: () -> Any
    """返回 winreg 模块（仅 Windows 存在）。返回 Any 以规避
    typeshed 平台差异：Linux 上 mypy 的 winreg stub 不暴露属性。"""
    import winreg
    return winreg


def _win_enable(cmd):
    # type: (str) -> bool
    try:
        winreg = _winreg()
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_SET_VALUE)
        try:
            winreg.SetValueEx(key, _REG_VALUE, 0, winreg.REG_SZ, cmd)
        finally:
            winreg.CloseKey(key)
        return True
    except Exception:
        return False


def _win_disable():
    # type: () -> bool
    try:
        winreg = _winreg()
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_SET_VALUE)
        try:
            try:
                winreg.DeleteValue(key, _REG_VALUE)
            except FileNotFoundError:
                pass  # 本来就没有，视为成功
        finally:
            winreg.CloseKey(key)
        return True
    except Exception:
        return False


def _win_is_enabled():
    # type: () -> bool
    try:
        winreg = _winreg()
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_QUERY_VALUE)
        try:
            winreg.QueryValueEx(key, _REG_VALUE)
            return True
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False


# ---------- Linux：XDG autostart .desktop ----------
def _linux_path(home):
    # type: (str) -> str
    return os.path.join(home, ".config", "autostart", _LINUX_FILE)


def _linux_enable(home, cmd):
    # type: (str, str) -> bool
    try:
        path = _linux_path(home)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # XDG 规范：Exec 键中的字面 % 是保留字符（字段代码 %f/%u 等），
        # 路径含 %（如 "100%prog"）必须转义为 %% 否则解析器误读
        content = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=文件夹同步备份工具\n"
            "Exec=%s\n"
            "Terminal=false\n"
            "X-GNOME-Autostart-enabled=true\n"
            "Hidden=false\n" % cmd.replace("%", "%%")
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except Exception:
        return False


def _linux_disable(home):
    # type: (str) -> bool
    try:
        path = _linux_path(home)
        if os.path.exists(path):
            os.remove(path)
        return True
    except Exception:
        return False


def _linux_is_enabled(home):
    # type: (str) -> bool
    return os.path.isfile(_linux_path(home))


# ---------- macOS：LaunchAgent plist ----------
def _mac_path(home):
    # type: (str) -> str
    return os.path.join(home, "Library", "LaunchAgents", _MAC_LABEL + ".plist")


def _mac_enable(home, args):
    # type: (str, list) -> bool
    try:
        import plistlib
        path = _mac_path(home)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {
            "Label": _MAC_LABEL,
            "ProgramArguments": args,
            "RunAtLoad": True,
        }
        with open(path, "wb") as f:
            plistlib.dump(data, f)
        return True
    except Exception:
        return False


def _mac_disable(home):
    # type: (str) -> bool
    try:
        path = _mac_path(home)
        if os.path.exists(path):
            os.remove(path)
        return True
    except Exception:
        return False


def _mac_is_enabled(home):
    # type: (str) -> bool
    return os.path.isfile(_mac_path(home))


# ---------- 对外 API ----------
def enable(home=None):
    # type: (Optional[str]) -> bool
    """注册开机自启。返回是否成功（不抛异常）。"""
    if home is None:
        home = os.path.expanduser("~")
    if sys.platform == "win32":
        return _win_enable(build_command())
    if sys.platform == "linux":
        return _linux_enable(home, build_command())
    if sys.platform == "darwin":
        return _mac_enable(home, _program_args())
    return False


def disable(home=None):
    # type: (Optional[str]) -> bool
    """取消开机自启。返回是否成功（不抛异常）。"""
    if home is None:
        home = os.path.expanduser("~")
    if sys.platform == "win32":
        return _win_disable()
    if sys.platform == "linux":
        return _linux_disable(home)
    if sys.platform == "darwin":
        return _mac_disable(home)
    return False


def is_enabled(home=None):
    # type: (Optional[str]) -> bool
    """当前是否已注册开机自启。"""
    if home is None:
        home = os.path.expanduser("~")
    if sys.platform == "win32":
        return _win_is_enabled()
    if sys.platform == "linux":
        return _linux_is_enabled(home)
    if sys.platform == "darwin":
        return _mac_is_enabled(home)
    return False
