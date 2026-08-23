"""手动同步流程混入：等待窗（进度/取消）+ worker 登记 + 预览/执行链路。

职责边界
--------
从 gui_app.App 拆出的"一次手动同步从点击到收尾"的全部状态机：
_on_sync_now -> _diff_worker(线程) -> _on_diff_ready(主线程预览确认)
-> _apply_worker(线程) -> 统一收尾弹窗。等待窗的代数号防竞态协议
（_show_wait/_hide_wait）也在此处——它与同步流程强耦合。

宿主状态契约
------------
本混人不持有独立状态，全部读写 App.__init__ 初始化并共享的属性：
root / logger / store / scheduler / self_paths、_cancel、_workers 与
_workers_lock、_manual_busy、_wait* 系列、_prog_count / _last_prog_ts、
_closing（读）、_tray / _tray_hidden（读）、以及核心层方法
_ui_put / _refresh_tasks。拆分仅为可维护性，运行期仍是同一个实例。
"""

import time
import threading
from typing import Any, Callable, List, Optional, Set, TYPE_CHECKING

import tkinter as tk
from tkinter import ttk, messagebox

from config import Task, TaskStore, CONFLICT_ASK
from sync_engine import perform_sync, apply_diff, finalize_sync
from scanner import ScanCancelled
from logger import AppLogger
from scheduler import Scheduler


class SyncFlowMixin(object):
    """手动同步流程与等待窗（见模块 docstring 的宿主状态契约）。"""

    # ---------- 宿主状态契约（仅类型声明） ----------
    # 运行期实例属性由 App.__init__ 提供并遮蔽下列类级占位；方法占位在
    # MRO 上也永远排在 App 核心实现之后，不会被命中。
    root = None                 # type: tk.Tk
    logger = None               # type: AppLogger
    store = None                # type: TaskStore
    scheduler = None            # type: Scheduler
    self_paths = set()          # type: Set[str]
    _cancel = None              # type: threading.Event
    _workers = []               # type: List[threading.Thread]
    _workers_lock = None        # type: threading.Lock
    _manual_busy = False        # type: bool
    _wait = None                # type: Optional[tk.Toplevel]
    _wait_label = None          # type: Optional[ttk.Label]
    _wait_prog = None           # type: Optional[ttk.Label]
    _wait_bar = None            # type: Optional[ttk.Progressbar]
    _wait_cancellable = False   # type: bool
    _wait_gen = 0               # type: int
    _prog_count = 0             # type: int
    _last_prog_ts = 0.0         # type: float
    _closing = False            # type: bool
    _tray = None                # type: Any
    _tray_hidden = False        # type: bool
    if TYPE_CHECKING:  # 方法契约仅类型层：类级赋值会按 MRO 遮蔽核心层真实现
        _ui_put = None          # type: Callable[..., None]
        _refresh_tasks = None   # type: Callable[..., None]
        _selected_task = None   # type: Callable[[], Optional[Task]]


    def _start_worker(self, target, args=()):
        # type: (Callable[..., Any], Any) -> bool
        """启动并登记手动同步 worker（见 _workers 注释）。返回是否启动成功。

        t.start() 可能因系统线程资源耗尽抛异常：此时回滚 _workers 登记
        并返回 False，由调用方释放运行槽/门闩——与 scheduler 的同类回滚
        一致，否则任务永久显示"运行中"且手动同步全局锁死。
        """
        t = threading.Thread(target=self._worker_wrapper, args=(target, args),
                             daemon=True)
        with self._workers_lock:
            self._workers.append(t)
        try:
            t.start()
        except Exception as e:
            with self._workers_lock:
                try:
                    self._workers.remove(t)
                except ValueError:
                    pass
            self.logger.error("同步 worker 线程启动失败: %s" % e)
            return False
        return True

    def _worker_wrapper(self, target, args):
        # type: (Callable[..., Any], Any) -> None
        cur = threading.current_thread()
        try:
            target(*args)
        finally:
            with self._workers_lock:
                try:
                    self._workers.remove(cur)
                except ValueError:
                    pass

    def _release_manual(self, task_id):
        # type: (str) -> None
        """释放手动同步的运行槽与全局门闩（仅手动同步路径调用）。

        顺序：先 release 运行槽、后清 _manual_busy。若先清门闩，窗口期内
        新手动同步可越过 _manual_busy 检查并 acquire 其他任务的运行槽，
        与新 worker 尾部的 _ui_put 交错（跨任务互斥门闩形同虚设）。
        """
        self.scheduler.release(task_id)
        self._manual_busy = False

    def _on_sync_now(self):
        # type: () -> None
        task = self._selected_task()
        if task is None:
            messagebox.showinfo("提示", "请先选择要同步的任务")
            return
        # README 承诺：禁用任务不参与定时，也不手动同步
        if not task.enabled:
            messagebox.showinfo("提示", "该任务已禁用，请先在编辑中启用后再同步")
            return
        if self._manual_busy:
            messagebox.showinfo("提示", "已有手动同步正在进行，请等待完成")
            return
        # 从预览开始就占用运行槽：预览期间调度器不会并发触发，预览/执行一致
        if not self.scheduler.acquire(task.id):
            messagebox.showinfo("提示", "该任务正在运行中，请稍候")
            return
        self._manual_busy = True
        try:
            self._cancel.clear()
            self._prog_count = 0
            self._last_prog_ts = 0.0
            self._show_wait("正在扫描并对比差异...", cancellable=True)
            # 线程启动失败（资源耗尽等）同样必须回滚运行槽与门闩，
            # 否则任务永久"运行中"且手动同步门闩锁死
            if not self._start_worker(self._diff_worker, (task,)):
                raise RuntimeError("同步线程启动失败")
        except Exception as e:
            # 等待窗创建失败/线程启动失败时必须释放运行槽与门闩，否则任务永久"运行中"
            self.logger.error("启动手动同步失败: %s" % e)
            try:
                self._hide_wait()
            except Exception:
                pass
            self._release_manual(task.id)
            self._popup_if_alive("error", "错误", "启动同步失败：%s" % e)
            return

    def _progress_cb(self, rel):
        # type: (str) -> None
        # worker 线程调用：时间节流（≥500ms 才投递一次）后经 UI 队列刷新等待窗进度。
        # P3 修复：此前按"每 20 项"计数节流，10 万级目录会向 UI 队列投递数千次，
        # drain 每 100ms 只消费 100 个导致积压卡顿；时间节流与扫描速度无关。
        now = time.time()
        if now - self._last_prog_ts >= 0.5:
            self._last_prog_ts = now
            self._prog_count += 1
            short = rel if len(rel) <= 60 else ("..." + rel[-57:])
            n = self._prog_count
            self._ui_put(lambda: self._set_wait_progress("已扫描 %d 项：%s" % (n, short)))

    def _popup_if_alive(self, kind, title, msg):
        # type: (str, str, str) -> None
        """主线程弹窗（经 UI 队列投递执行）：退出流程中不弹模态框。

        worker 尾部的 messagebox 若不检查 _closing，用户关窗退出时隐藏窗口
        后仍会弹出模态框，阻塞队列里排在后面的 _finish_close（进程"看似卡死"）。
        窗口隐藏到托盘（含 --autostart 后台形态）时同样不弹：不可见模态框
        会阻塞 UI 队列直到用户恢复窗口点掉；结果已写入任务状态列与日志，
        恢复窗口即可查看。
        """
        if self._closing or self._tray_hidden:
            # 托盘隐藏态（非退出期）：气泡通知兜底，用户不恢复窗口也能
            # 感知同步结果；退出期进程将销毁，仅落日志。
            if self._tray_hidden and not self._closing and self._tray is not None:
                try:
                    self._tray.notify(title, msg)
                except Exception:
                    pass
            self.logger.info("[托盘隐藏态/退出期] %s: %s" % (title, msg))
            return
        if kind == "info":
            messagebox.showinfo(title, msg)
        else:
            messagebox.showerror(title, msg)

    def _diff_worker(self, task):
        # type: (Task) -> None
        try:
            res = perform_sync(task, logger=self.logger, self_paths=self.self_paths,
                               dry_run=True, progress=self._progress_cb,
                               cancel_event=self._cancel)
            self._ui_put(lambda: self._on_diff_ready(task, res))
        except ScanCancelled:
            self._release_manual(task.id)
            self._ui_put(lambda g=self._wait_gen: self._hide_wait(g))
            self._ui_put(lambda: self._popup_if_alive("info", "提示", "已取消"))
            self._ui_put(self._refresh_tasks)
        except Exception as e:
            self.logger.error("对比失败 [%s]: %s" % (task.name, e))
            self._release_manual(task.id)
            self._ui_put(lambda g=self._wait_gen: self._hide_wait(g))
            self._ui_put(lambda: self._popup_if_alive("error", "错误", "对比失败：%s" % e))
            self._ui_put(self._refresh_tasks)

    def _on_diff_ready(self, task, res):
        # type: (Task, dict) -> None
        # 主线程执行（经 UI 队列）：关闭等待窗并弹出差异预览。
        # 任何异常都必须释放运行槽，否则任务永久显示"运行中"，编辑/删除/同步全被拒。
        try:
            self._hide_wait()
            if self._closing:
                # 关闭流程已启动（用户已明确退出）：只释放运行槽，不再弹预览/
                # 启动执行线程（on_close 已 _cancel.set()，预览结果的时效也无意义）
                self._release_manual(task.id)
                return
            if res.get("aborted"):
                # 引擎中止（源目录不可达/扫描不完整）：明确报错，
                # 不能因 diff 为空而误报"无需同步（无差异）"
                self._release_manual(task.id)
                self._refresh_tasks(full=False)
                # 托盘隐藏态/退出期不弹不可见模态框（与 worker 路径同一守卫）
                self._popup_if_alive("error", "错误", "同步已中止：%s" % task.last_summary)
                return
            if res["diff"].is_empty():
                # 无差异：无需确认，直接收尾（所见即所得——0 个动作无可执行）
                self._release_manual(task.id)
                self._refresh_tasks(full=False)
                self._popup_if_alive("info", "提示", "无需同步（无差异）")
                return
            from gui_diff import DiffDialog
            dlg = DiffDialog(self.root, res["diff"], task)
            self.root.wait_window(dlg)
            if self._closing:
                # 预览期间主窗口已关闭：放弃执行（关闭流程已 _cancel.set()，
                # 此处的 _cancel.clear() 不再执行，避免抵消取消信号后启动
                # 一个 wait_workers 等不到的写盘线程，被进程退出强杀）
                self._release_manual(task.id)
                return
            if dlg.result_policy is None:
                # 取消预览：释放运行槽
                self._release_manual(task.id)
                self._refresh_tasks(full=False)
                return
            if dlg.result_policy == "_noop_":
                self._release_manual(task.id)
                self._refresh_tasks(full=False)
                self._popup_if_alive("info", "提示", "无需同步（无差异）")
                return
            policy = dlg.result_policy if dlg.result_policy != CONFLICT_ASK else None
            self._cancel.clear()  # 确保已确认的执行为干净运行，不被残留取消标志秒变 no-op
            self._show_wait("正在执行同步...", cancellable=True)
            if not self._start_worker(self._apply_worker, (task, res, policy)):
                # 抛给下方 except 统一收尾：释放运行槽 + 关等待窗 + 报错
                raise RuntimeError("同步执行线程启动失败")
        except Exception as e:
            self.logger.error("预览/执行准备异常 [%s]: %s" % (task.name, e))
            try:
                self._release_manual(task.id)
            except Exception:
                pass
            try:
                self._hide_wait()
            except Exception:
                pass
            try:
                self._popup_if_alive("error", "错误", "操作失败：%s" % e)
            except Exception:
                pass
            self._refresh_tasks(full=False)

    def _apply_worker(self, task, res, policy):
        # type: (Task, dict, Optional[str]) -> None
        # 运行槽已在预览开始时占用（acquire），此处直接复用预览结果执行：所见即所得
        try:
            out = apply_diff(task, res["diff"], conflict_policy=policy,
                             self_paths=self.self_paths, logger=self.logger,
                             cancel_event=self._cancel, dst_snap=res.get("dst_snap"))
            # 统一收尾：审计日志 + 运行期字段 + baseline（与 CLI 路径共用同一实现）
            finalize_sync(task, out, self.store, self.logger)
            self._ui_put(lambda g=self._wait_gen: self._hide_wait(g))
            self._ui_put(lambda: self._popup_if_alive(
                "info", "完成", "同步完成：%s" % task.last_summary))
        except ScanCancelled:
            self.logger.warn("任务[%s] 已取消" % task.name)
            task.last_status = "已取消"
            self.store.update_runtime(task)
            self._ui_put(lambda g=self._wait_gen: self._hide_wait(g))
            self._ui_put(lambda: self._popup_if_alive("info", "提示", "已取消"))
        except Exception as e:
            self.logger.error("同步失败 [%s]: %s" % (task.name, e))
            # 异常路径落盘失败状态（与 _run_task 一致），避免停留上次的"成功"
            task.last_run = time.time()
            task.last_status = "失败"
            task.last_summary = "执行异常: %s" % e
            try:
                self.store.update_runtime(task)
            except Exception:
                pass
            self._ui_put(lambda g=self._wait_gen: self._hide_wait(g))
            self._ui_put(lambda: self._popup_if_alive("error", "错误", "同步失败：%s" % e))
        finally:
            self._release_manual(task.id)
            self._ui_put(self._refresh_tasks)

    # ---------- 等待提示（含进度与取消） ----------
    def _show_wait(self, msg, cancellable=False):
        # type: (str, bool) -> None
        self._wait_gen += 1  # 每次显示都提升代数，残留的旧 _hide_wait 不再能销毁本窗
        if self._wait is not None:
            try:
                if self._wait.winfo_exists():
                    assert self._wait_label is not None  # 有等待窗则必有标签
                    self._wait_label.config(text=msg)
                    return
            except tk.TclError:
                pass
            # 旧等待窗已被用户点 X 销毁：清理引用，走下方重建，避免下次 run 走旧分支不建窗
            self._wait = None
        self._wait_cancellable = cancellable
        self._wait = tk.Toplevel(self.root)
        self._wait.title("请稍候")
        self._wait.transient(self.root)
        self._wait.resizable(False, False)
        self._wait.protocol("WM_DELETE_WINDOW", self._on_wait_close)
        self._wait_label = ttk.Label(self._wait, text=msg, padding=(20, 10))
        self._wait_label.pack()
        self._wait_prog = ttk.Label(self._wait, text="", padding=(0, 0))
        self._wait_prog.pack()
        self._wait_bar = ttk.Progressbar(self._wait, mode="indeterminate", length=280)
        self._wait_bar.pack(padx=20, pady=6)
        self._wait_bar.start(80)
        if cancellable:
            ttk.Button(self._wait, text="取消", command=self._on_cancel_wait).pack(pady=(0, 12))
        try:
            self._wait.grab_set()   # 模态化，防止等待期间误操作主窗口
        except tk.TclError:
            pass
        self._wait.geometry("+%d+%d" % (self.root.winfo_x() + 200, self.root.winfo_y() + 200))

    def _set_wait_progress(self, text):
        # type: (str) -> None
        if self._wait is not None and self._wait_prog is not None:
            try:
                self._wait_prog.config(text=text)
            except tk.TclError:
                pass

    def _on_wait_close(self):
        # type: () -> None
        # 点 X 关闭等待窗：可取消则等同取消；不可取消则忽略关闭（保持模态不被误关）
        if self._wait_cancellable:
            self._on_cancel_wait()

    def _on_cancel_wait(self):
        # type: () -> None
        self._cancel.set()
        if self._wait is not None and self._wait_prog is not None:
            try:
                self._wait_prog.config(text="正在取消...")
            except tk.TclError:
                pass

    def _hide_wait(self, gen=None):
        # type: (Optional[int]) -> None
        # 代数校验：若期间已显示新的等待窗（gen 不匹配），残留的旧 hide
        # 不得销毁新窗（worker 先入队 hide、后置 _manual_busy 的窗口期竞态）
        if gen is not None and gen != self._wait_gen:
            return
        if self._wait is not None:
            try:
                if self._wait_bar is not None:
                    self._wait_bar.stop()
                self._wait.destroy()
            except tk.TclError:
                pass
            self._wait = None
            self._wait_label = None
            self._wait_prog = None
            self._wait_bar = None
            self._wait_cancellable = False
