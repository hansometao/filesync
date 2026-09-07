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
from typing import Dict, List, Optional, Any

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

# ---------- 配色变量（青绿色主题） ----------
C_BRAND = "#26A69A"           # 主色：青绿色（按钮/开关激活/状态对勾）
C_BRAND_DARK = "#1E8E84"      # 主色 hover
C_BRAND_LIGHT = "#E0F2F1"     # 主色淡底（卡片选中态）
C_DELETE = "#E57373"          # 删除按钮边框
C_WARN = "#FFB74D"            # 警告/待处理
C_OK = "#26A69A"              # 成功对勾（同主色）
C_BG = "#FAFAFA"              # 页面背景
C_CARD_BG = "#FFFFFF"         # 卡片背景
C_CARD_BG_ALT = "#F5F5F5"     # 卡片交替背景
C_TEXT = "#212121"            # 主文字
C_TEXT_MUTED = "#757575"      # 辅助文字（路径/调度信息）
C_TEXT_DISABLED = "#BDBDBD"   # 禁用态文字
C_BORDER = "#E0E0E0"          # 分隔线
C_SWITCH_OFF = "#BDBDBD"      # 开关关闭
C_SWITCH_ON = "#26A69A"       # 开关打开

# ---------- 工具：截断中间路径（保留首尾） ----------
def _short_path(p, max_len=36):
    # type: (str, int) -> str
    """过长路径截断为 首段...末段，例如 /home/user/.../data/file.txt"""
    if len(p) <= max_len:
        return p
    head = p[:max_len // 2 - 2]
    tail = p[-(max_len // 2 - 1):]
    return head + "..." + tail


# ======================================================================
#  TaskCard — 单张任务卡片（独立 Frame，可嵌入 Canvas 滚动容器）
# ======================================================================
class TaskCard(ttk.Frame):
    """一张任务卡片：开关 + 名称/路径 + 调度/上次 + 状态 + 操作按钮。

    所有交互回调由外部 App 通过构造注入（解耦 UI 与业务逻辑）。
    卡片本身不持有 Task 引用，每次 update 时从 App 拿最新快照，
    避免跨线程读写同一个 Task 对象。
    """

    def __init__(self, master, app, task_id):
        # type: (tk.Widget, App, str) -> None
        super().__init__(master, style="Card.TFrame", padding=(14, 10))
        self._app = app
        self._id = task_id

        # ---- 左侧：圆形开关（Canvas 自绘，比 Checkbutton 好看） ----
        self._sw_canvas = tk.Canvas(self, width=44, height=24,
                                    bg=C_CARD_BG, highlightthickness=0,
                                    bd=0)
        self._sw_canvas.pack(side=tk.LEFT, padx=(0, 12))
        self._sw_canvas.bind("<Button-1>", self._on_toggle_switch)

        # ---- 中间信息区 ----
        info = ttk.Frame(self, style="Card.TFrame")
        info.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # 名称行：任务名 + 模式徽标（单向镜像/双向同步）
        name_row = ttk.Frame(info, style="Card.TFrame")
        name_row.pack(anchor=tk.W, fill=tk.X)

        self._name_lbl = tk.Label(name_row, font=("", 11, "bold"),
                                  fg=C_TEXT, bg=C_CARD_BG, anchor="w")
        self._name_lbl.pack(side=tk.LEFT)

        # P0-5 修复：模式徽标（原表格有"方向"列，卡片化后回归）
        self._mode_lbl = tk.Label(name_row, font=("", 8),
                                  fg=C_TEXT_MUTED, bg=C_CARD_BG, anchor="w")
        self._mode_lbl.pack(side=tk.LEFT, padx=(8, 0), pady=(2, 0))

        self._path_lbl = tk.Label(info, font=("", 9), fg=C_TEXT_MUTED,
                                  bg=C_CARD_BG, anchor="w")
        self._path_lbl.pack(anchor="w", pady=(2, 0))

        self._sched_lbl = tk.Label(info, font=("", 9), fg=C_TEXT_MUTED,
                                   bg=C_CARD_BG, anchor="w")
        self._sched_lbl.pack(anchor="w", pady=(2, 0))

        # ---- 右侧：状态 + 操作按钮 + 下次运行 ----
        right = ttk.Frame(self, style="Card.TFrame")
        right.pack(side=tk.RIGHT)

        # 下次运行（最右）
        self._next_lbl = tk.Label(right, font=("", 9), fg=C_TEXT_MUTED,
                                  bg=C_CARD_BG)
        self._next_lbl.pack(side=tk.RIGHT, padx=(10, 0))

        # 操作按钮组（运行 / 编辑 / 删除）
        self._btn_frame = ttk.Frame(right, style="Card.TFrame")
        self._btn_frame.pack(side=tk.RIGHT)

        self._run_btn = tk.Button(
            self._btn_frame, text="运行", width=6,
            fg="#FFFFFF", bg=C_BRAND, activebackground=C_BRAND_DARK,
            relief=tk.FLAT, bd=0, padx=8, pady=2, cursor="hand2",
            command=self._on_run)
        self._run_btn.pack(side=tk.LEFT, padx=(0, 4))

        self._edit_btn = tk.Button(
            self._btn_frame, text="编辑", width=6,
            fg=C_TEXT, bg="#FFFFFF", relief=tk.SOLID, bd=1,
            activebackground=C_CARD_BG_ALT, padx=8, pady=2, cursor="hand2",
            command=self._on_edit)
        self._edit_btn.pack(side=tk.LEFT, padx=(0, 4))

        self._del_btn = tk.Button(
            self._btn_frame, text="删除", width=6,
            fg=C_DELETE, bg="#FFFFFF", relief=tk.SOLID, bd=1,
            activebackground=C_CARD_BG_ALT, padx=8, pady=2, cursor="hand2",
            command=self._on_delete)
        self._del_btn.pack(side=tk.LEFT)

        # 状态标记（对勾 / 运行中 / -），位于操作按钮左侧
        self._status_lbl = tk.Label(right, font=("", 10, "bold"),
                                    bg=C_CARD_BG)
        self._status_lbl.pack(side=tk.RIGHT, padx=(0, 8))

        # 右键菜单 / 选中 / 双击运行 绑定（整卡都响应）
        for w in (self, self._sw_canvas, info, name_row, self._name_lbl,
                  self._mode_lbl, self._path_lbl, self._sched_lbl, right,
                  self._next_lbl, self._btn_frame, self._status_lbl):
            try:
                w.bind("<Button-3>", self._on_context_menu)
                w.bind("<Button-1>", self._on_click)
                # P1: 双击卡片 = 立即同步（与"运行"按钮同路径）
                w.bind("<Double-Button-1>", self._on_dblclick)
            except tk.TclError:
                pass

        # 选择态高亮：绑定 1px 实线边框
        self._selected = False
        self.bind("<Configure>", self._on_configure)

    # ---- 事件回调（委托给 App） ----
    def _on_run(self):
        self._app._run_card_task(self._id)

    def _on_edit(self):
        self._app._edit_card_task(self._id)

    def _on_delete(self):
        self._app._delete_card_task(self._id)

    def _on_toggle_switch(self, _evt=None):
        self._app._toggle_card_enabled(self._id)

    def _on_context_menu(self, event):
        try:
            self._app._on_task_context_menu_card(self._id, event)
        except Exception:
            pass

    def _on_click(self, _evt=None):
        self._app._select_card(self._id)

    def _on_dblclick(self, _evt=None):
        self._app._select_card(self._id)
        self._app._run_card_task(self._id)

    def _on_configure(self, _evt=None):
        # 选中态由 App._select_card 控制样式，这里仅保证卡片高度一致
        pass

    # ---- 渲染：刷新内容 ----
    def refresh(self, task):
        # type: (Task) -> None
        """按 Task 快照刷新卡片显示。"""
        running = self._app.scheduler.is_task_running(task.id)
        self._name_lbl.config(text=task.name)
        self._mode_lbl.config(text=_MODE_LABEL.get(task.mode, task.mode))
        self._path_lbl.config(
            text="%s → %s" % (_short_path(task.source), _short_path(task.target)))

        # 调度描述
        if task.schedule.enabled and task.enabled:
            stype = task.schedule.type
            if stype == "interval":
                sched_txt = "每 %d 分钟" % task.schedule.interval_minutes
            elif stype == "daily" and task.schedule.times:
                sched_txt = "每日 %s" % ",".join(task.schedule.times)
            elif stype == "weekly" and task.schedule.weekdays:
                days = "一二三四五六日"
                sched_txt = "每周 %s" % " ".join(days[n - 1] for n in task.schedule.weekdays)
            else:
                sched_txt = "未安排定时"
            last_txt = "上次: %s" % (format_epoch(task.last_run) or "-")
            self._sched_lbl.config(text="%s · %s" % (sched_txt, last_txt))
        else:
            self._sched_lbl.config(
                text="未启用" if not task.enabled else "未安排定时")

        # 下次运行
        if task.schedule.enabled and task.enabled and task.next_run:
            self._next_lbl.config(text="下次: %s" % format_epoch(task.next_run),
                                  fg=C_TEXT_MUTED)
        else:
            self._next_lbl.config(text="", fg=C_TEXT_DISABLED)

        # 状态标记
        # P0-4 修复：从未运行（无 last_run）显示"未运行"，不再误报"✓成功"
        if running:
            self._status_lbl.config(text="●运行中", fg=C_BRAND)
        elif not task.enabled:
            self._status_lbl.config(text="○已禁用", fg=C_TEXT_DISABLED)
        elif not task.last_run:
            self._status_lbl.config(text="─未运行", fg=C_TEXT_DISABLED)
        elif task.last_status in ("成功", None):
            self._status_lbl.config(text="✓成功", fg=C_OK)
        elif task.last_status in ("部分失败", "已取消"):
            self._status_lbl.config(text="⚠%s" % task.last_status, fg=C_WARN)
        else:
            self._status_lbl.config(text="✗%s" % (task.last_status or "失败"),
                                    fg=C_DELETE)

        # P1: 运行中禁用"运行"按钮（视觉反馈，避免点击后只弹提示）
        self._run_btn.config(state=tk.DISABLED if running else tk.NORMAL)

        # 开关绘制
        self._draw_switch(task.enabled)

        # 禁用态文字
        if not task.enabled:
            self._name_lbl.config(fg=C_TEXT_DISABLED)
            self._path_lbl.config(fg=C_TEXT_DISABLED)
            self._sched_lbl.config(fg=C_TEXT_DISABLED)
        else:
            self._name_lbl.config(fg=C_TEXT)
            self._path_lbl.config(fg=C_TEXT_MUTED)
            self._sched_lbl.config(fg=C_TEXT_MUTED)

    def _draw_switch(self, on):
        # type: (bool) -> None
        """自绘圆形开关：on=青绿色填充，off=灰色。"""
        c = self._sw_canvas
        c.delete("all")
        cw = int(c["width"])
        ch = int(c["height"])
        r = ch // 2 - 2
        # 轨道
        track_x1 = 2
        track_x2 = cw - 2
        track_y1 = 4
        track_y2 = ch - 4
        c.create_oval(track_x1, track_y1, track_x2, track_y2,
                      fill=(C_SWITCH_ON if on else C_SWITCH_OFF),
                      outline="")
        # 滑块
        if on:
            cx = track_x2 - r - 1
        else:
            cx = track_x1 + r + 1
        cy = ch // 2
        c.create_oval(cx - r, cy - r, cx + r, cy + r,
                      fill="#FFFFFF", outline="#D0D0D0")

    def set_selected(self, sel):
        # type: (bool) -> None
        """选中态：淡青绿色底 + 1px 主色边框。"""
        self._selected = sel
        if sel:
            self.configure(style="CardSelected.TFrame")
            for w in (self._name_lbl, self._mode_lbl, self._path_lbl,
                      self._sched_lbl, self._next_lbl, self._status_lbl,
                      self._sw_canvas):
                try:
                    w.configure(bg=C_BRAND_LIGHT)
                except tk.TclError:
                    pass
        else:
            self.configure(style="Card.TFrame")
            for w in (self._name_lbl, self._mode_lbl, self._path_lbl,
                      self._sched_lbl, self._next_lbl, self._status_lbl,
                      self._sw_canvas):
                try:
                    w.configure(bg=C_CARD_BG)
                except tk.TclError:
                    pass


# ======================================================================
#  App — 主应用
# ======================================================================
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
        self._wait_gen = 0
        self._cancel = threading.Event()
        self._sched_cancel = threading.Event()
        self._prog_count = 0
        self._last_prog_ts = 0.0
        self._closing = False
        self._tick_id = None               # type: Optional[str]
        self._drain_id = None              # type: Optional[str]
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
        self._selected_id = None       # type: Optional[str]
        self._empty_lbl = None         # type: Optional[tk.Label]

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

    # ==================================================================
    #  UI 构建 —— 品牌栏 + 工具栏 + Canvas 卡片列表 + 运行日志 + 状态栏
    # ==================================================================
    def _build_ui(self):
        # type: () -> None
        self.root.title("filesync v%s — 定时文件同步工具" % APP_VERSION)
        self.root.geometry("960x640")
        self.root.configure(bg=C_BG)

        self._setup_style()

        # ---- 1. 品牌栏 ----
        brand = tk.Frame(self.root, bg=C_BRAND, height=52)
        brand.pack(fill=tk.X, side=tk.TOP)
        brand.pack_propagate(False)

        # 左：品牌标识（简单 emoji + 文字）
        brand_left = tk.Frame(brand, bg=C_BRAND)
        brand_left.pack(side=tk.LEFT, padx=16)
        tk.Label(brand_left, text="📁", font=("", 18), bg=C_BRAND,
                 fg="#FFFFFF").pack(side=tk.LEFT)
        tk.Label(brand_left, text="filesync", font=("", 14, "bold"),
                 bg=C_BRAND, fg="#FFFFFF").pack(side=tk.LEFT, padx=(6, 4))
        tk.Label(brand_left, text="定时文件同步工具", font=("", 10),
                 bg=C_BRAND, fg="#E0F2F1").pack(side=tk.LEFT)

        # 右：全局按钮（运行全部 / 新建组 / 设置）
        brand_right = tk.Frame(brand, bg=C_BRAND)
        brand_right.pack(side=tk.RIGHT, padx=12)

        run_all_btn = tk.Button(
            brand_right, text="▶ 运行全部", font=("", 10, "bold"),
            fg="#FFFFFF", bg=C_BRAND_DARK, activebackground="#00695C",
            relief=tk.FLAT, bd=0, padx=14, pady=6, cursor="hand2",
            command=self._on_run_all)
        run_all_btn.pack(side=tk.LEFT, padx=(0, 6))

        new_group_btn = tk.Button(
            brand_right, text="＋ 新建组", font=("", 9),
            fg="#FFFFFF", bg=C_BRAND, activebackground=C_BRAND_DARK,
            relief=tk.FLAT, bd=0, padx=10, pady=6, cursor="hand2",
            command=self._on_new_group)
        new_group_btn.pack(side=tk.LEFT, padx=(0, 6))

        settings_btn = tk.Button(
            brand_right, text="⚙ 设置", font=("", 9),
            fg="#FFFFFF", bg=C_BRAND, activebackground=C_BRAND_DARK,
            relief=tk.FLAT, bd=0, padx=10, pady=6, cursor="hand2",
            command=self._on_settings)
        settings_btn.pack(side=tk.LEFT)

        # ---- 2. 工具栏 ----
        toolbar = tk.Frame(self.root, bg=C_BG)
        toolbar.pack(fill=tk.X, side=tk.TOP, padx=14, pady=(10, 4))

        self._add_btn = tk.Button(
            toolbar, text="＋ 添加任务", font=("", 9, "bold"),
            fg="#FFFFFF", bg=C_BRAND, activebackground=C_BRAND_DARK,
            relief=tk.FLAT, bd=0, padx=14, pady=5, cursor="hand2",
            command=self._on_add)
        self._add_btn.pack(side=tk.LEFT)

        self._run_all_tb = tk.Button(
            toolbar, text="▶ 运行全部", font=("", 9),
            fg=C_TEXT, bg="#FFFFFF", relief=tk.SOLID, bd=1,
            activebackground=C_CARD_BG_ALT, padx=14, pady=5, cursor="hand2",
            command=self._on_run_all)
        self._run_all_tb.pack(side=tk.LEFT, padx=(6, 0))

        # 中间占位
        tk.Frame(toolbar, bg=C_BG).pack(side=tk.LEFT, fill=tk.X, expand=True)

        # 右：已启用计数 + 调度器开关
        self._enabled_lbl = tk.Label(toolbar, text="0/0 个已启用",
                                     font=("", 9), fg=C_TEXT_MUTED, bg=C_BG)
        self._enabled_lbl.pack(side=tk.RIGHT, padx=(12, 0))

        self._sched_btn = tk.Button(
            toolbar, text="启动调度", font=("", 9),
            fg=C_TEXT, bg="#FFFFFF", relief=tk.SOLID, bd=1,
            activebackground=C_CARD_BG_ALT, padx=10, pady=5, cursor="hand2",
            command=self._toggle_scheduler)
        self._sched_btn.pack(side=tk.RIGHT, padx=(6, 0))

        # ---- 3. 卡片式任务列表（Canvas + Frame 滚动容器）----
        list_outer = tk.Frame(self.root, bg=C_BG)
        list_outer.pack(fill=tk.BOTH, expand=True, padx=14, pady=4)

        self._task_canvas = tk.Canvas(list_outer, bg=C_BG, highlightthickness=0,
                                      bd=0, borderwidth=0)
        sb = ttk.Scrollbar(list_outer, orient="vertical",
                           command=self._task_canvas.yview)
        self._task_canvas.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._task_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # 内部 Frame（卡片容器）
        self._task_inner = ttk.Frame(self._task_canvas, style="CardHost.TFrame")
        _win_id = self._task_canvas.create_window((0, 0), window=self._task_inner,
                                                  anchor="nw")
        # assert 收窄 mypy 类型：_build_ui 内刚创建，必非 None
        assert self._task_canvas is not None
        assert self._task_inner is not None
        # 局部变量捕获：lambda/闭包延迟执行，用非 Optional 局部变量
        _canvas = self._task_canvas

        # P0-3 修复：窗口拉伸时卡片随 Canvas 宽度伸展
        # （create_window 默认不绑宽度，卡片只按内容宽，右侧留白）
        _canvas.bind(
            "<Configure>",
            lambda e: _canvas.itemconfigure(_win_id, width=e.width))

        # 动态调整 Canvas 滚动范围
        self._task_inner.bind(
            "<Configure>",
            lambda e: _canvas.configure(
                scrollregion=_canvas.bbox("all")))

        # 鼠标滚轮滚动（跨平台）
        # P0-2 修复：仅当指针位于任务列表上方时才滚动列表；
        # 此前 bind_all 全局劫持，指针在日志区滚动时滚的却是任务列表
        def _wheel_in_canvas(event):
            # type: (tk.Event) -> bool
            w = getattr(event, "widget", None)
            while w is not None:
                if w is _canvas:
                    return True
                w = getattr(w, "master", None)
            return False

        def _on_mousewheel(event):
            # type: (tk.Event) -> None
            if not _wheel_in_canvas(event):
                return
            if sys.platform == "darwin":
                _canvas.yview_scroll(int(-event.delta), "units")
            else:
                _canvas.yview_scroll(int(-event.delta / 120), "units")

        def _on_wheel_btn45(event):
            # type: (tk.Event) -> None
            if _wheel_in_canvas(event):
                _canvas.yview_scroll(-1 if int(getattr(event, "num", 4)) == 4 else 1,
                                     "units")

        _canvas.bind_all("<MouseWheel>", _on_mousewheel)
        _canvas.bind_all("<Button-4>", _on_wheel_btn45)
        _canvas.bind_all("<Button-5>", _on_wheel_btn45)

        # ---- 4. 运行日志 ----
        log_outer = tk.Frame(self.root, bg=C_BG)
        log_outer.pack(fill=tk.X, side=tk.BOTTOM, padx=14, pady=(4, 4))

        tk.Label(log_outer, text="运行日志", font=("", 9, "bold"),
                 fg=C_TEXT, bg=C_BG).pack(anchor="w")

        self.log_text = scrolledtext.ScrolledText(
            log_outer, height=5, state=tk.DISABLED,
            font=("Consolas", 9), bg="#FFFFFF", fg=C_TEXT,
            relief=tk.SOLID, bd=1, highlightthickness=0,
            borderwidth=1, bordercolor=C_BORDER)
        self.log_text.pack(fill=tk.X, pady=(2, 0))

        # P1: 日志按级别着色（ERROR 红 / WARN 橙），提升扫读性
        self.log_text.tag_configure("error", foreground="#C62828")
        self.log_text.tag_configure("warn", foreground="#E65100")

        # P1: ↑↓ 方向键在卡片间移动选中（无鼠标操作可达）
        self.root.bind("<Up>", lambda e: self._on_arrow(-1))
        self.root.bind("<Down>", lambda e: self._on_arrow(1))

        # ---- 5. 底部状态栏 ----
        self._status_bar = tk.Frame(self.root, bg=C_CARD_BG, height=24)
        self._status_bar.pack(fill=tk.X, side=tk.BOTTOM)
        self._status_bar.pack_propagate(False)

        self._status_label = tk.Label(self._status_bar, text="调度器：已停止",
                                       font=("", 8), fg=C_TEXT_MUTED,
                                       bg=C_CARD_BG, anchor="w")
        self._status_label.pack(side=tk.LEFT, padx=12)

        self._next_lbl_bar = tk.Label(self._status_bar, text="", font=("", 8),
                                      fg=C_TEXT_MUTED, bg=C_CARD_BG)
        self._next_lbl_bar.pack(side=tk.LEFT, padx=(20, 0))

        self._ver_lbl = tk.Label(self._status_bar,
                                 text="v%s" % APP_VERSION,
                                 font=("", 8), fg=C_TEXT_DISABLED,
                                 bg=C_CARD_BG)
        self._ver_lbl.pack(side=tk.RIGHT, padx=12)

        self._data_dir_lbl = tk.Label(self._status_bar,
                                      text="数据: %s" % CONFIG_PATH,
                                      font=("", 8), fg=C_TEXT_DISABLED,
                                      bg=C_CARD_BG)
        self._data_dir_lbl.pack(side=tk.RIGHT, padx=(0, 6))

    # ---- ttk.Style 配置 ----
    def _setup_style(self):
        # type: () -> None
        style = ttk.Style(self.root)
        # 选一个基础主题再覆盖（跨平台兼容）
        for theme in ("clam", "vista", "winnative", "default"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break

        # 卡片容器样式
        style.configure("Card.TFrame", background=C_CARD_BG,
                        relief=tk.SOLID, borderwidth=1)
        style.configure("CardSelected.TFrame", background=C_BRAND_LIGHT,
                        relief=tk.SOLID, borderwidth=1)
        style.configure("CardHost.TFrame", background=C_BG)

        # 按钮统一（ttk 按钮样式覆盖全局默认）
        style.configure("TButton", font=("", 9))
        style.configure("Accent.TButton", font=("", 9, "bold"),
                        background=C_BRAND, foreground="#FFFFFF",
                        padding=8)
        style.map("Accent.TButton",
                  background=[("active", C_BRAND_DARK)])

        # Scrollbar 扁平
        style.configure("Vertical.TScrollbar", background=C_BORDER,
                        troughcolor=C_BG, borderwidth=0, arrowsize=14)

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
                if self._selected_id == tid:
                    self._selected_id = None

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
                assert self._task_inner is not None
                for t in tasks:
                    if t.id in self._task_rows:
                        self._task_rows[t.id].pack(fill=tk.X, padx=2, pady=3)

            if not tasks:
                self._show_empty_state()
            else:
                self._hide_empty_state()

        # 恢复选中态视觉
        if self._selected_id and self._selected_id in self._task_rows:
            self._task_rows[self._selected_id].set_selected(True)

    def _show_empty_state(self):
        # type: () -> None
        if self._empty_lbl is None:
            assert self._task_inner is not None
            # P1: 文案指向实际按钮位置（工具栏左侧）+ 整块可点击直接新建
            self._empty_lbl = tk.Label(
                self._task_inner,
                text="还没有同步任务\n点击左上方「＋ 添加任务」或此处创建第一个任务",
                font=("", 10), fg=C_TEXT_DISABLED, bg=C_BG, pady=40,
                cursor="hand2")
            self._empty_lbl.bind("<Button-1>", lambda e: self._on_add())
        self._empty_lbl.pack(fill=tk.X, padx=2)

    def _hide_empty_state(self):
        # type: () -> None
        if hasattr(self, "_empty_lbl") and self._empty_lbl is not None:
            try:
                self._empty_lbl.pack_forget()
            except tk.TclError:
                pass

    # ==================================================================
    #  选中 / 右键菜单 / 卡片操作入口
    # ==================================================================
    def _select_card(self, task_id):
        # type: (str) -> None
        """点击卡片选中（替代 Treeview selection）。"""
        old = self._selected_id
        self._selected_id = task_id
        if old and old in self._task_rows and old != task_id:
            self._task_rows[old].set_selected(False)
        if task_id in self._task_rows:
            self._task_rows[task_id].set_selected(True)

    def _on_arrow(self, delta):
        # type: (int) -> None
        """↑/↓ 在卡片列表中移动选中（替代 Treeview 键盘导航）。"""
        ids = [t.id for t in self.store.snapshot()]
        if not ids:
            return
        if self._selected_id not in ids:
            self._select_card(ids[0] if delta > 0 else ids[-1])
            return
        i = ids.index(self._selected_id)
        i = min(max(i + delta, 0), len(ids) - 1)
        self._select_card(ids[i])

    def _selected_task(self):
        # type: () -> Optional[Task]
        """获取当前选中任务（与 Treeview 版签名一致）。"""
        if self._selected_id is None:
            return None
        return self.store.get(self._selected_id)

    def _on_task_context_menu_card(self, task_id, event):
        # type: (str, object) -> None
        """卡片右键菜单（替代 Treeview 版，功能相同：启用/禁用切换）。"""
        task = self.store.get(task_id)
        if task is None:
            return
        self._select_card(task_id)
        menu = tk.Menu(self.root, tearoff=0)
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
        # 复用原 _on_sync_now 的完整流程（SyncFlowMixin）
        self._selected_id = task_id
        if task_id in self._task_rows:
            self._task_rows[task_id].set_selected(True)
        self._on_sync_now()

    def _edit_card_task(self, task_id):
        # type: (str) -> None
        self._selected_id = task_id
        if task_id in self._task_rows:
            self._task_rows[task_id].set_selected(True)
        self._on_edit()

    def _delete_card_task(self, task_id):
        # type: (str) -> None
        self._selected_id = task_id
        if task_id in self._task_rows:
            self._task_rows[task_id].set_selected(True)
        self._on_delete()

    def _toggle_card_enabled(self, task_id):
        # type: (str) -> None
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
                # 记住选中：编辑可能改了 ID？不会，Task.id 是 UUID 不随编辑变
                self._refresh_tasks(full=True)
                self._selected_id = task.id
                if task.id in self._task_rows:
                    self._task_rows[task.id].set_selected(True)
                self._maybe_autostart()
        finally:
            self.scheduler.release(task.id)

    def _on_delete(self):
        # type: () -> None
        task = self._selected_task()
        if task is None:
            messagebox.showinfo("提示", "请先选择要删除的任务")
            return
        if not self.scheduler.acquire(task.id):
            messagebox.showinfo("提示", "该任务正在运行中，请等待完成后再删除")
            return
        try:
            if messagebox.askyesno("确认", "确定删除任务 '%s'？" % task.name):
                self.store.remove(task.id)
                self._selected_id = None
                self._refresh_tasks(full=True)
        finally:
            self.scheduler.release(task.id)

    def _toggle_task_enabled(self, task_id):
        # type: (str) -> None
        task = self.store.get(task_id)
        if task is None:
            return
        if not self.scheduler.acquire(task.id):
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
            self.scheduler.release(task.id)
        # 只增量刷新这张卡片（避免全量重排打断用户视觉）
        if task_id in self._task_rows:
            self._task_rows[task_id].refresh(task)
        # 刷新顶部计数和底部状态栏
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

    def _maybe_autostart(self):
        # type: () -> None
        has_sched = any(t.schedule.enabled and t.enabled for t in self.store.tasks)
        if has_sched and not self.scheduler.running:
            self.scheduler.start()
            self._refresh_tasks(full=True)

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
        self._popup_if_alive("info", "批量运行", msg)

    def _on_new_group(self):
        # type: () -> None
        """分组功能预留入口（现有 Task 无 group 字段，后续扩展）。"""
        messagebox.showinfo("提示", "分组功能即将推出，敬请期待")

    def _on_settings(self):
        # type: () -> None
        """设置入口：打开日志目录 + 打开配置目录。"""
        choice = messagebox.askyesnocancel(
            "设置",
            "打开日志目录？\n\n"
            "点 [是] 打开日志目录，[否] 打开配置目录，[取消] 返回。")
        if choice is True:
            self._open_logs()
        elif choice is False:
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
        """从级别（或日志行内容）推断着色 tag；普通行返回 None。"""
        s = source.upper()
        if "ERROR" in s or "失败" in s or "异常" in s:
            return "error"
        if "WARN" in s or "警告" in s:
            return "warn"
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
