"""退出时序混入：X 关闭转后台 / 真正退出的完整关闭编排。

职责边界
--------
从 gui_app.App 拆出的进程退出编排：on_close（未置退出标志时转后台，
否则进入关闭序列）、_join_workers_bounded（有界等待写盘线程）、
_finish_close（取消 after 链 → 关日志 → 移托盘图标 → 销毁窗口）。

宿主状态契约
------------
root / logger / scheduler、_cancel 与 _sched_cancel（置位取消信号）、
_workers 与 _workers_lock、_quitting（读）、_closing（**写**，核心层
_tick/_on_diff_ready 读它做交互屏蔽）、_tick_id / _drain_id、_tray，
以及 SyncFlowMixin._hide_wait 与核心层 _ui_put。

线程模型：收尾等待放在后台线程执行，主线程保持事件循环消费 UI 队列；
完成后经队列回到主线程销毁窗口（见 on_close 内注释）。
"""

import threading
from typing import Any, Callable, List, Optional, TYPE_CHECKING

import tkinter as tk

from scheduler import Scheduler, join_threads_bounded
from logger import AppLogger


class CloseSeqMixin(object):
    """退出关闭时序（见模块 docstring 的宿主状态契约）。"""

    # ---------- 宿主状态契约（仅类型声明，运行期被实例属性遮蔽） ----------
    root = None                 # type: tk.Tk
    logger = None               # type: AppLogger
    scheduler = None            # type: Scheduler
    _cancel = None              # type: threading.Event
    _sched_cancel = None        # type: threading.Event
    _workers = []               # type: List[threading.Thread]
    _workers_lock = None        # type: threading.Lock
    _quitting = False           # type: bool
    _closing = False            # type: bool
    _tick_id = None             # type: Optional[str]
    _drain_id = None            # type: Optional[str]
    _tray = None                # type: Optional[Any]  # noqa: E501  (tray_mod.TrayIcon)
    if TYPE_CHECKING:  # 方法契约仅类型层：类级赋值会按 MRO 遮蔽其他混入的真实现
        _hide_to_background = None  # type: Callable[..., None]
        _hide_wait = None       # type: Callable[..., None]
        _ui_put = None          # type: Callable[..., None]

    def _join_workers_bounded(self):
        # type: () -> None
        """有界等待调度 worker 与手动同步 worker 结束（退出流程用）。"""
        self.scheduler.wait_workers(5)
        # 手动同步 worker（diff/apply）同样有界等待：此前仅等调度
        # worker，手动线程游离在 wait_workers 之外，进程退出时被强杀，
        # 大文件复制中途被杀会留下半截 .tmp~ 残留
        with self._workers_lock:
            workers = list(self._workers)
        join_threads_bounded(workers, 5)

    def on_close(self):
        # type: () -> None
        """窗口关闭按钮（X）：未置退出标志时转后台运行，否则完整关闭。"""
        if not self._quitting:
            # X 关闭 → 最小化后台运行（Windows 托盘 / 非 Windows 任务栏）
            self._hide_to_background()
            return
        if self._closing:
            return
        self._closing = True
        # M-5：先隐藏主窗口，屏蔽关闭期间（最多约 6s）的交互，避免再触发新线程/对话框
        try:
            self.root.withdraw()
        except tk.TclError:
            pass
        self._cancel.set()
        self._sched_cancel.set()   # 调度 worker 一并取消（见 _run_task）
        self._hide_wait()
        self.scheduler.stop()
        # 等待收尾放到后台线程（有界），主线程保持事件循环处理 UI 队列；
        # 完成后经队列回到主线程销毁窗口，避免长时间冻结界面
        def _shutdown():
            # type: () -> None
            try:
                self._join_workers_bounded()
            finally:
                self._ui_put(self._finish_close)
        try:
            threading.Thread(target=_shutdown, daemon=True).start()
        except Exception as e:
            # 收尾线程起不来（极端资源耗尽）：当前线程内联有界收尾兜底，
            # 保证 _finish_close 必达。窗口已 withdraw，阻塞至多约 10 秒
            # 无交互可被打断；不走 _ui_put——内联执行期间主循环被占，
            # 队列里的 _finish_close 得不到消费
            self.logger.error("关闭收尾线程启动失败，改为内联收尾: %s" % e)
            try:
                self._join_workers_bounded()
            finally:
                self._finish_close()

    def _finish_close(self):
        # type: () -> None
        # 先取消周期性 after，避免 destroy 后残留回调触发
        # "invalid command name" 的 background error
        for aid in (self._tick_id, self._drain_id):
            if aid is not None:
                try:
                    self.root.after_cancel(aid)
                except Exception:
                    pass
        try:
            self.logger.close()
        except Exception:
            pass
        # 退出前移除托盘图标（NIM_DELETE），避免残留幽灵图标
        if self._tray is not None:
            try:
                self._tray.destroy()
            except Exception:
                pass
            self._tray = None
        try:
            self.root.destroy()
        except tk.TclError:
            pass
