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
_wake_thread = None        # type: Optional[threading.Thread]  # 监听线程引用
# 监听线程的停止信号：进程退出前必须让监听线程主动结束，否则它会带着
# 阻塞中的调用撞上解释器 finalization（见 stop_wakeup_listener 说明）
_stop = threading.Event()  # type: threading.Event

# Win32 常量
_ERROR_ALREADY_EXISTS = 183
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 0x00000102

# 监听循环的节流参数：都必须是**有限**值。
# 曾经用 _INFINITE(0xFFFFFFFF) 做等待超时，导致监听线程带着这次阻塞撞上
# 解释器 finalization（PyEval_RestoreThread: NULL tstate），故此处不再定义
# _INFINITE 常量，避免被误用回去。
_WAKE_WIN_TIMEOUT = 500        # Win32 等待超时（毫秒）
_WAKE_POLL_INTERVAL = 1.0      # POSIX 轮询间隔（秒）


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
    global _wake_thread
    if not _acquired:
        return
    if sys.platform == "win32" and _wake_event is not None:
        _stop.clear()
        t = threading.Thread(target=_win_wait_loop, args=(callback,),
                             name="singleinstance-wake", daemon=True)
        _wake_thread = t
        t.start()
    elif sys.platform in ("linux", "darwin") and _wake_marker_path is not None:
        _stop.clear()
        t = threading.Thread(target=_posix_wait_loop, args=(callback,),
                             name="singleinstance-wake", daemon=True)
        _wake_thread = t
        t.start()


def stop_wakeup_listener(timeout=2.0):
    # type: (float) -> None
    """停止唤起监听线程（进程退出前调用）。幂等，绝不抛异常。

    为什么必须有这一步：监听线程虽为 daemon，但一旦启动就阻塞在等待调用
    里（Windows 为 WaitForSingleObject，POSIX 为轮询睡眠）。进程退出时
    mainloop() 返回后解释器进入 finalization，daemon 线程若仍阻塞在其中，
    从该调用返回后会尝试恢复已被销毁的 thread state，触发 CPython 3.13
    的致命检查：

        Fatal Python error: PyEval_RestoreThread: NULL tstate

    该错误必现（线程启动后每次退出都会撞上），且发生在解释器收尾阶段，
    常规 try/except 与日志都拦不住。修复方式是在 mainloop 返回后、解释器
    真正收尾前让线程主动结束：置 _stop、唤醒阻塞中的等待、有限超时 join。
    """
    global _wake_thread
    _stop.set()
    # 唤醒可能正阻塞在 WaitForSingleObject 的线程，让它立刻醒来检查 _stop
    if sys.platform == "win32" and _wake_event:
        try:
            import ctypes as _ct
            _ct.windll.kernel32.SetEvent(_wake_event)  # type: ignore[attr-defined]
        except Exception:
            pass
    t = _wake_thread
    if t is not None:
        try:
            t.join(timeout)
        except Exception:
            pass
        if t.is_alive():
            # 有界等待超时：仍不阻塞退出（线程是 daemon，解释器会回收）
            try:
                get_logger().warn("唤起监听线程未在 %.1fs 内停止(不影响退出)"
                                  % timeout)
            except Exception:
                pass
        _wake_thread = None


# ---------- Windows ----------
def _win_acquire():
    # type: () -> bool
    global _wake_event
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]  # Windows 专属，mypy 无存根
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
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]  # Windows 专属，mypy 无存根
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.ResetEvent.restype = wintypes.BOOL
    kernel32.ResetEvent.argtypes = [wintypes.HANDLE]
    while not _stop.is_set():
        # 有限超时而非 _INFINITE：循环需周期性醒来检查 _stop，
        # 否则进程退出时线程会带着这次阻塞撞上解释器 finalization
        # （PyEval_RestoreThread: NULL tstate，CPython 3.13 致命错误）
        rc = kernel32.WaitForSingleObject(_wake_event, _WAKE_WIN_TIMEOUT)
        if _stop.is_set():
            return
        if rc == _WAIT_TIMEOUT:
            continue        # 超时：回到循环顶部复查停止标志
        if rc != _WAIT_OBJECT_0:
            return            # 等待异常：退出监听（唤起降级，不影响其他功能）
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
    while not _stop.is_set():
        # _stop.wait 而非 time.sleep：前者被置位时立即返回，退出无需等满
        # 一个轮询周期（与 Win32 分支同构，保证退出时线程能及时结束）
        if _stop.wait(_WAKE_POLL_INTERVAL):
            return
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
