"""主窗口：品牌栏 + 工具栏 + 卡片式任务列表 + 运行日志 + 底部状态栏。

模块结构：
- gui_workers.SyncFlowMixin  手动同步全流程 + 等待窗（进度/取消）
- gui_tray.TrayMenuMixin     菜单栏 / 托盘 / 最小化后台运行 / 恢复
- gui_close.CloseSeqMixin    X 转后台判定与完整退出时序

线程模型：
worker 线程（diff/apply/调度执行）**不直接调用任何 tkinter API**：
一切 UI 更新通过 `_ui_queue` 投递，主线程每 100ms 由 `_drain_ui_queue`
统一执行。这是对 tkinter 非线程安全的根本性规避。
"""

import os
import sys
import time
import queue
import threading
from typing import Any, Dict, List, Optional, Set

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
from gui_layout import LayoutMixin, BTN_ADD_TEXT
from gui_tasklist import (  # noqa: F401  (配色常量被本模块 UI 构建引用)
    TaskCard,
    _MODE_LABEL,
    _short_path,
    C_BRAND, C_BRAND_DARK, C_BRAND_LIGHT,
    C_DELETE, C_WARN, C_OK,
    C_BG, C_CARD_BG, C_CARD_BG_ALT,
    C_TEXT, C_TEXT_MUTED, C_TEXT_DISABLED,
    C_BORDER, C_SWITCH_OFF, C_SWITCH_ON,
    C_WHITE, C_BRAND_SUB, C_LOG_ERROR, C_LOG_WARN, C_DELETE_HOVER,
)

APP_DIR = app_dir()
LOG_DIR = os.path.join(APP_DIR, "logs")
CONFIG_PATH = os.path.join(APP_DIR, "config", "tasks.json")

# 会吞掉字符按键的输入控件（tk 与 ttk 变体）。root 级快捷键在这些控件
# 获得焦点时必须让路，否则搜索框里按 Delete 会删掉任务、对话框里按 Ctrl+F
# 会把焦点抢回主窗口导致键盘输入串位。详见 App._in_text_input。
_TEXT_INPUT_WIDGETS = frozenset((
    "Entry", "Text", "Spinbox", "Combobox",
    "TEntry", "TSpinbox", "TCombobox",
))


# ======================================================================
#  App — 主应用
# ======================================================================
class App(SyncFlowMixin, TrayMenuMixin, CloseSeqMixin, LayoutMixin):
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
        self._wait_gen = 0
        self._cancel = threading.Event()
        self._sched_cancel = threading.Event()
        self._prog_count = 0
        self._last_prog_ts = 0.0
        self._closing = False
        self._tick_id = None               # type: Optional[str]
        self._drain_id = None              # type: Optional[str]
        self._draining = False             # _drain_ui_queue 防重入守卫
        self._ui_queue = queue.Queue()  # type: queue.Queue[Any]

        self._workers = []                 # type: List[threading.Thread]
        self._workers_lock = threading.Lock()
        self._manual_busy = False
        self._batch_busy = False            # "运行全部"触发期防重入

        self._tray = None            # type: Optional[tray_mod.TrayIcon]
        self._tray_hidden = False
        self._quitting = False

        # ---- 卡片列表相关（替代 self.tree） ----
        self._task_canvas = None       # type: Optional[tk.Canvas]
        self._task_inner = None        # type: Optional[ttk.Frame]
        self._task_rows = {}           # type: Dict[str, TaskCard]
        self._selected_ids = set()      # type: set  # 多选集合（Ctrl+Click 追加/移除）
        self._selected_id = None        # type: Optional[str]  # 向后兼容：最后一个选中
        self._empty_lbl = None         # type: Optional[tk.Frame]  # 空态容器（图标+标题+副文案）
        self._dark_mode = False        # type: bool  # 深色模式开关

        self._build_ui()
        self.logger.add_callback(self._on_log)

        self._load_log_history()
        self._refresh_tasks(full=True)
        self._maybe_autostart()
        self._build_menu()
        if tray_mod.is_supported():
            self._init_tray()
            self.root.bind("<Unmap>", self._on_unmap)
        if autostart:
            self.root.after(200, self._hide_to_background)
        self._drain_id = self.root.after(100, self._drain_ui_queue)
        self._tick_id = self.root.after(1000, self._tick)

    # ---------- UI 队列 ----------
    def _ui_put(self, fn):
        # type: (object) -> None
        self._ui_queue.put(fn)

    def _drain_ui_queue(self):
        # type: () -> None
        try:
            self._drain_id = self.root.after(100, self._drain_ui_queue)
        except tk.TclError:
            return
        # 防重入守卫：wait_window/grab_set 等会进入嵌套事件循环，此时
        # 已排定的 after 仍会触发，两个 drain 交错消费同一队列会导致
        # 回调乱序（如 hide_wait 先于 popup 执行）。嵌套期间跳过本轮，
        # 外层 drain 返回后下一轮继续处理——保持单一消费者顺序。
        if self._draining:
            return
        self._draining = True
        try:
            for _ in range(100):
                try:
                    fn = self._ui_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    fn()
                except Exception:
                    import traceback
                    try:
                        self.logger.error("UI 回调执行异常: " + traceback.format_exc())
                    except Exception:
                        pass
        finally:
            self._draining = False

    # ==================================================================
    #  UI 构建（编排）：品牌栏/工具栏/内容区/状态栏搭建在 LayoutMixin
    # ==================================================================
    def _build_ui(self):
        # type: () -> None
        self.root.title("filesync v%s — 定时文件同步工具" % APP_VERSION)
        self.root.geometry("960x640")
        # 最小尺寸：防止用户压缩过小导致工具栏/卡片按钮挤压换行
        self.root.minsize(760, 480)
        self.root.configure(bg=C_BG)

        self._setup_style()
        self._setup_layout()
        self._setup_shortcuts()

    # ==================================================================
    #  键盘快捷键
    # ==================================================================
    def _setup_shortcuts(self):
        # type: () -> None
        """全局键盘快捷键：Ctrl+N 新建 / Ctrl+R 运行 / Delete 删除 / Ctrl+F 搜索 / Ctrl+D 深色模式。"""
        self.root.bind("<Control-n>", lambda e: self._on_shortcut_add())
        self.root.bind("<Control-r>", lambda e: self._on_shortcut_run())
        self.root.bind("<Delete>", lambda e: self._on_shortcut_delete())
        self.root.bind("<Control-f>", lambda e: self._focus_search())
        self.root.bind("<Control-d>", lambda e: self._on_shortcut_dark())

    def _in_text_input(self):
        # type: () -> bool
        """当前键盘焦点是否落在会吞字符的输入控件上。

        root 级 bind 落在 Toplevel bindtag，而该 tag 排在控件 class
        bindtag 之后：tk 的 Entry/Text 处理 Delete 等键后不 break，事件
        仍继续传播到 root，于是"搜索框里按 Delete 弹删除框""对话框里按
        Ctrl+F 把焦点抢回主窗口"成为可能（后者会把后续键盘输入打进错误
        控件）。故所有快捷键先过此守卫。

        按类名而非 isinstance 判定：ttk 变体类名跨 tkinter 版本不稳定，
        且无 tkinter 的无头测试环境下 isinstance 会 AttributeError。
        """
        try:
            w = self.root.focus_get()
        except Exception:
            return False
        if w is None:
            return False
        return type(w).__name__ in _TEXT_INPUT_WIDGETS

    def _on_shortcut_add(self):
        # type: () -> None
        if self._in_text_input():
            return
        self._on_add()

    def _on_shortcut_run(self):
        # type: () -> None
        """Ctrl+R：运行当前选中任务（无选中时提示）。"""
        if self._in_text_input():
            return
        task = self._selected_task()
        if task is None:
            messagebox.showinfo("提示", "请先选择要同步的任务")
            return
        self._run_card_task(task.id)

    def _on_shortcut_delete(self):
        # type: () -> None
        if self._in_text_input():
            return
        self._on_delete()

    def _on_shortcut_dark(self):
        # type: () -> None
        if self._in_text_input():
            return
        self._toggle_dark_mode()

    def _focus_search(self):
        # type: () -> None
        """Ctrl+F：聚焦搜索框并全选。"""
        if self._in_text_input():
            return
        if hasattr(self, "_search_entry") and self._search_entry is not None:
            try:
                self._search_entry.focus_set()
                self._search_entry.select_range(0, tk.END)
            except tk.TclError:
                pass

    # ==================================================================
    #  任务卡片列表刷新（替代原 self.tree 的 delete/insert/set）
    # ==================================================================
    def _refresh_tasks(self, full=False):
        # type: (bool) -> None
        running = self.scheduler.running
        self._status_label.config(
            text="调度器：%s" % ("运行中" if running else "已停止"))
        self._sched_btn.config(
            text="停止调度" if running else "启动调度")

        # 统计启用数
        tasks = list(self.store.snapshot())
        enabled_count = sum(1 for t in tasks if t.enabled)
        self._enabled_lbl.config(
            text="%d/%d 个已启用" % (enabled_count, len(tasks)))

        # 状态栏：任务总数
        if hasattr(self, "_task_count_lbl"):
            self._task_count_lbl.config(text="任务: %d" % len(tasks))

        # 状态栏：最近同步时间
        if hasattr(self, "_last_sync_lbl"):
            last_runs = [t.last_run for t in tasks if t.last_run]
            if last_runs:
                latest = max(last_runs)
                self._last_sync_lbl.config(
                    text="最近: %s" % format_epoch(latest))
            else:
                self._last_sync_lbl.config(text="")

        # 底部状态栏：显示下一次最近运行
        upcoming = [t for t in tasks
                    if t.enabled and t.schedule.enabled and t.next_run]
        if upcoming:
            upcoming.sort(key=lambda t: t.next_run or 0)
            nxt = upcoming[0]
            self._next_lbl_bar.config(
                text="下次: %s  [%s]" % (
                    format_epoch(nxt.next_run), nxt.name))
        else:
            self._next_lbl_bar.config(text="")

        if full:
            # 全量重建：销毁所有旧卡片，重新创建
            for c in self._task_rows.values():
                try:
                    c.destroy()
                except tk.TclError:
                    pass
            self._task_rows.clear()
            self._selected_ids.clear()
            self._selected_id = None
            assert self._task_inner is not None
            for t in tasks:
                card = TaskCard(self._task_inner, self, t.id)
                card.pack(fill=tk.X, padx=2, pady=3)
                self._task_rows[t.id] = card
                card.refresh(t)
            # 空态提示
            if not tasks:
                self._show_empty_state()
            else:
                self._hide_empty_state()
            # 重建后重应用过滤：否则增删改任务会让已隐藏的卡片全部重现
            self._apply_search_filter(self._current_search_kw())
        else:
            # 增量更新：更新已有卡片的内容，新增/删除卡片按需要处理
            existing = set(self._task_rows.keys())
            current = {t.id for t in tasks}
            membership_changed = existing != current

            # 删除已不存在的卡片
            for tid in existing - current:
                try:
                    self._task_rows[tid].destroy()
                except tk.TclError:
                    pass
                del self._task_rows[tid]
            # 同步剔除悬空选中（否则 _on_delete 取名会越界）
            self._prune_selection(current)

            # 更新或新增
            assert self._task_inner is not None
            for t in tasks:
                if t.id in self._task_rows:
                    self._task_rows[t.id].refresh(t)
                else:
                    card = TaskCard(self._task_inner, self, t.id)
                    card.pack(fill=tk.X, padx=2, pady=3)
                    self._task_rows[t.id] = card
                    card.refresh(t)
                    # 恢复选中态（新插入的）
                    if self._selected_id == t.id:
                        card.set_selected(True)

            # P0-6 修复：仅成员集变化时才重排（对齐 store 顺序）；
            # 每秒 tick 的常规刷新不再全量 pack_forget/pack，
            # 消除布局抖动与闪烁
            if membership_changed:
                for card in self._task_rows.values():
                    card.pack_forget()
                # 此处不自行 pack：可见性统一交给 _apply_search_filter。
                # 若在此无条件重排，增删任务会把搜索/筛选已隐藏的卡片
                # 全部重新显示出来（搜索看似"失效"）。该函数按
                # store.snapshot() 顺序 pack 可见项，顺序天然对齐。
                self._apply_search_filter(self._current_search_kw())

            if not tasks:
                self._show_empty_state()
            else:
                self._hide_empty_state()

        # 恢复选中态视觉
        for tid in self._selected_ids:
            if tid in self._task_rows:
                self._task_rows[tid].set_selected(True)

    def _show_empty_state(self):
        # type: () -> None
        if self._empty_lbl is None:
            assert self._task_inner is not None
            # 三层结构：大图标 + 标题 + 副文案（替代单行灰字，提升留白期观感）
            # P1: 文案指向实际按钮位置（工具栏左侧）+ 整块可点击直接新建
            holder = tk.Frame(self._task_inner, bg=C_BG)
            icon = tk.Label(holder, text="FS", font=("", 28, "bold"),
                            fg=C_BORDER, bg=C_BG)
            icon.pack(pady=(48, 8))
            title = tk.Label(holder, text="还没有同步任务",
                             font=("", 12, "bold"), fg=C_TEXT_MUTED, bg=C_BG)
            title.pack()
            sub = tk.Label(holder,
                           text="点击左上方「%s」或此处创建第一个任务" % BTN_ADD_TEXT,
                           font=("", 9), fg=C_TEXT_DISABLED, bg=C_BG)
            sub.pack(pady=(4, 40))
            # 整块可点击：图标/标题/副文案/容器任一点击都触发新建
            for w in (holder, icon, title, sub):
                w.bind("<Button-1>", lambda e: self._on_add())
            for w in (holder, icon, title, sub):
                try:
                    w.configure(cursor="hand2")
                except tk.TclError:
                    pass
            self._empty_lbl = holder
        self._empty_lbl.pack(fill=tk.X, padx=2)

    def _hide_empty_state(self):
        # type: () -> None
        if hasattr(self, "_empty_lbl") and self._empty_lbl is not None:
            try:
                self._empty_lbl.pack_forget()
            except tk.TclError:
                pass

    # ---------- 搜索/筛选 ----------
    def _on_search_focus_in(self, _evt=None):
        # type: (object) -> None
        if self._search_entry.get() == self._search_placeholder:
            self._search_entry.delete(0, tk.END)
            self._search_entry.config(fg=C_TEXT)

    def _on_search_focus_out(self, _evt=None):
        # type: (object) -> None
        if not self._search_entry.get().strip():
            self._search_entry.delete(0, tk.END)
            self._search_entry.insert(0, self._search_placeholder)
            self._search_entry.config(fg=C_TEXT_DISABLED)

    def _on_search(self):
        # type: () -> None
        kw = self._current_search_kw()
        # 一键清除（×）随有效输入显隐；占位插入发生在 _search_clear
        # 创建前（_setup_layout 内），getattr 兜底避免构建期 AttributeError
        clear = getattr(self, "_search_clear", None)
        if clear is not None:
            try:
                if kw:
                    clear.pack(side=tk.LEFT)
                else:
                    clear.pack_forget()
            except tk.TclError:
                pass
        self._apply_search_filter(kw)

    def _on_search_clear(self):
        # type: () -> None
        """清空搜索并恢复占位提示（write trace 会联动重置过滤与 × 显隐）。"""
        self._search_entry.delete(0, tk.END)
        self._search_entry.insert(0, self._search_placeholder)
        self._search_entry.config(fg=C_TEXT_DISABLED)

    def _apply_search_filter(self, keyword):
        # type: (str) -> None
        tasks = list(self.store.snapshot())
        kw = keyword.lower()
        filter_val = getattr(self, "_filter_var", None)
        status_filter = filter_val.get() if filter_val is not None else "全部"
        for t in tasks:
            if t.id not in self._task_rows:
                continue
            card = self._task_rows[t.id]
            match = True
            if kw and not (kw in t.name.lower() or kw in t.source.lower() or kw in t.target.lower()):
                match = False
            if status_filter == "启用" and not t.enabled:
                match = False
            elif status_filter == "禁用" and t.enabled:
                match = False
            elif status_filter == "运行中" and not self.scheduler.is_task_running(t.id):
                match = False
            elif status_filter == "失败" and t.last_status != "失败":
                match = False
            if match:
                card.pack(fill=tk.X, padx=2, pady=3)
            else:
                card.pack_forget()

    def _current_search_kw(self):
        # type: () -> str
        """当前生效的搜索关键词（占位提示视为空，去首尾空白）。"""
        var = getattr(self, "_search_var", None)
        if var is None:
            return ""
        kw = var.get().strip()
        if kw == getattr(self, "_search_placeholder", ""):
            return ""
        return kw

    def _on_filter_changed(self, *_):
        # type: (*object) -> None
        """筛选下拉变化时重新应用过滤（与搜索关键词联合过滤）。"""
        self._apply_search_filter(self._current_search_kw())

    # ==================================================================
    #  选中 / 右键菜单 / 卡片操作入口
    # ==================================================================
    def _select_card(self, task_id, add=False):
        # type: (str, bool) -> None
        """点击卡片选中。Ctrl+Click 时 add=True 追加/移除多选。"""
        if add:
            if task_id in self._selected_ids:
                self._selected_ids.discard(task_id)
                if task_id in self._task_rows:
                    self._task_rows[task_id].set_selected(False)
                if self._selected_id == task_id:
                    self._selected_id = None
            else:
                self._selected_ids.add(task_id)
                self._selected_id = task_id
                if task_id in self._task_rows:
                    self._task_rows[task_id].set_selected(True)
        else:
            for tid in list(self._selected_ids):
                if tid in self._task_rows and tid != task_id:
                    self._task_rows[tid].set_selected(False)
            self._selected_ids = {task_id}
            self._selected_id = task_id
            if task_id in self._task_rows:
                self._task_rows[task_id].set_selected(True)
        self._update_batch_bar()

    def _on_arrow(self, delta):
        # type: (int) -> None
        """↑/↓ 在卡片列表中移动选中（替代 Treeview 键盘导航）。"""
        # 与快捷键同根因：root 级 bind 会收到输入控件内的方向键
        if getattr(self, "_in_text_input", None) is not None \
                and self._in_text_input():
            return
        ids = [t.id for t in self.store.snapshot()]
        if not ids:
            return
        if self._selected_id not in ids:
            self._select_card(ids[0] if delta > 0 else ids[-1])
            return
        i = ids.index(self._selected_id)
        i = min(max(i + delta, 0), len(ids) - 1)
        self._select_card(ids[i])

    def _update_batch_bar(self):
        # type: () -> None
        """多选时显示批量操作栏（启用/禁用/运行/清除选择）。"""
        if not hasattr(self, "_batch_bar"):
            return
        if len(self._selected_ids) > 1:
            self._batch_count_lbl.config(text="已选 %d 个任务" % len(self._selected_ids))
            self._batch_bar.pack(fill=tk.X, side=tk.BOTTOM, padx=14, pady=(0, 4))
        else:
            self._batch_bar.pack_forget()

    def _batch_enable(self, enabled):
        # type: (bool) -> None
        """批量启用/禁用选中任务。"""
        for tid in list(self._selected_ids):
            task = self.store.get(tid)
            if task is None:
                continue
            if not self.scheduler.acquire(tid):
                continue
            try:
                task.enabled = enabled
                task.next_run = None
                self.store.update(task)
                self.logger.info("任务[%s] 已%s" % (task.name, "启用" if enabled else "禁用"))
            finally:
                self.scheduler.release(tid)
        self._refresh_tasks(full=False)

    def _batch_run(self):
        # type: () -> None
        """批量运行选中任务（逐个触发，跳过运行中）。"""
        fired = 0
        for tid in list(self._selected_ids):
            task = self.store.get(tid)
            if task is None or not task.enabled:
                continue
            if self.scheduler.run_now(tid):
                fired += 1
        if fired:
            self.logger.info("批量运行: 已触发 %d 个任务" % fired)
        self._refresh_tasks(full=False)

    def _clear_selection(self):
        # type: () -> None
        """清除多选。"""
        for tid in list(self._selected_ids):
            if tid in self._task_rows:
                self._task_rows[tid].set_selected(False)
        self._selected_ids.clear()
        self._selected_id = None
        self._update_batch_bar()

    def _prune_selection(self, valid_ids):
        # type: (Set[str]) -> None
        """剔除已消失任务的选中项。

        任务被 store 移除后卡片先销毁，但 _selected_ids 仍留着该 id
        （悬空 id）。悬空 id 会让 _on_delete 取名时 names 为空而越界，
        也会让批量操作对不存在的任务反复 acquire/release。
        """
        self._selected_ids &= set(valid_ids)
        if self._selected_id not in self._selected_ids:
            self._selected_id = min(self._selected_ids) if self._selected_ids else None
        self._update_batch_bar()

    def _selected_task(self):
        # type: () -> Optional[Task]
        """获取当前选中任务（与 Treeview 版签名一致）。"""
        tid = self._selected_id
        if tid is None and self._selected_ids:
            tid = sorted(self._selected_ids)[0]
        if tid is None:
            return None
        return self.store.get(tid)

    def _on_task_context_menu_card(self, task_id, event):
        # type: (str, object) -> None
        """卡片右键菜单。多选时右键未选中项则单选该项；已选中项则保持多选。"""
        task = self.store.get(task_id)
        if task is None:
            return
        if task_id not in self._selected_ids:
            self._select_card(task_id)
        menu = tk.Menu(self.root, tearoff=0)
        if len(self._selected_ids) > 1:
            menu.add_command(
                label="批量启用",
                command=lambda: self._batch_enable(True))
            menu.add_command(
                label="批量禁用",
                command=lambda: self._batch_enable(False))
            menu.add_command(
                label="批量运行",
                command=self._batch_run)
            menu.add_separator()
        else:
            menu.add_command(
                label="禁用任务" if task.enabled else "启用任务",
                command=lambda: self._toggle_card_enabled(task_id))
            menu.add_command(
                label="立即同步",
                command=lambda: self._run_card_task(task_id))
        menu.add_command(
            label="编辑",
            command=lambda: self._edit_card_task(task_id))
        menu.add_command(
            label="删除",
            command=lambda: self._delete_card_task(task_id))
        try:
            menu.tk_popup(event.x_root, event.y_root)  # type: ignore[attr-defined]
        finally:
            menu.grab_release()

    # ==================================================================
    #  卡片按钮回调（委托到原逻辑，保持 mixin 兼容）
    # ==================================================================
    def _run_card_task(self, task_id):
        # type: (str) -> None
        task = self.store.get(task_id)
        if task is None:
            return
        if not task.enabled:
            messagebox.showinfo("提示", "任务已禁用，请先启用再运行")
            return
        self._selected_ids = {task_id}
        self._selected_id = task_id
        if task_id in self._task_rows:
            self._task_rows[task_id].set_selected(True)
        self._on_sync_now()

    def _edit_card_task(self, task_id):
        # type: (str) -> None
        self._selected_ids = {task_id}
        self._selected_id = task_id
        if task_id in self._task_rows:
            self._task_rows[task_id].set_selected(True)
        self._on_edit()

    def _delete_card_task(self, task_id):
        # type: (str) -> None
        self._selected_ids = {task_id}
        self._selected_id = task_id
        if task_id in self._task_rows:
            self._task_rows[task_id].set_selected(True)
        self._on_delete()

    def _toggle_card_enabled(self, task_id):
        # type: (str) -> None
        if task_id not in self._selected_ids:
            self._select_card(task_id)
        self._toggle_task_enabled(task_id)

    # ==================================================================
    #  原有方法（签名不变，内部实现适配卡片列表）
    # ==================================================================
    def _on_add(self):
        # type: () -> None
        from gui_task_dialog import TaskDialog
        dlg = TaskDialog(self.root, None, self.store)
        self.root.wait_window(dlg)
        if dlg.result is not None:
            self.store.add(dlg.result)
            self._selected_ids = {dlg.result.id}
            self._selected_id = dlg.result.id
            self._refresh_tasks(full=True)
            self._maybe_autostart()

    def _on_edit(self):
        # type: () -> None
        from gui_task_dialog import TaskDialog
        task = self._selected_task()
        if task is None:
            messagebox.showinfo("提示", "请先选择要编辑的任务")
            return
        if not self.scheduler.acquire(task.id):
            messagebox.showinfo("提示", "该任务正在运行中，请等待完成后再编辑")
            return
        try:
            prev_src = task.source
            prev_dst = task.target
            prev_mode = task.mode
            dlg = TaskDialog(self.root, task, self.store)
            self.root.wait_window(dlg)
            if dlg.result is not None:
                dlg.result.next_run = None
                if sync_identity_changed(prev_src, prev_dst, prev_mode,
                                         dlg.result.source,
                                         dlg.result.target, dlg.result.mode):
                    dlg.result.baseline = {}
                    self.store.save_baseline(dlg.result)
                    self.logger.info(
                        "任务[%s] 同步身份已变更，baseline 已作废" % dlg.result.name)
                self.store.update(dlg.result)
                self._selected_ids = {task.id}
                self._selected_id = task.id
                self._refresh_tasks(full=True)
                if task.id in self._task_rows:
                    self._task_rows[task.id].set_selected(True)
                self._maybe_autostart()
        finally:
            self.scheduler.release(task.id)

    def _on_delete(self):
        # type: () -> None
        if not self._selected_ids:
            messagebox.showinfo("提示", "请先选择要删除的任务")
            return
        ids = list(self._selected_ids)
        names = []
        for tid in ids:
            t = self.store.get(tid)
            if t is not None:
                names.append(t.name)
        if not names:
            # 选中项全部悬空（任务已被其他路径移除）：无处可删，清选中即返回
            self._clear_selection()
            return
        if len(names) == 1:
            msg = "确定删除任务 '%s'？" % names[0]
        else:
            msg = "确定删除选中的 %d 个任务？\n%s" % (len(names), "、".join(names))
        if not messagebox.askyesno("确认", msg):
            return
        for tid in ids:
            if not self.scheduler.acquire(tid):
                continue
            try:
                self.store.remove(tid)
            finally:
                self.scheduler.release(tid)
        self._selected_ids.clear()
        self._selected_id = None
        self._refresh_tasks(full=True)

    def _toggle_task_enabled(self, task_id):
        # type: (str) -> None
        task = self.store.get(task_id)
        if task is None:
            return
        if not self.scheduler.acquire(task_id):
            messagebox.showinfo("提示", "该任务正在运行中，请等待完成后再%s" %
                                ("禁用" if task.enabled else "启用"))
            return
        try:
            task.enabled = not task.enabled
            task.next_run = None
            self.store.update(task)
            self.logger.info("任务[%s] 已%s" % (
                task.name, "启用" if task.enabled else "禁用"))
        finally:
            self.scheduler.release(task_id)
        if task_id in self._task_rows:
            self._task_rows[task_id].refresh(task)
        self._refresh_tasks(full=False)

    def _run_task(self, task):
        # type: (Task) -> None
        try:
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
        self._update_batch_bar()

    def _maybe_autostart(self):
        # type: () -> None
        has_sched = any(t.schedule.enabled and t.enabled for t in self.store.tasks)
        if has_sched and not self.scheduler.running:
            self.scheduler.start()
            self._refresh_tasks(full=True)
            self._update_batch_bar()

    # ==================================================================
    #  品牌栏按钮回调
    # ==================================================================
    def _on_run_all(self):
        # type: () -> None
        """运行所有已启用的任务。

        P0-1 修复：此前在主线程同步执行 perform_sync，批量运行期间
        GUI 完全冻结（无法取消/刷新）。改为逐个 scheduler.run_now 触发：
        - 每个 worker 由调度器管理（运行槽/异常兜底/next_run 推进）
        - 不阻塞主线程，卡片实时显示 ●运行中
        - run_now 自带幂等（运行中返回 False），双击不会重复触发
        """
        if self._batch_busy:
            return
        tasks = [t for t in self.store.snapshot() if t.enabled]
        if not tasks:
            messagebox.showinfo("提示", "没有已启用的任务")
            return
        self._batch_busy = True
        fired = skipped = 0
        try:
            for t in tasks:
                if self._closing:
                    break
                if self.scheduler.run_now(t.id):
                    fired += 1
                    self.logger.info("批量运行: 已触发任务[%s]" % t.name)
                else:
                    skipped += 1
        finally:
            self._batch_busy = False
        msg = "已触发 %d 个任务" % fired
        if skipped:
            msg += "，跳过 %d 个（运行中）" % skipped
        self._refresh_tasks(full=False)
        self._update_batch_bar()
        self._popup_if_alive("info", "批量运行", msg)

    def _on_settings(self):
        # type: () -> None
        """设置入口：对话框内提供打开日志目录 / 配置目录两个显式动作。"""
        dlg = tk.Toplevel(self.root)
        dlg.title("设置")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        dlg.grab_set()
        body = ttk.Frame(dlg, padding=16)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text="数据与日志", style="Title.TLabel").pack(anchor=tk.W)
        ttk.Label(body, text="任务配置与运行日志分别存放在以下目录，可点击打开：",
                  style="Muted.TLabel").pack(anchor=tk.W, pady=(2, 10))
        ttk.Button(body, text="打开日志目录", style="Outline.TButton",
                   command=self._open_logs).pack(fill=tk.X, pady=3)
        ttk.Button(body, text="打开配置目录", style="Outline.TButton",
                   command=self._open_config_dir).pack(fill=tk.X, pady=3)
        ttk.Button(body, text="关闭", style="Accent.TButton",
                   command=dlg.destroy).pack(fill=tk.X, pady=(10, 0))
        dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)

    def _open_config_dir(self):
        # type: () -> None
        cfg_dir = os.path.dirname(CONFIG_PATH)
        if sys.platform == "win32":
            try:
                os.startfile(cfg_dir)  # type: ignore
            except Exception:
                messagebox.showinfo("配置目录", cfg_dir)
        else:
            try:
                import subprocess
                subprocess.Popen(["xdg-open", cfg_dir])
            except Exception:
                messagebox.showinfo("配置目录", cfg_dir)

    # ==================================================================
    #  日志面板（与 Treeview 版相同，self.log_text 接口不变）
    # ==================================================================
    def _on_log(self, level, line):
        # type: (str, str) -> None
        self._ui_put(lambda: self._append_log(line, level))

    @staticmethod
    def _log_tag(source):
        # type: (str) -> Optional[str]
        """从级别（或日志行内容）推断着色 tag；普通行返回 None。

        仅匹配行首级别标签 [ERROR]/[WARN]，避免正文含"失败"等词
        （如"成功 5 项，失败 0 项"）被误标红。
        """
        s = source.strip()
        if s.startswith("[ERROR]") or s.startswith("[WARN]"):
            return "error" if "[ERROR]" in s[:10] else "warn"
        return None

    def _append_log(self, line, level):
        # type: (str, str) -> None
        tag = self._log_tag(level or line)
        self.log_text.configure(state=tk.NORMAL)
        if tag is not None:
            self.log_text.insert(tk.END, line + "\n", tag)
        else:
            self.log_text.insert(tk.END, line + "\n")
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
                tag = self._log_tag(ln)
                if tag is not None:
                    self.log_text.insert(tk.END, ln, tag)
                else:
                    self.log_text.insert(tk.END, ln)
            self.log_text.configure(state=tk.DISABLED)
            self.log_text.see(tk.END)
        except OSError:
            pass

    # ==================================================================
    #  杂项
    # ==================================================================
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
            import traceback
            try:
                self.logger.error("刷新任务列表异常: " + traceback.format_exc())
            except Exception:
                pass
        try:
            self._tick_id = self.root.after(1000, self._tick)
        except tk.TclError:
            pass
