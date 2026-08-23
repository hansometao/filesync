"""主窗口：任务列表 + 工具栏 + 日志面板，串联调度器与同步流程。

模块结构（第七轮拆分）
--------------------
App 由三个职责混人组成，本文件只保留"常驻 UI 核心"：
- gui_workers.SyncFlowMixin  手动同步全流程 + 等待窗（进度/取消）
- gui_tray.TrayMenuMixin     菜单栏 / 托盘 / 最小化后台运行 / 恢复
- gui_close.CloseSeqMixin    X 转后台判定与完整退出时序
拆分仅为可维护性：运行期仍是同一个 App 实例，混人通过宿主状态契约
（各模块 docstring 列明）读写 self.* 属性。

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
from typing import List, Optional

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

from config import (
    Task, TaskStore, MODE_ONE_WAY, MODE_TWO_WAY,
    sync_identity_changed,
)
from scheduler import Scheduler
from sync_engine import perform_sync, finalize_sync
from scanner import ScanCancelled
from logger import init_logger
from utils.paths import longpath, app_dir
from utils.timeutil import format_epoch
import autostart
import tray as tray_mod
from main import APP_VERSION

from gui_close import CloseSeqMixin
from gui_tray import TrayMenuMixin
from gui_workers import SyncFlowMixin

APP_DIR = app_dir()
LOG_DIR = os.path.join(APP_DIR, "logs")
CONFIG_PATH = os.path.join(APP_DIR, "config", "tasks.json")

_MODE_LABEL = {MODE_ONE_WAY: "单向镜像", MODE_TWO_WAY: "双向同步"}


class App(SyncFlowMixin, TrayMenuMixin, CloseSeqMixin):
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
        self.root.title("文件夹同步备份工具  v%s" % APP_VERSION)
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
        # 右键快捷菜单：单任务启用/禁用（免进编辑对话框）
        self.tree.bind("<Button-3>", self._on_task_context_menu)

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
            # 编辑模式 TaskDialog 原位改写 task 字段：先快照同步身份
            # （源/目标/方向），保存后据此判定是否需要作废 baseline
            prev_src = task.source
            prev_dst = task.target
            prev_mode = task.mode
            dlg = TaskDialog(self.root, task, self.store)
            self.root.wait_window(dlg)
            if dlg.result is not None:
                # 编辑模式复用既有 Task 对象：先置 next_run=None 再 update 发布，
                # 避免对已发布给调度线程的对象做无锁跨线程写（违反 scheduler
                # _next_lock 协议；置 None 幂等无害，但顺序上先改后发最干净）
                dlg.result.next_run = None  # 重置，让调度器按新配置重算下次触发
                # H1：身份变更后旧 baseline 是旧路径对的一致性快照，沿用会把
                # 新目标侧文件误分类（removed/modified）——双向删除开启时源侧
                # 被普通 delete 无备份删除、目标侧异容同名文件被无备份覆盖、
                # 多余文件反向拷入源。作废后下次按首同步语义重分类（同内容
                # no-op；异内容走冲突流程先备份）。顺序：先落盘清空基线再
                # update 发布新路径——若中途崩溃，状态是"旧路径+空基线"，
                # 首同步语义安全；反序则会把危险组合留在磁盘上。
                if sync_identity_changed(prev_src, prev_dst, prev_mode,
                                         dlg.result.source,
                                         dlg.result.target, dlg.result.mode):
                    dlg.result.baseline = {}
                    self.store.save_baseline(dlg.result)
                    self.logger.info(
                        "任务[%s] 同步身份已变更(源/目标/方向)，baseline 已作废，"
                        "下次同步按首同步重分类" % dlg.result.name)
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
    def _on_task_context_menu(self, event):
        # type: (object) -> None
        """任务列表右键菜单：选中行 + 启用/禁用切换。

        与编辑路径同协议：先占运行槽再改字段，避免与调度器并发
        （禁用瞬间正在运行的任务由引擎锁内 enabled 复查兜底）。
        """
        iid = self.tree.identify_row(event.y)  # type: ignore[attr-defined]
        if not iid:
            return
        task = self.store.get(iid)
        if task is None:
            return
        try:
            self.tree.selection_set(iid)
        except tk.TclError:
            pass
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(
            label="禁用任务" if task.enabled else "启用任务",
            command=lambda: self._toggle_task_enabled(task.id))
        try:
            menu.tk_popup(event.x_root, event.y_root)  # type: ignore[attr-defined]
        finally:
            menu.grab_release()

    def _toggle_task_enabled(self, task_id):
        # type: (str) -> None
        """切换任务启用状态并持久化（运行槽协议与 _on_edit 一致）。"""
        task = self.store.get(task_id)
        if task is None:
            return
        if not self.scheduler.acquire(task.id):
            messagebox.showinfo("提示", "该任务正在运行中，请等待完成后再%s" %
                                ("禁用" if task.enabled else "启用"))
            return
        try:
            task.enabled = not task.enabled
            # 与编辑路径一致：先置空 next_run 再 update 发布，调度器按新
            # 状态重算下次触发（禁用后不再轮询；重新启用按既有 last_run
            # 锚点判定是否补跑）
            task.next_run = None
            self.store.update(task)
            self.logger.info("任务[%s] 已%s" % (
                task.name, "启用" if task.enabled else "禁用"))
        finally:
            self.scheduler.release(task.id)
        self._refresh_tasks(full=True)

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
