"""主窗口布局与 ttk 样式构建混入。

职责边界
--------
从 gui_app.App 拆出的纯"控件搭建"代码：品牌栏、工具栏、卡片列表
Canvas 滚动容器、日志面板、状态栏与全套 ttk.Style。App._build_ui
保留在核心层作为编排入口（窗口标题/尺寸 + 依序调用本混入），
满足"核心层保留常驻界面"的结构断言（test_38）。

宿主状态契约
------------
root（写 title/geometry/configure）、回调方法 _on_settings/_on_add/
_on_run_all/_on_search/_on_search_focus_in/_on_search_focus_out/
_toggle_scheduler/_on_arrow（延迟调用，运行期由 App 提供）；
本混入创建并挂到宿主上的属性：_search_var/_search_entry/
_search_placeholder/_add_btn/_run_all_tb/_enabled_lbl/_sched_btn/
_pane/_task_canvas/_task_inner/log_text/_status_bar/_status_label/
_next_lbl_bar/_ver_lbl。

线程模型：仅主线程构建（_build_ui 在 App.__init__ 内调用）。
"""

import sys

import tkinter as tk
from tkinter import ttk, scrolledtext

from typing import Any, Callable, Optional, TYPE_CHECKING

from gui_tasklist import (
    C_BRAND, C_BRAND_DARK, C_BRAND_LIGHT,
    C_DELETE, C_BG, C_CARD_BG, C_CARD_BG_ALT,
    C_TEXT, C_TEXT_MUTED, C_TEXT_DISABLED,
    C_BORDER, C_WHITE, C_BRAND_SUB, C_LOG_ERROR, C_LOG_WARN, C_DELETE_HOVER,
)
from main import APP_VERSION


class LayoutMixin(object):
    """主窗口布局与样式（见模块 docstring 的宿主状态契约）。"""

    # ---------- 宿主状态契约（仅类型声明，运行期被实例属性遮蔽） ----------
    root = None                 # type: tk.Tk
    # 构建前为 None（App.__init__ 先声明占位再调 _build_ui），与宿主初始化一致
    _task_canvas = None         # type: Optional[tk.Canvas]
    _task_inner = None          # type: Optional[ttk.Frame]
    if TYPE_CHECKING:  # 方法契约仅类型层：类级赋值会按 MRO 遮蔽 App 的真实现
        _on_settings = None     # type: Callable[..., None]
        _on_add = None          # type: Callable[..., None]
        _on_run_all = None      # type: Callable[..., None]
        _on_search = None       # type: Callable[..., None]
        _on_search_focus_in = None   # type: Callable[..., None]
        _on_search_focus_out = None  # type: Callable[..., None]
        _toggle_scheduler = None     # type: Callable[..., None]
        _on_arrow = None        # type: Callable[..., None]

    def _setup_layout(self):
        # type: () -> None
        """搭建五区布局：品牌栏 + 工具栏 + 卡片列表/日志 + 状态栏。"""
        # ---- 1. 品牌栏 ----
        brand = tk.Frame(self.root, bg=C_BRAND, height=52)
        brand.pack(fill=tk.X, side=tk.TOP)
        brand.pack_propagate(False)

        # 左：品牌标识（简单 emoji + 文字）
        brand_left = tk.Frame(brand, bg=C_BRAND)
        brand_left.pack(side=tk.LEFT, padx=16)
        tk.Label(brand_left, text="📁", font=("", 18), bg=C_BRAND,
                 fg=C_WHITE).pack(side=tk.LEFT)
        tk.Label(brand_left, text="filesync", font=("", 14, "bold"),
                 bg=C_BRAND, fg=C_WHITE).pack(side=tk.LEFT, padx=(6, 4))
        tk.Label(brand_left, text="定时文件同步工具", font=("", 10),
                 bg=C_BRAND, fg=C_BRAND_SUB).pack(side=tk.LEFT)

        # 右：全局入口（设置）——Label 手绘扁平按钮，与品牌栏底色无缝
        brand_right = tk.Frame(brand, bg=C_BRAND)
        brand_right.pack(side=tk.RIGHT, padx=12)

        settings_lbl = tk.Label(brand_right, text="⚙ 设置", font=("", 9),
                                bg=C_BRAND, fg=C_WHITE, padx=10, pady=5,
                                cursor="hand2")
        settings_lbl.pack(side=tk.LEFT)
        settings_lbl.bind("<Button-1>", lambda e: self._on_settings())
        settings_lbl.bind("<Enter>",
                          lambda e: settings_lbl.config(bg=C_BRAND_DARK))
        settings_lbl.bind("<Leave>",
                          lambda e: settings_lbl.config(bg=C_BRAND))

        # ---- 2. 工具栏 ----
        toolbar = tk.Frame(self.root, bg=C_BG)
        toolbar.pack(fill=tk.X, side=tk.TOP, padx=14, pady=(10, 4))

        self._add_btn = ttk.Button(
            toolbar, text="＋ 添加任务", style="Accent.TButton",
            command=self._on_add)
        self._add_btn.pack(side=tk.LEFT)

        self._run_all_tb = ttk.Button(
            toolbar, text="▶ 运行全部", style="Outline.TButton",
            command=self._on_run_all)
        self._run_all_tb.pack(side=tk.LEFT, padx=(6, 0))

        # 搜索/筛选
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._on_search())
        _search_frame = tk.Frame(toolbar, bg=C_BG)
        _search_frame.pack(side=tk.LEFT, padx=(12, 0))
        _search_icon = tk.Label(_search_frame, text="🔍", font=("", 9),
                                bg=C_CARD_BG, fg=C_TEXT_MUTED, padx=4)
        _search_icon.pack(side=tk.LEFT)
        self._search_entry = tk.Entry(
            _search_frame, textvariable=self._search_var,
            font=("", 9), width=16, relief=tk.SOLID, bd=1,
            bg=C_CARD_BG, fg=C_TEXT, insertbackground=C_TEXT)
        self._search_entry.pack(side=tk.LEFT)
        # 占位提示
        self._search_placeholder = "搜索任务名称…"
        self._search_entry.insert(0, self._search_placeholder)
        self._search_entry.config(fg=C_TEXT_DISABLED)
        self._search_entry.bind("<FocusIn>", self._on_search_focus_in)
        self._search_entry.bind("<FocusOut>", self._on_search_focus_out)

        # 中间占位
        tk.Frame(toolbar, bg=C_BG).pack(side=tk.LEFT, fill=tk.X, expand=True)

        # 右：已启用计数 + 调度器开关
        self._enabled_lbl = tk.Label(toolbar, text="0/0 个已启用",
                                     font=("", 9), fg=C_TEXT_MUTED, bg=C_BG)
        self._enabled_lbl.pack(side=tk.RIGHT, padx=(12, 0))

        self._sched_btn = ttk.Button(
            toolbar, text="启动调度", style="Outline.TButton",
            command=self._toggle_scheduler)
        self._sched_btn.pack(side=tk.RIGHT, padx=(6, 0))

        # ---- 3. 卡片式任务列表 + 日志面板（PanedWindow 可拖拽分隔）----
        self._pane = tk.PanedWindow(
            self.root, orient=tk.VERTICAL, bg=C_BG,
            sashwidth=4, sashrelief=tk.FLAT, borderwidth=0)
        self._pane.pack(fill=tk.BOTH, expand=True, padx=14, pady=4)

        # 上半区：卡片列表
        list_outer = tk.Frame(self._pane, bg=C_BG)
        self._pane.add(list_outer, minsize=120, stretch="always")

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
        # assert 收窄 mypy 类型：本方法内刚创建，必非 None
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

        # ---- 4. 运行日志（PanedWindow 下半区，可拖拽调节高度）----
        log_outer = tk.Frame(self._pane, bg=C_BG)
        self._pane.add(log_outer, minsize=60, stretch="never")

        tk.Label(log_outer, text="运行日志", font=("", 9, "bold"),
                 fg=C_TEXT, bg=C_BG).pack(anchor="w")

        self.log_text = scrolledtext.ScrolledText(
            log_outer, height=5, state=tk.DISABLED,
            font=("Consolas", 9), bg=C_CARD_BG, fg=C_TEXT,
            relief=tk.SOLID, bd=1, highlightthickness=0,
            borderwidth=1)
        self.log_text.pack(fill=tk.BOTH, expand=True, pady=(2, 0))

        # P1: 日志按级别着色（ERROR 红 / WARN 橙），提升扫读性
        self.log_text.tag_configure("error", foreground=C_LOG_ERROR)
        self.log_text.tag_configure("warn", foreground=C_LOG_WARN)

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

    # ---- ttk.Style 配置 ----
    def _setup_style(self):
        # type: () -> None
        style = ttk.Style(self.root)
        # 选一个基础主题再覆盖（跨平台兼容）
        for theme in ("clam", "vista", "winnative", "default"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break

        # ---------- 字体约定 ----------
        # F_NORMAL  = ("", 9)      # 正文
        # F_BOLD    = ("", 9, "bold")  # 强调
        # F_TITLE   = ("", 11, "bold") # 卡片标题
        # F_SMALL   = ("", 8)      # 状态栏 / 辅助
        # F_BRAND   = ("", 14, "bold") # 品牌名

        # ---------- 卡片容器 ----------
        style.configure("Card.TFrame", background=C_CARD_BG,
                        relief=tk.SOLID, borderwidth=1)
        style.configure("CardSelected.TFrame", background=C_BRAND_LIGHT,
                        relief=tk.SOLID, borderwidth=1)
        style.configure("CardHost.TFrame", background=C_BG)

        # ---------- TButton（全局默认） ----------
        style.configure("TButton", font=("", 9), padding=4)
        style.map("TButton",
                  background=[("active", C_CARD_BG_ALT)])

        # Accent：主色实心按钮（添加任务 / 运行 / 确认执行）
        style.configure("Accent.TButton", font=("", 9, "bold"),
                        background=C_BRAND, foreground=C_WHITE,
                        padding=(12, 5))
        style.map("Accent.TButton",
                  background=[("active", C_BRAND_DARK),
                              ("disabled", C_TEXT_DISABLED)],
                  foreground=[("disabled", C_WHITE)])

        # Outline：白底描边按钮（编辑 / 运行全部 / 调度开关）
        style.configure("Outline.TButton", font=("", 9),
                        background=C_CARD_BG, foreground=C_TEXT,
                        relief=tk.SOLID, borderwidth=1, padding=(12, 5))
        style.map("Outline.TButton",
                  background=[("active", C_CARD_BG_ALT),
                              ("disabled", C_BG)],
                  foreground=[("disabled", C_TEXT_DISABLED)])

        # Danger：删除按钮（红字白底）
        style.configure("Danger.TButton", font=("", 9),
                        background=C_CARD_BG, foreground=C_DELETE,
                        relief=tk.SOLID, borderwidth=1, padding=(12, 5))
        style.map("Danger.TButton",
                  background=[("active", C_DELETE_HOVER)],
                  foreground=[("disabled", C_TEXT_DISABLED)])

        # BrandBtn：品牌栏内白字透明底（设置）
        style.configure("Brand.TButton", font=("", 9),
                        background=C_BRAND, foreground=C_WHITE,
                        relief=tk.FLAT, borderwidth=0, padding=(10, 6))
        style.map("Brand.TButton",
                  background=[("active", C_BRAND_DARK)])

        # ---------- TLabel ----------
        style.configure("TLabel", font=("", 9), foreground=C_TEXT)
        style.configure("Muted.TLabel", font=("", 9), foreground=C_TEXT_MUTED)
        style.configure("Small.TLabel", font=("", 8), foreground=C_TEXT_MUTED)
        style.configure("Title.TLabel", font=("", 11, "bold"), foreground=C_TEXT)
        style.configure("Error.TLabel", font=("", 9), foreground=C_DELETE)
        style.configure("Brand.TLabel", font=("", 14, "bold"),
                        foreground=C_WHITE, background=C_BRAND)
        style.configure("BrandSub.TLabel", font=("", 10),
                        foreground=C_BRAND_SUB, background=C_BRAND)

        # ---------- TLabelframe（对话框分组） ----------
        style.configure("TLabelframe", font=("", 9))
        style.configure("TLabelframe.Label", font=("", 9, "bold"))

        # ---------- TNotebook（TaskDialog 选项卡） ----------
        style.configure("TNotebook", background=C_BG)
        style.configure("TNotebook.Tab", font=("", 9), padding=(12, 4))
        style.map("TNotebook.Tab",
                  background=[("selected", C_CARD_BG),
                              ("!selected", C_BG)],
                  foreground=[("selected", C_TEXT),
                              ("!selected", C_TEXT_MUTED)])

        # ---------- Treeview（DiffDialog 差异列表） ----------
        style.configure("Treeview", font=("", 9), rowheight=24,
                        background=C_CARD_BG, fieldbackground=C_CARD_BG)
        style.configure("Treeview.Heading", font=("", 9, "bold"))
        style.map("Treeview",
                  background=[("selected", C_BRAND_LIGHT)],
                  foreground=[("selected", C_TEXT)])

        # ---------- TScrollbar ----------
        style.configure("Vertical.TScrollbar", background=C_BORDER,
                        troughcolor=C_BG, borderwidth=0, arrowsize=14)

        # ---------- TCombobox / TEntry ----------
        style.configure("TCombobox", font=("", 9))
        style.configure("TEntry", font=("", 9))

        # ---------- TSpinbox ----------
        style.configure("TSpinbox", font=("", 9))
