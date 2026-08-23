"""主窗口：任务列表 + 工具栏 + 日志面板，串联调度器与同步流程。

线程模型
--------
worker 线程（diff/apply/调度执行）**不直接调用任何 tkinter API**：
一切 UI 更新通过 `_ui_queue` 投递，主线程每 100ms 由 `_drain_ui_queue`
统一执行。这是对 tkinter 非线程安全的根本性规避（也消除了跨线程
root.after 的 marshal 死锁风险）。
"""

import os
import sys
import time
import queue
import threading
from typing import Any, Callable, List, Optional

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

from config import (
    Task, TaskStore, MODE_ONE_WAY, MODE_TWO_WAY, CONFLICT_ASK,
)
from scheduler import Scheduler
from sync_engine import perform_sync, apply_diff, finalize_sync
from scanner import ScanCancelled
from logger import init_logger
from utils.paths import longpath, app_dir
from utils.timeutil import format_epoch
import autostart
import tray as tray_mod

APP_DIR = app_dir()
LOG_DIR = os.path.join(APP_DIR, "logs")
CONFIG_PATH = os.path.join(APP_DIR, "config", "tasks.json")

_MODE_LABEL = {MODE_ONE_WAY: "单向镜像", MODE_TWO_WAY: "双向同步"}


class App(object):
    def __init__(self, root, autostart=False):
        # type: (tk.Tk, bool) -> None
        self.root = root
        self.logger = init_logger(LOG_DIR, quiet=True)  # GUI 模式抑制控制台打印
        self.store = TaskStore(CONFIG_PATH)
        self.self_paths = {
            os.path.abspath(LOG_DIR),
            os.path.abspath(os.path.dirname(CONFIG_PATH)),
            os.path.abspath(os.path.join(os.path.dirname(CONFIG_PATH), "baseline")),
        }
        self.scheduler = Scheduler(self.store, self._run_task, self.logger)
        self.scheduler.set_status_callback(lambda: self._ui_put(self._refresh_tasks))

        self._wait = None                  # type: Optional[tk.Toplevel]
        self._wait_label = None            # type: Optional[ttk.Label]
        self._wait_prog = None             # type: Optional[ttk.Label]
        self._wait_bar = None              # type: Optional[ttk.Progressbar]
        self._wait_cancellable = False
        self._wait_gen = 0                 # 等待窗代数：残留 _hide_wait 防误销毁新窗
        self._cancel = threading.Event()   # 手动同步的取消信号
        self._sched_cancel = threading.Event()  # 调度触发的 worker 取消信号（退出时置位）
        self._prog_count = 0
        self._last_prog_ts = 0.0           # P3: 进度节流上次投递时间戳
        self._closing = False
        self._tick_id = None               # type: Optional[str]
        self._drain_id = None              # type: Optional[str]
        self._ui_queue = queue.Queue()     # type: queue.Queue  # worker -> UI 的唯一通道
        # 手动同步 worker（diff/apply）登记表：on_close 与调度 worker 一起
        # 有界等待。此前手动线程游离在 wait_workers 之外，进程退出时被强杀，
        # 大文件复制中途被杀会留下半截文件
        self._workers = []                 # type: List[threading.Thread]
        self._workers_lock = threading.Lock()
        # 手动同步全局门闩：_cancel/_wait 为单例，跨任务并发手动同步会互相
        # 干扰（后者 clear 掉前者的取消标志 / 销毁前者的等待窗）。
        # 运行槽只防同一任务并发，这里补跨任务互斥
        self._manual_busy = False
        # 最小化后台运行 / 托盘相关状态
        self._tray = None            # type: Optional[tray_mod.TrayIcon]
        self._tray_hidden = False    # 已隐藏到托盘（防止 withdraw 触发 <Unmap> 死循环）
        self._quitting = False       # 真正退出标志（托盘菜单/菜单栏"退出"置位）

        self._build_ui()
        self.logger.add_callback(self._on_log)

        self._load_log_history()
        self._refresh_tasks(full=True)
        self._maybe_autostart()
        # 最小化后台运行：菜单栏（全平台）+ 托盘图标（仅 Windows 可用）
        self._build_menu()
        if tray_mod.is_supported():
            self._init_tray()
            # Windows 最小化（-）→ 隐藏到托盘（任务栏不留入口）
            self.root.bind("<Unmap>", self._on_unmap)
        # 开机自启入口：以后台运行形态启动（不弹主窗口）
        if autostart:
            self.root.after(200, self._hide_to_background)
        self._drain_id = self.root.after(100, self._drain_ui_queue)
        self._tick_id = self.root.after(1000, self._tick)

    # ---------- UI 队列（worker 线程安全） ----------
    def _ui_put(self, fn):
        # type: (object) -> None
        """把一个"在主线程执行"的调用投递进队列。任意线程可安全调用。"""
        self._ui_queue.put(fn)

    def _drain_ui_queue(self):
        # type: () -> None
        # 顶部续期：即使本批次 fn() 抛异常或进入模态嵌套主循环（wait_window），
        # drain 链也不会断——嵌套主循环同样处理 after 事件，日志面板持续刷新，
        # 且 on_close 期间入队的 _finish_close 仍会被投递（避免窗口看似卡死）。
        # 窗口销毁后 root.after 抛 TclError，链自然终止。
        try:
            self._drain_id = self.root.after(100, self._drain_ui_queue)
        except tk.TclError:
            return
        for _ in range(100):
            try:
                fn = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                # 记录而非吞掉：否则运行槽泄漏、任务永久卡"运行中"等问题会被静默掩盖
                import traceback
                try:
                    self.logger.error("UI 回调执行异常: " + traceback.format_exc())
                except Exception:
                    pass

    # ---------- UI 构建 ----------
    def _build_ui(self):
        # type: () -> None
        self.root.title("文件夹同步备份工具  v1.1")
        self.root.geometry("900x620")

        top = ttk.Frame(self.root)
        top.pack(fill=tk.X)
        ttk.Button(top, text="新增任务", command=self._on_add).pack(side=tk.LEFT, padx=3, pady=4)
        ttk.Button(top, text="编辑", command=self._on_edit).pack(side=tk.LEFT, padx=3, pady=4)
        ttk.Button(top, text="删除", command=self._on_delete).pack(side=tk.LEFT, padx=3, pady=4)
        ttk.Button(top, text="立即同步", command=self._on_sync_now).pack(side=tk.LEFT, padx=3, pady=4)
        self._sched_btn = ttk.Button(top, text="启动调度", command=self._toggle_scheduler)
        self._sched_btn.pack(side=tk.LEFT, padx=3, pady=4)
        ttk.Button(top, text="打开日志目录", command=self._open_logs).pack(side=tk.LEFT, padx=3, pady=4)

        cols = ("name", "mode", "source", "target", "next", "status", "last")
        # 任务列表可能很多行：Treeview + 垂直滚动条，保证可滚动查看全部
        list_frame = ttk.Frame(self.root)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self.tree = ttk.Treeview(list_frame, columns=cols, show="headings", selectmode="browse")
        tree_sb = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_sb.set)
        tree_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(fill=tk.BOTH, expand=True)
        self.tree.heading("name", text="名称")
        self.tree.heading("mode", text="方向")
        self.tree.heading("source", text="源目录")
        self.tree.heading("target", text="目标目录")
        self.tree.heading("next", text="下次运行")
        self.tree.heading("status", text="状态")
        self.tree.heading("last", text="上次运行")
        self.tree.column("name", width=120)
        self.tree.column("mode", width=80)
        self.tree.column("source", width=180)
        self.tree.column("target", width=180)
        self.tree.column("next", width=130)
        self.tree.column("status", width=90)
        self.tree.column("last", width=130)

        self._status_label = ttk.Label(self.root, text="调度器：已停止", foreground="#333")
        self._status_label.pack(anchor=tk.W, padx=8)

        log_frm = ttk.LabelFrame(self.root, text="运行日志", padding=4)
        log_frm.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))
        self.log_text = scrolledtext.ScrolledText(log_frm, height=10, state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True)

    # ---------- 任务列表 ----------
    def _refresh_tasks(self, full=False):
        # type: (bool) -> None
        # full=False（默认）：仅更新 next/status/last 三列，不删行，保留选中态，避免闪烁。
        # full=True：任务增删改/调度启停时整体重建树。
        running = self.scheduler.running
        self._status_label.config(text="调度器：%s" % ("运行中" if running else "已停止"))
        self._sched_btn.config(text="停止调度" if running else "启动调度")
        if full:
            self.tree.delete(*self.tree.get_children())
        # 用快照迭代（与 scheduler 一致）：直接迭代 store.tasks 时若未来有
        # worker 线程增删任务，会抛 list changed size during iteration
        for t in self.store.snapshot():
            status = "运行中" if self.scheduler.is_task_running(t.id) else (
                "已禁用" if not t.enabled else (t.last_status or "-"))
            if full:
                self.tree.insert("", tk.END, iid=t.id, values=(
                    t.name,
                    _MODE_LABEL.get(t.mode, t.mode),
                    t.source,
                    t.target,
                    format_epoch(t.next_run),
                    status,
                    format_epoch(t.last_run),
                ))
            elif self.tree.exists(t.id):
                self.tree.set(t.id, "next", format_epoch(t.next_run))
                self.tree.set(t.id, "status", status)
                self.tree.set(t.id, "last", format_epoch(t.last_run))

    def _selected_task(self):
        # type: () -> Optional[Task]
        sel = self.tree.selection()
        if not sel:
            return None
        return self.store.get(sel[0])

    # ---------- 任务增删改 ----------
    def _on_add(self):
        # type: () -> None
        from gui_task_dialog import TaskDialog
        dlg = TaskDialog(self.root, None, self.store)
        self.root.wait_window(dlg)
        if dlg.result is not None:
            self.store.add(dlg.result)
            self._refresh_tasks(full=True)
            self._maybe_autostart()

    def _on_edit(self):
        # type: () -> None
        from gui_task_dialog import TaskDialog
        task = self._selected_task()
        if task is None:
            messagebox.showinfo("提示", "请先选择要编辑的任务")
            return
        # M-1：对话框生命周期内占运行槽，避免弹窗期间调度器并发启动该任务（check-then-act 竞态）
        if not self.scheduler.acquire(task.id):
            messagebox.showinfo("提示", "该任务正在运行中，请等待完成后再编辑")
            return
        try:
            dlg = TaskDialog(self.root, task, self.store)
            self.root.wait_window(dlg)
            if dlg.result is not None:
                # 编辑模式复用既有 Task 对象：先置 next_run=None 再 update 发布，
                # 避免对已发布给调度线程的对象做无锁跨线程写（违反 scheduler
                # _next_lock 协议；置 None 幂等无害，但顺序上先改后发最干净）
                dlg.result.next_run = None  # 重置，让调度器按新配置重算下次触发
                self.store.update(dlg.result)
                self._refresh_tasks(full=True)
                # 与新增任务一致：编辑后若存在启用的定时任务则自动拉起调度器
                self._maybe_autostart()
        finally:
            self.scheduler.release(task.id)

    def _on_delete(self):
        # type: () -> None
        task = self._selected_task()
        if task is None:
            messagebox.showinfo("提示", "请先选择要删除的任务")
            return
        # M-1：确认框 + 删除期间占运行槽，避免与调度器并发运行/改写同一任务
        if not self.scheduler.acquire(task.id):
            messagebox.showinfo("提示", "该任务正在运行中，请等待完成后再删除")
            return
        try:
            if messagebox.askyesno("确认", "确定删除任务 '%s'？" % task.name):
                self.store.remove(task.id)
                self._refresh_tasks(full=True)
        finally:
            self.scheduler.release(task.id)

    # ---------- 同步预览/执行 ----------
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
                messagebox.showerror("错误", "同步已中止：%s" % task.last_summary)
                return
            if res["diff"].is_empty():
                # 无差异：无需确认，直接收尾（所见即所得——0 个动作无可执行）
                self._release_manual(task.id)
                self._refresh_tasks(full=False)
                messagebox.showinfo("提示", "无需同步（无差异）")
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
                messagebox.showinfo("提示", "无需同步（无差异）")
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
                messagebox.showerror("错误", "操作失败：%s" % e)
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

    # ---------- 调度器回调（工作线程中调用） ----------
    def _run_task(self, task):
        # type: (Task) -> None
        try:
            # 传 _sched_cancel：退出流程置位后，调度触发的 worker 同样可取消
            # 大文件复制（此前仅手动 worker 传 _cancel，调度 worker 靠 5s 有界
            # join 强杀，仍可能留半截 .tmp~ 残留）
            res = perform_sync(task, logger=self.logger, self_paths=self.self_paths,
                               cancel_event=self._sched_cancel)
            finalize_sync(task, res, self.store, self.logger)
        except ScanCancelled:
            self.logger.warn("任务[%s] 已取消" % task.name)
            task.last_run = time.time()
            task.last_status = "已取消"
            task.last_summary = "用户退出/取消"
            try:
                self.store.update_runtime(task)
            except Exception:
                pass
        except Exception as e:
            self.logger.error("任务执行异常 [%s]: %s" % (task.name, e))
            # 异常路径同样落盘失败状态：否则界面与 tasks.json 里停留上一次的
            # "成功"，调度持续失败时用户无从察觉
            task.last_run = time.time()
            task.last_status = "失败"
            task.last_summary = "执行异常: %s" % e
            try:
                self.store.update_runtime(task)
            except Exception:
                pass
        finally:
            self._ui_put(self._refresh_tasks)

    def _toggle_scheduler(self):
        # type: () -> None
        if self.scheduler.running:
            self.scheduler.stop()
        else:
            self.scheduler.start()
        self._refresh_tasks(full=True)

    def _maybe_autostart(self):
        # type: () -> None
        has_sched = any(t.schedule.enabled and t.enabled for t in self.store.tasks)
        if has_sched and not self.scheduler.running:
            self.scheduler.start()
            self._refresh_tasks(full=True)

    # ---------- 日志面板 ----------
    def _on_log(self, level, line):
        # type: (str, str) -> None
        # logger 回调可能在任意线程：只入队，主线程渲染
        self._ui_put(lambda: self._append_log(line, level))

    def _append_log(self, line, level):
        # type: (str, str) -> None
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, line + "\n")
        # 限制行数，避免长时间运行内存无限增长
        if float(self.log_text.index(tk.END)) > 2000:
            self.log_text.delete("1.0", "100.0")
        self.log_text.configure(state=tk.DISABLED)
        self.log_text.see(tk.END)

    def _load_log_history(self):
        # type: () -> None
        path = os.path.join(LOG_DIR, "foldersync.log")
        if not os.path.exists(path):
            return
        try:
            lp = longpath(path)
            with open(lp, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-200:]
            self.log_text.configure(state=tk.NORMAL)
            for ln in lines:
                self.log_text.insert(tk.END, ln)
            self.log_text.configure(state=tk.DISABLED)
            self.log_text.see(tk.END)
        except OSError:
            pass

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

    # ---------- 杂项 ----------
    def _open_logs(self):
        # type: () -> None
        d = LOG_DIR
        if sys.platform == "win32":
            os.startfile(d)  # type: ignore
        else:
            try:
                import subprocess
                subprocess.Popen(["xdg-open", d])
            except Exception:
                messagebox.showinfo("日志目录", d)

    def _tick(self):
        # type: () -> None
        if self._closing:
            return
        try:
            self._refresh_tasks(full=False)
        except Exception:
            # 兜底：_refresh_tasks 抛异常（如窗口销毁竞态）不能让 after 链中断，
            # 否则界面状态永久停更且无日志；记录后继续续期
            import traceback
            try:
                self.logger.error("刷新任务列表异常: " + traceback.format_exc())
            except Exception:
                pass
        try:
            self._tick_id = self.root.after(1000, self._tick)
        except tk.TclError:
            pass

    # ---------- 菜单栏 / 托盘 / 最小化后台运行 ----------
    def _build_menu(self):
        # type: () -> None
        """菜单栏「文件」：最小化到后台 / 开机自启 / 退出（全平台可用）。"""
        menubar = tk.Menu(self.root)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="最小化到后台", command=self._hide_to_background)
        self._autostart_var = tk.BooleanVar(value=autostart.is_enabled())
        file_menu.add_checkbutton(label="开机自启", variable=self._autostart_var,
                                  command=self._toggle_autostart)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self._request_quit)
        menubar.add_cascade(label="文件", menu=file_menu)
        self.root.config(menu=menubar)

    def _toggle_autostart(self):
        # type: () -> None
        # 菜单栏勾选项：勾选即注册、取消即反注册；失败回滚勾选并提示
        want = self._autostart_var.get()
        ok = autostart.enable() if want else autostart.disable()
        if not ok:
            self._autostart_var.set(not want)
            messagebox.showerror("错误", "设置开机自启失败，请检查权限或手动配置")
            return
        self.logger.info("开机自启已%s" % ("启用" if want else "关闭"))

    def _init_tray(self):
        # type: () -> None
        """创建托盘图标（失败不致命：降级为任务栏最小化）。"""
        try:
            self._tray = tray_mod.TrayIcon(
                "文件夹同步备份工具",
                menu=[(1, "显示主窗口"), (2, "退出")],
                on_menu=self._on_tray_menu,
                on_activate=self._restore_from_tray,
                icon_path=os.path.join(APP_DIR, "app.ico"),
                main_hwnd=self.root.winfo_id())  # 主窗口句柄，托盘菜单关闭后还焦点
        except Exception as e:
            self._tray = None
            self.logger.warn("托盘图标创建失败（降级为任务栏最小化）: %s" % e)

    def _on_tray_menu(self, item_id):
        # type: (int) -> None
        # 托盘回调在 Windows 消息泵（主线程）中执行，可直接调 tkinter
        if item_id == 1:
            self._restore_from_tray()
        elif item_id == 2:
            self._request_quit()

    def _hide_to_background(self):
        # type: () -> None
        """最小化后台运行：Windows 隐藏到托盘；非 Windows 最小化到任务栏。

        只隐藏窗口，**不**停止调度器、不退出进程——后台继续按计划同步。
        """
        if self._closing:
            return
        if self._tray is not None:
            self._tray_hidden = True
            try:
                self.root.withdraw()
            except tk.TclError:
                self._tray_hidden = False
        else:
            # 非 Windows 无托盘：弹确认框告知后台运行状态与退出路径
            self._ask_minimize_or_quit()

    def _ask_minimize_or_quit(self):
        # type: () -> None
        """非 Windows 平台：点 X 后弹三选一对话框，明确退出路径。

        - 最小化到后台（默认）：iconify，调度器照常
        - 退出程序：置位后走完整关闭流程
        - 取消：保持窗口前台
        """
        if self._closing:
            return
        try:
            ans = messagebox.askyesnocancel(
                "最小化到后台",
                "点关闭按钮不会退出程序，而是最小化到后台继续运行。\n\n"
                "• 任务栏图标可恢复窗口\n"
                "• 后台期间调度器照常工作\n"
                "• 恢复窗口后从菜单栏「文件→退出」真正退出\n\n"
                "最小化到后台？（选「否」直接退出程序，选「取消」保持窗口）",
                icon="question",
                default="yes")
        except tk.TclError:
            return
        if ans is True:                 # Yes → 最小化到后台
            try:
                self.root.iconify()
            except tk.TclError:
                pass
        elif ans is False:              # No → 退出程序
            self._request_quit()
        # None（Cancel）→ 保持窗口前台，什么都不做

    def _restore_from_tray(self):
        # type: () -> None
        """从托盘恢复主窗口（左键单击 / 菜单"显示主窗口"）。"""
        self._tray_hidden = False
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except tk.TclError:
            pass
        self._refresh_tasks(full=True)

    def _request_quit(self):
        # type: () -> None
        """真正退出（托盘菜单/菜单栏"退出"）：置位后走完整关闭流程。"""
        self._quitting = True
        self.on_close()

    def _on_unmap(self, event):
        # type: (object) -> None
        # Windows 最小化（-）时触发 <Unmap>：隐藏到托盘。
        # 需判 state()=="iconic"：withdraw 也会触发 <Unmap>，但状态为
        # withdrawn，且 _tray_hidden 已置位防重入。
        if self._closing or self._tray is None or self._tray_hidden:
            return
        try:
            if self.root.state() == "iconic":
                self._hide_to_background()
        except tk.TclError:
            pass

    def _join_workers_bounded(self):
        # type: () -> None
        """有界等待调度 worker 与手动同步 worker 结束（退出流程用）。"""
        self.scheduler.wait_workers(5)
        # 手动同步 worker（diff/apply）同样有界等待：此前仅等调度
        # worker，手动线程游离在 wait_workers 之外，进程退出时被强杀，
        # 大文件复制中途被杀会留下半截 .tmp~ 残留
        deadline = time.time() + 5
        with self._workers_lock:
            workers = list(self._workers)
        for th in workers:
            remain = deadline - time.time()
            if remain <= 0:
                break
            try:
                th.join(timeout=remain)
            except Exception:
                pass

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
