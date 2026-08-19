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
import queue
import threading
from typing import Optional

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

from config import (
    Task, TaskStore, MODE_ONE_WAY, MODE_TWO_WAY, CONFLICT_ASK,
)
from scheduler import Scheduler
from sync_engine import perform_sync, apply_diff
from scanner import ScanCancelled
from logger import init_logger
from utils.paths import longpath, app_dir
from utils.timeutil import format_epoch

APP_DIR = app_dir()
LOG_DIR = os.path.join(APP_DIR, "logs")
CONFIG_PATH = os.path.join(APP_DIR, "config", "tasks.json")

_MODE_LABEL = {MODE_ONE_WAY: "单向镜像", MODE_TWO_WAY: "双向同步"}


class App(object):
    def __init__(self, root):
        # type: (tk.Tk) -> None
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
        self._wait_label = None
        self._wait_prog = None
        self._wait_bar = None
        self._wait_cancellable = False
        self._cancel = threading.Event()   # 手动同步的取消信号
        self._prog_count = 0
        self._closing = False
        self._tick_id = None               # type: Optional[str]
        self._drain_id = None              # type: Optional[str]
        self._ui_queue = queue.Queue()     # worker -> UI 的唯一通道

        self._build_ui()
        self.logger.add_callback(self._on_log)

        self._load_log_history()
        self._refresh_tasks(full=True)
        self._maybe_autostart()
        self._drain_id = self.root.after(100, self._drain_ui_queue)
        self._tick_id = self.root.after(1000, self._tick)

    # ---------- UI 队列（worker 线程安全） ----------
    def _ui_put(self, fn):
        # type: (object) -> None
        """把一个"在主线程执行"的调用投递进队列。任意线程可安全调用。"""
        self._ui_queue.put(fn)

    def _drain_ui_queue(self):
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
        # full=False（默认）：仅更新 next/status/last 三列，不删行，保留选中态，避免闪烁。
        # full=True：任务增删改/调度启停时整体重建树。
        running = self.scheduler.running
        self._status_label.config(text="调度器：%s" % ("运行中" if running else "已停止"))
        self._sched_btn.config(text="停止调度" if running else "启动调度")
        if full:
            self.tree.delete(*self.tree.get_children())
        for t in self.store.tasks:
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
        from gui_task_dialog import TaskDialog
        dlg = TaskDialog(self.root, None, self.store)
        self.root.wait_window(dlg)
        if dlg.result is not None:
            self.store.add(dlg.result)
            self._refresh_tasks(full=True)
            self._maybe_autostart()

    def _on_edit(self):
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
                self.store.update(dlg.result)
                dlg.result.next_run = None  # 重置，让调度器按新配置重算下次触发
                self._refresh_tasks(full=True)
        finally:
            self.scheduler.release(task.id)

    def _on_delete(self):
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
    def _on_sync_now(self):
        task = self._selected_task()
        if task is None:
            messagebox.showinfo("提示", "请先选择要同步的任务")
            return
        # README 承诺：禁用任务不参与定时，也不手动同步
        if not task.enabled:
            messagebox.showinfo("提示", "该任务已禁用，请先在编辑中启用后再同步")
            return
        # 从预览开始就占用运行槽：预览期间调度器不会并发触发，预览/执行一致
        if not self.scheduler.acquire(task.id):
            messagebox.showinfo("提示", "该任务正在运行中，请稍候")
            return
        self._cancel.clear()
        self._prog_count = 0
        self._show_wait("正在扫描并对比差异...", cancellable=True)
        threading.Thread(target=self._diff_worker, args=(task,), daemon=True).start()

    def _progress_cb(self, rel):
        # type: (str) -> None
        # worker 线程调用：节流后经 UI 队列刷新等待窗进度标签
        self._prog_count += 1
        if self._prog_count % 20 == 0:
            short = rel if len(rel) <= 60 else ("..." + rel[-57:])
            n = self._prog_count
            self._ui_put(lambda: self._set_wait_progress("已扫描 %d 项：%s" % (n, short)))

    def _diff_worker(self, task):
        # type: (Task) -> None
        try:
            res = perform_sync(task, logger=self.logger, self_paths=self.self_paths,
                               dry_run=True, progress=self._progress_cb,
                               cancel_event=self._cancel)
            self._ui_put(lambda: self._on_diff_ready(task, res))
        except ScanCancelled:
            self.scheduler.release(task.id)
            self._ui_put(self._hide_wait)
            self._ui_put(lambda: messagebox.showinfo("提示", "已取消"))
            self._ui_put(self._refresh_tasks)
        except Exception as e:
            self.logger.error("对比失败 [%s]: %s" % (task.name, e))
            self.scheduler.release(task.id)
            self._ui_put(self._hide_wait)
            self._ui_put(lambda: messagebox.showerror("错误", "对比失败：%s" % e))
            self._ui_put(self._refresh_tasks)

    def _on_diff_ready(self, task, res):
        # type: (Task, dict) -> None
        # 主线程执行（经 UI 队列）：关闭等待窗并弹出差异预览。
        # 任何异常都必须释放运行槽，否则任务永久显示"运行中"，编辑/删除/同步全被拒。
        try:
            self._hide_wait()
            from gui_diff import DiffDialog
            dlg = DiffDialog(self.root, res["diff"], task)
            self.root.wait_window(dlg)
            if dlg.result_policy is None:
                # 取消预览：释放运行槽
                self.scheduler.release(task.id)
                self._refresh_tasks(full=False)
                return
            if dlg.result_policy == "_noop_":
                self.scheduler.release(task.id)
                self._refresh_tasks(full=False)
                messagebox.showinfo("提示", "无需同步（无差异）")
                return
            policy = dlg.result_policy if dlg.result_policy != CONFLICT_ASK else None
            self._cancel.clear()  # 确保已确认的执行为干净运行，不被残留取消标志秒变 no-op
            self._show_wait("正在执行同步...", cancellable=True)
            threading.Thread(target=self._apply_worker, args=(task, res, policy), daemon=True).start()
        except Exception as e:
            self.logger.error("预览/执行准备异常 [%s]: %s" % (task.name, e))
            try:
                self.scheduler.release(task.id)
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
            # 逐文件审计轨迹（apply_diff 返回的动作日志），让「完整清单见日志」可查
            for ln in (out or {}).get("logs", []):
                self.logger.info(ln)
            self.store.update_runtime(task)
            self.store.save_baseline(task)
            self._ui_put(self._hide_wait)
            self._ui_put(lambda: messagebox.showinfo("完成", "同步完成：%s" % task.last_summary))
        except ScanCancelled:
            self.logger.warn("任务[%s] 已取消" % task.name)
            task.last_status = "已取消"
            self.store.update_runtime(task)
            self._ui_put(self._hide_wait)
            self._ui_put(lambda: messagebox.showinfo("提示", "已取消"))
        except Exception as e:
            self.logger.error("同步失败 [%s]: %s" % (task.name, e))
            self._ui_put(self._hide_wait)
            self._ui_put(lambda: messagebox.showerror("错误", "同步失败：%s" % e))
        finally:
            self.scheduler.release(task.id)
            self._ui_put(self._refresh_tasks)

    # ---------- 调度器回调（工作线程中调用） ----------
    def _run_task(self, task):
        # type: (Task) -> None
        try:
            res = perform_sync(task, logger=self.logger, self_paths=self.self_paths)
            for ln in (res or {}).get("logs", []):
                self.logger.info(ln)
            self.store.update_runtime(task)
            self.store.save_baseline(task)
        except Exception as e:
            self.logger.error("任务执行异常 [%s]: %s" % (task.name, e))
        finally:
            self._ui_put(self._refresh_tasks)

    def _toggle_scheduler(self):
        if self.scheduler.running:
            self.scheduler.stop()
        else:
            self.scheduler.start()
        self._refresh_tasks(full=True)

    def _maybe_autostart(self):
        has_sched = any(t.schedule.enabled and t.enabled for t in self.store.tasks)
        if has_sched and not self.scheduler.running:
            self.scheduler.start()
            self._refresh_tasks(full=True)

    # ---------- 日志面板 ----------
    def _on_log(self, level, line):
        # logger 回调可能在任意线程：只入队，主线程渲染
        self._ui_put(lambda: self._append_log(line, level))

    def _append_log(self, line, level):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, line + "\n")
        # 限制行数，避免长时间运行内存无限增长
        if float(self.log_text.index(tk.END)) > 2000:
            self.log_text.delete("1.0", "100.0")
        self.log_text.configure(state=tk.DISABLED)
        self.log_text.see(tk.END)

    def _load_log_history(self):
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
        if self._wait is not None:
            try:
                if self._wait.winfo_exists():
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
        if self._wait is not None and self._wait_prog is not None:
            try:
                self._wait_prog.config(text=text)
            except tk.TclError:
                pass

    def _on_wait_close(self):
        # 点 X 关闭等待窗：可取消则等同取消；不可取消则忽略关闭（保持模态不被误关）
        if self._wait_cancellable:
            self._on_cancel_wait()

    def _on_cancel_wait(self):
        self._cancel.set()
        if self._wait is not None and self._wait_prog is not None:
            try:
                self._wait_prog.config(text="正在取消...")
            except tk.TclError:
                pass

    def _hide_wait(self):
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
        if self._closing:
            return
        self._refresh_tasks(full=False)
        try:
            self._tick_id = self.root.after(1000, self._tick)
        except tk.TclError:
            pass

    def on_close(self):
        if self._closing:
            return
        self._closing = True
        # M-5：先隐藏主窗口，屏蔽关闭期间（最多约 6s）的交互，避免再触发新线程/对话框
        try:
            self.root.withdraw()
        except tk.TclError:
            pass
        self._cancel.set()
        self._hide_wait()
        self.scheduler.stop()
        # 等待收尾放到后台线程（有界），主线程保持事件循环处理 UI 队列；
        # 完成后经队列回到主线程销毁窗口，避免长时间冻结界面
        def _shutdown():
            try:
                self.scheduler.wait_workers(5)
            finally:
                self._ui_put(self._finish_close)
        threading.Thread(target=_shutdown, daemon=True).start()

    def _finish_close(self):
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
        try:
            self.root.destroy()
        except tk.TclError:
            pass
