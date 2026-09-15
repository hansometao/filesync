"""单实例防护：阻止同时运行两个 GUI 实例，并支持唤起已有实例。

动机：两个实例并发运行时调度器各自触发同步，会互相覆盖 tasks.json/
baseline（原子写只保证文件完整，不保证内容不丢）；最小化到托盘后再次
双击 exe 会新起实例而非唤回窗口，加重"程序已退出"的误判。

机制：
- Windows：命名互斥体（Local\\FolderSync.Mutex）占坑，进程退出自动释放；
  唤起走命名手动复位事件（Local\\FolderSync.Wakeup）——已有实例的后台
  监听线程收到信号后经 UI 队列恢复窗口。手动复位事件保持置位直到
  ResetEvent，唤起信号不会因监听线程尚未启动而丢失。
- POSIX（Linux/macOS）：config 目录锁文件 flock 非阻塞占坑；唤起写
  时间戳标记文件，监听线程每秒轮询 mtime（flock 无法跨进程发信号，
  1s 延迟可接受）。监听基线取占坑时刻的墙钟，早于占坑的陈旧标记
  （上次异常退出残留）不会误触发。

无头 CLI（--list/--sync/--help）不受单实例限制：cron 定时同步与 GUI
常驻必须能共存（调用方在 main 中只对 GUI 路径启用）。

占坑/唤起机制自身故障时 fail-open（允许第二实例运行并记录日志）：
单实例是体验与数据一致性优化，不能因防护故障导致程序完全无法启动。
"""

import ctypes
import os
import sys
import time
import threading
from ctypes import wintypes
from typing import Any, Callable, Optional

from utils.paths import app_dir
from logger import get_logger

# 进程级状态（acquire 成功后填充，进程存活期间持有）
_acquired = False          # type: bool
_lock_file = None          # type: Optional[Any]         # POSIX flock 文件句柄
_wake_event = None         # type: Optional[int]         # Windows 命名事件句柄
_wake_marker_path = None   # type: Optional[str]         # POSIX 唤起标记文件路径
_start_wall = 0.0          # type: float                 # 占坑时刻（墙钟秒）

# Win32 常量
_ERROR_ALREADY_EXISTS = 183
_WAIT_OBJECT_0 = 0
_INFINITE = 0xFFFFFFFF


def acquire_single_instance():
    # type: () -> bool
    """尝试成为唯一 GUI 实例。返回 True=已占坑；False=已有实例在运行。

    调用方收到 False 后应先 notify_existing_instance()（唤起已有实例）
    再退出本进程。Windows 下唤起信号在检测到已有实例时一并发出，
    notify_existing_instance 为幂等 no-op。
    """
    global _acquired, _start_wall
    if _acquired:
        return True
    try:
        if sys.platform == "win32":
            ok = _win_acquire()
        elif sys.platform in ("linux", "darwin"):
            ok = _posix_acquire()
        else:
            return True  # 未知平台：无防护机制，放行
    except Exception as e:
        # fail-open：防护机制故障不阻断启动
        try:
            get_logger().warn("单实例防护机制异常(已放行): %s" % e)
        except Exception:
            pass
        return True
    if ok:
        _acquired = True
        _start_wall = time.time()
    return ok


def notify_existing_instance():
    # type: () -> None
    """唤起已有实例（恢复其主窗口）。尽力而为，绝不抛异常。"""
    try:
        if sys.platform == "win32":
            return  # 唤起事件已在 _win_acquire 检测到已有实例时发出
        _posix_notify()
    except Exception:
        pass


def start_wakeup_listener(callback):
    # type: (Callable[[], None]) -> None
    """在已占坑实例中启动唤起监听（后台守护线程，进程存活期间常驻）。

    callback 在监听线程中被调用，须由调用方保证线程安全
    （GUI 传 lambda: app._ui_put(...) 经 UI 队列投递主线程）。
    """
    if not _acquired:
        return
    if sys.platform == "win32" and _wake_event is not None:
        t = threading.Thread(target=_win_wait_loop, args=(callback,),
                             name="singleinstance-wake", daemon=True)
        t.start()
    elif sys.platform in ("linux", "darwin") and _wake_marker_path is not None:
        t = threading.Thread(target=_posix_wait_loop, args=(callback,),
                             name="singleinstance-wake", daemon=True)
        t.start()


# ---------- Windows ----------
def _win_acquire():
    # type: () -> bool
    global _wake_event
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL,
                                      wintypes.LPCWSTR]
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL,
                                      wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.SetEvent.restype = wintypes.BOOL
    kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    kernel32.ResetEvent.restype = wintypes.BOOL
    kernel32.ResetEvent.argtypes = [wintypes.HANDLE]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]

    mutex = kernel32.CreateMutexW(None, True, u"Local\\FolderSync.Mutex")
    if not mutex:
        # 创建失败（权限等）：fail-open，放行且不启用唤起监听
        get_logger().warn("单实例互斥体创建失败(已放行)")
        return True
    if kernel32.GetLastError() == _ERROR_ALREADY_EXISTS:
        # 已有实例：通过命名事件唤醒其监听线程（事件由已有实例创建，
        # 此处 CreateEventW 打开同一命名对象）；手动复位保持置位，
        # 即使对方监听线程尚未启动也不会丢信号
        ev = kernel32.CreateEventW(None, True, False, u"Local\\FolderSync.Wakeup")
        if ev:
            kernel32.SetEvent(ev)
        return False
    ev = kernel32.CreateEventW(None, True, False, u"Local\\FolderSync.Wakeup")
    _wake_event = ev if ev else None
    return True


def _win_wait_loop(callback):
    # type: (Callable[[], None]) -> None
    kernel32 = ctypes.windll.kernel32
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.ResetEvent.restype = wintypes.BOOL
    kernel32.ResetEvent.argtypes = [wintypes.HANDLE]
    while True:
        rc = kernel32.WaitForSingleObject(_wake_event, _INFINITE)
        if rc != _WAIT_OBJECT_0:
            return  # 等待异常：退出监听（唤起功能降级失效，不影响其他功能）
        kernel32.ResetEvent(_wake_event)
        try:
            callback()
        except Exception:
            try:
                get_logger().warn("唤起回调执行失败: %s" % sys.exc_info()[1])
            except Exception:
                pass


# ---------- POSIX ----------
def _config_dir():
    # type: () -> str
    return os.path.join(app_dir(), "config")


def _posix_acquire():
    # type: () -> bool
    global _lock_file, _wake_marker_path
    import fcntl
    d = _config_dir()
    if not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    lock_path = os.path.join(d, "foldersync.lock")
    f = open(lock_path, "a+")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False  # 已有实例持有锁
    _lock_file = f  # 保持引用：句柄被 GC 会释放锁
    _wake_marker_path = os.path.join(d, "foldersync.wake")
    # 清理上次异常退出可能残留的陈旧标记（其 mtime 必早于本次占坑时刻）
    try:
        if os.path.exists(_wake_marker_path):
            os.remove(_wake_marker_path)
    except OSError:
        pass
    return True


def _posix_notify():
    # type: () -> None
    if _wake_marker_path is None:
        return
    d = os.path.dirname(_wake_marker_path)
    if not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    with open(_wake_marker_path, "a") as f:
        f.write("%.3f\n" % time.time())


def _posix_wait_loop(callback):
    # type: (Callable[[], None]) -> None
    marker = _wake_marker_path
    baseline = _start_wall
    if marker is None:
        return
    while True:
        time.sleep(1.0)
        try:
            m = os.path.getmtime(marker)
        except OSError:
            continue
        if m > baseline:
            # 消费标记（删除防重复触发），再回调
            try:
                os.remove(marker)
            except OSError:
                pass
            try:
                callback()
            except Exception:
                try:
                    get_logger().warn("唤起回调执行失败: %s" % sys.exc_info()[1])
                except Exception:
                    pass
