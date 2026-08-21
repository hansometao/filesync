"""Windows 系统托盘图标（纯 ctypes 实现，零第三方依赖）。

仅在 Windows 上可用（Win7 兼容）：创建一个消息专用隐藏窗口接收托盘回调，
左键单击触发激活回调，右键弹出菜单（显示主窗口 / 退出）。

非 Windows 平台：模块可正常导入，`is_supported()` 返回 False，
`TrayIcon` 构造时抛 OSError——GUI 层据此优雅降级（不创建托盘）。

线程模型：托盘回调由 Tk 主循环派发（Tk 在 Windows 上会 DispatchMessage
本线程全部消息，包括我们 ctypes 创建的窗口）。由于 ctypes 窗口回调
与 Tk 主循环运行在同一线程，回调内**可直接调用 tkinter API**，无需
root.after 调度；这与"worker 线程不得直接调 tkinter"的约束不冲突
（worker 是跨线程，而托盘回调就是主线程的消息泵）。
"""

import ctypes
import os
import sys
from ctypes import wintypes
from typing import Any, Callable, List, Optional, Tuple

from utils.paths import app_dir
from logger import get_logger

# ---- Win32 常量（自给自足，避免依赖 win32con） ----
WM_APP = 0x8000
# 托盘回调 lParam 语义（shellapi.h）：
#   uVersion=0 或 NOTIFYICON_VERSION(3)：lParam 是原始鼠标/键盘消息（如 WM_LBUTTONUP）；
#   uVersion=NOTIFYICON_VERSION_4：LOWORD(lParam) 是 NIN_* 通知事件。
# 本实现用 V3（lParam=WM_LBUTTONUP 等），但保留 V4 的 NIN_SELECT 兜底，
# 未来若升级版本号无需改动匹配逻辑。以下为 SDK 真实定义（WM_USER=0x0400）：
NIN_SELECT = 0x0400           # WM_USER+0：单击选中图标（V4 模式左键）
NIN_KEYSELECT = 0x0401        # WM_USER+1：键盘选择（V4 模式）
NIN_BALLOONSHOW = 0x0402      # WM_USER+2
NIN_BALLOONHIDE = 0x0403      # WM_USER+3
NIN_BALLOONTIMEOUT = 0x0404   # WM_USER+4
NIN_BALLOONUSERCLICK = 0x0405 # WM_USER+5
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIM_SETVERSION = 0x00000004
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NOTIFYICON_VERSION = 0x00000003

MF_STRING = 0x00000000
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
TPM_NONOTIFY = 0x0080
IDI_APPLICATION = 32512
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040
HWND_MESSAGE = -3

class NOTIFYICONDATAW(ctypes.Structure):
    """NOTIFYICONDATAW（V2 布局，覆盖 Win7 所需的全部字段）。"""

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uTimeout", wintypes.DWORD),  # union：uTimeout / uVersion
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", wintypes.DWORD),
    ]


# WNDPROC / WNDCLASSW 依赖 Windows 专有的 ctypes.WINFUNCTYPE（stdcall 回调），
# 非 Windows 平台不存在该 API——因此惰性定义在 _create() 中（仅 Win32 执行），
# 保证本模块在任意平台可导入（非 Windows 仅 is_supported() 返回 False）。


def is_supported():
    # type: () -> bool
    """托盘图标仅 Windows 可用。"""
    return sys.platform == "win32"


class TrayIcon(object):
    """系统托盘图标。

    menu：[(item_id, 菜单文本), ...]，右键弹出；选择后回调 on_menu(item_id)。
    on_activate：左键单击托盘图标时回调（无参）。
    icon_path：ico 文件路径；缺省取应用目录下的 app.ico，不存在则用系统默认图标。
    """

    _CLASS_NAME = "FolderSyncTrayWnd"

    def __init__(self, title, menu, on_menu, on_activate=None, icon_path=None, main_hwnd=None):
        # type: (str, List[Tuple[int, str]], Callable[[int], None], Optional[Callable[[], None]], Optional[str], Optional[int]) -> None
        if not is_supported():
            raise OSError("系统托盘图标仅支持 Windows")
        self.logger = get_logger()  # 版本协商失败等内部告警落日志
        self._title = title
        self._menu = menu
        self._on_menu = on_menu
        self._on_activate = on_activate
        self._main_hwnd = main_hwnd  # type: Optional[int]  # 主窗口句柄，菜单关闭后还焦点
        self._window = None          # type: Optional[Any]
        self._icon = None            # type: Optional[Any]
        self._icon_owned = False     # 图标是否自有（LoadImageW 文件加载，可 DestroyIcon）
        self._callback_ref = None    # type: Optional[Any]  # 防止 WNDPROC 被 GC
        self._nid = None             # type: Optional[NOTIFYICONDATAW]
        self._create(title, icon_path)

    # ---------- 生命周期 ----------
    def _create(self, title, icon_path):
        # type: (str, Optional[str]) -> None
        user32 = self._user32()
        kernel32 = getattr(ctypes, "windll").kernel32
        shell32 = getattr(ctypes, "windll").shell32
        self._configure_api(user32, shell32, kernel32)

        # WNDPROC/WNDCLASSW 依赖 Windows 专有的 WINFUNCTYPE，此处惰性定义
        WNDPROC = ctypes.WINFUNCTYPE(  # type: ignore[attr-defined]  # typeshed 仅 Windows 暴露
            ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
            wintypes.WPARAM, wintypes.LPARAM)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        # 窗口类 + 消息专用隐藏窗口（HWND_MESSAGE：不进任务栏、不可见）
        self._callback_ref = WNDPROC(self._wnd_proc)
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._callback_ref
        wc.hInstance = hinst
        wc.lpszClassName = self._CLASS_NAME
        if not user32.RegisterClassW(ctypes.byref(wc)):
            raise ctypes.WinError()  # type: ignore[attr-defined]  # typeshed 仅 Windows 暴露

        try:
            hwnd = user32.CreateWindowExW(
                0, self._CLASS_NAME, title, 0, 0, 0, 0, 0,
                HWND_MESSAGE, None, hinst, None)
            if not hwnd:
                raise ctypes.WinError()  # type: ignore[attr-defined]
            self._window = hwnd

            # 图标：优先 app.ico，缺失回退系统默认图标。
            # 返回 (句柄, 是否自有)：自有图标可 DestroyIcon，共享图标禁止
            self._icon, self._icon_owned = self._load_icon(user32, icon_path)

            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = hwnd
            nid.uID = 1
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_APP + 1
            nid.hIcon = self._icon
            nid.szTip = title[:127]
            self._nid = nid

            if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
                raise ctypes.WinError()  # type: ignore[attr-defined]
            # 协商版本：请求 V3 让托盘交互行为与 Win7+ 一致（V3 下左键单击
            # 以 WM_LBUTTONUP 原始鼠标消息送达回调，而非 NIN_SELECT 通知——
            # NIN_SELECT 仅 V4 键盘选择/低版本命中，保留处理分支作为防御）。
            # 若协商失败（uVersion 保持 0 = 旧行为），交互仍可用，仅记录日志。
            nid.uTimeout = NOTIFYICON_VERSION
            if not shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(nid)):
                self.logger.warn("托盘版本协商失败，沿用默认交互行为")
        except Exception:
            # 失败路径清理：窗口/图标/窗口类全部回收，避免残留
            self._cleanup_after_failed_create(user32, kernel32, hinst)
            raise

    def _configure_api(self, user32, shell32, kernel32):
        # type: (Any, Any, Any) -> None
        """为句柄/返回值类 Win32 API 设置 restype，避免 64 位进程上句柄截断。

        ctypes 默认 restype 是 32 位 int：HWND/HICON/HMODULE 等指针在 64 位
        下被截断成低 32 位（经典陷阱），必须显式声明（仅在 _create 内调用，
        即仅 Win32 运行时执行）。
        """
        # 句柄返回类
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.LoadImageW.restype = wintypes.HICON
        user32.LoadIconW.restype = wintypes.HICON
        user32.SetActiveWindow.restype = wintypes.HWND
        user32.CreatePopupMenu.restype = wintypes.HMENU
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD  # 属于 kernel32，不在 user32
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        # 返回值/布尔/原子类
        user32.DefWindowProcW.restype = ctypes.c_ssize_t   # LRESULT
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.TrackPopupMenu.restype = wintypes.UINT
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.AttachThreadInput.restype = wintypes.BOOL
        user32.GetCursorPos.restype = wintypes.BOOL
        user32.DestroyWindow.restype = wintypes.BOOL
        user32.DestroyIcon.restype = wintypes.BOOL
        user32.DestroyMenu.restype = wintypes.BOOL
        user32.AppendMenuW.restype = wintypes.BOOL
        user32.UnregisterClassW.restype = wintypes.BOOL
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL

    def _cleanup_after_failed_create(self, user32, kernel32, hinst):
        # type: (Any, Any, Any) -> None
        """_create 失败路径的资源回收（幂等）。"""
        self._nid = None   # NIM_ADD 未成功，无需 NIM_DELETE
        if self._window:
            try:
                user32.DestroyWindow(self._window)
            except Exception:
                pass
            self._window = None
        if self._icon is not None:
            # 仅销毁自有图标；系统共享图标（IDI_APPLICATION）禁止 DestroyIcon
            if self._icon_owned:
                try:
                    user32.DestroyIcon(self._icon)
                except Exception:
                    pass
            self._icon = None
            self._icon_owned = False
        try:
            user32.UnregisterClassW(self._CLASS_NAME, hinst)
        except Exception:
            pass
        self._callback_ref = None

    def _load_icon(self, user32, icon_path):
        # type: (Any, Optional[str]) -> Tuple[Any, bool]
        """加载图标，返回 (句柄, 是否自有)。

        自有图标（LoadImageW 从文件加载）由调用方负责 DestroyIcon；
        系统共享图标（LoadIconW 的 IDI_APPLICATION）**禁止** DestroyIcon
        （MSDN：共享图标由系统管理，销毁是未定义行为）。
        """
        if icon_path is None:
            icon_path = os.path.join(app_dir(), "app.ico")
        # onefile 打包后 app.ico 经 spec 的 datas 收集、运行时解压在 _MEIPASS
        # 临时目录；app_dir() 返回 exe 所在目录（配置/日志落点），两者不同。
        # 优先 _MEIPASS（打包自带），其次 exe 目录（仅源码运行/打包未收集
        # 图标时兜底；frozen 下 _MEIPASS/app.ico 恒存在，该分支实际不命中），
        # 最后系统默认。三级加载保证任何形态都有可见图标。
        candidates = []  # type: List[str]
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(os.path.join(str(meipass), "app.ico"))
        if icon_path:
            candidates.append(icon_path)
        for p in candidates:
            if p and os.path.isfile(p):
                h = user32.LoadImageW(
                    None, p, IMAGE_ICON, 16, 16,
                    LR_LOADFROMFILE | LR_DEFAULTSIZE)
                if h:
                    return h, True
        return user32.LoadIconW(None, IDI_APPLICATION), False

    def destroy(self):
        # type: () -> None
        """移除托盘图标并销毁窗口（幂等，可在异常路径重复调用）。"""
        if self._nid is not None:
            shell32 = getattr(ctypes, "windll").shell32
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            self._nid = None
        if self._window:
            self._user32().DestroyWindow(self._window)
            self._window = None
            # 窗口已销毁：注销窗口类，避免进程内重复创建时 RegisterClassW 失败
            try:
                user32 = self._user32()
                kernel32 = getattr(ctypes, "windll").kernel32
                hinst = kernel32.GetModuleHandleW(None)
                user32.UnregisterClassW(self._CLASS_NAME, hinst)
            except Exception:
                pass
        if self._icon is not None:
            # 仅销毁自有图标；系统共享图标（IDI_APPLICATION）禁止 DestroyIcon
            if self._icon_owned:
                self._user32().DestroyIcon(self._icon)
            self._icon = None
            self._icon_owned = False
        self._callback_ref = None

    def __del__(self):
        # type: () -> None
        """GC 兜底：对象被回收但资源未释放时清理（幂等，尽力而为）。"""
        try:
            if self._nid is not None or self._window is not None:
                self.destroy()
        except Exception:
            pass

    # ---------- 回调 ----------
    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        # type: (Any, int, int, int) -> int
        """消息窗口的 WndProc：托盘通知都走 WM_APP+1 通道。"""
        if msg == WM_APP + 1:
            evt = lparam & 0xFFFF
            try:
                # V3 模式 lParam=WM_LBUTTONUP（原始鼠标消息）；
                # V4 模式 lParam=NIN_SELECT(0x0400)。两者都算左键激活。
                if evt in (NIN_SELECT, WM_LBUTTONUP):
                    if self._on_activate is not None:
                        self._on_activate()
                elif evt in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._show_menu()
            except Exception:
                # 回调异常不得穿过 WNDPROC 进入 Windows 消息泵（会中断
                # Tk 主循环/显示系统错误对话框）；托盘交互失败仅记录日志
                try:
                    get_logger().error("托盘回调异常: %s" % sys.exc_info()[1])
                except Exception:
                    pass
        return self._user32().DefWindowProcW(hwnd, msg, wparam, lparam)

    def _show_menu(self):
        # type: () -> None
        user32 = self._user32()
        menu = user32.CreatePopupMenu()
        try:
            for item_id, label in self._menu:
                user32.AppendMenuW(menu, MF_STRING, item_id, label)
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            # 必须 SetForegroundWindow，否则 TrackPopupMenu 首次点击不会消失
            user32.SetForegroundWindow(self._window)
            cmd = user32.TrackPopupMenu(
                menu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                pt.x, pt.y, 0, self._window, None)
            if cmd:
                self._on_menu(cmd)
        finally:
            user32.DestroyMenu(menu)
            # 菜单关闭后把焦点还给主窗口，避免滞留在隐藏的消息窗口
            self._return_focus_to_main()

    def _return_focus_to_main(self):
        # type: () -> None
        """菜单关闭后把焦点还给主窗口（若有主窗口句柄）。"""
        if self._main_hwnd is not None and self._main_hwnd:
            try:
                user32 = self._user32()
                kernel32 = getattr(ctypes, "windll").kernel32
                # AttachThreadInput 绕过前台窗口锁定限制
                tid = user32.GetWindowThreadProcessId(self._main_hwnd, None)
                cur_tid = kernel32.GetCurrentThreadId()  # GetCurrentThreadId 属 kernel32
                if tid and tid != cur_tid:
                    user32.AttachThreadInput(cur_tid, tid, True)
                    user32.SetForegroundWindow(self._main_hwnd)
                    user32.AttachThreadInput(cur_tid, tid, False)
                else:
                    user32.SetForegroundWindow(self._main_hwnd)
                user32.SetActiveWindow(self._main_hwnd)
            except Exception:
                pass

    # ---------- 工具 ----------
    def _user32(self):
        # type: () -> Any
        return getattr(ctypes, "windll").user32
