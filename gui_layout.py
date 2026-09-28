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

from typing import Any, Callable, List, Optional, Tuple, TYPE_CHECKING

from gui_tasklist import (
    C_BRAND, C_BRAND_DARK, C_BRAND_LIGHT,
    C_DELETE, C_BG, C_CARD_BG, C_CARD_BG_ALT, C_CARD_HOVER,
    C_TEXT, C_TEXT_MUTED, C_TEXT_DISABLED,
    C_BORDER, C_WHITE, C_BRAND_SUB, C_LOG_ERROR, C_LOG_WARN, C_DELETE_HOVER,
    C_LOG_ERROR_LIGHT, C_LOG_WARN_LIGHT,
    C_DARK_BG, C_DARK_CARD_BG, C_DARK_CARD_BG_ALT, C_DARK_CARD_HOVER,
    C_DARK_TEXT, C_DARK_TEXT_MUTED, C_DARK_TEXT_DISABLED, C_DARK_BORDER,
    C_DARK_BRAND_LIGHT,
)
from main import APP_VERSION

# 工具栏主按钮文案（单一来源）：空态提示需指名按钮位置，改文案时
# 两处不会脱节（曾出现空态写「＋ 添加任务」而按钮已是「+ 添加任务」）
BTN_ADD_TEXT = "+ 添加任务"

# ---------- 主题色表 ----------
# ttk 部件走 ttk.Style 重配；但布局层的 tk.Frame/Label/Canvas/Entry/
# ScrolledText 是硬编码底色的，切换主题时不会变（表现为暗色 ttk 之间
# 夹着白底 Frame）。这些部件在 _setup_layout 创建时用 _reg_themed 登记
# 语义角色，_apply_theme 按当前 _dark_mode 遍历重配。
# 角色集在深浅两套中必须一致——只加浅色键会让该部件在深色下 KeyError。
_THEME_BG = {
    False: {"page": C_BG, "card": C_CARD_BG, "border": C_BORDER},
    True: {"page": C_DARK_BG, "card": C_DARK_CARD_BG, "border": C_DARK_BORDER},
}
_THEME_FG = {
    False: {"text": C_TEXT, "muted": C_TEXT_MUTED, "disabled": C_TEXT_DISABLED},
    True: {"text": C_DARK_TEXT, "muted": C_DARK_TEXT_MUTED,
           "disabled": C_DARK_TEXT_DISABLED},
}

# Tooltip 配色：原先硬编码 #FFFFCC 亮黄，深色模式下弹出极为突兀。
# 用中性深色描边 + 与卡片同族的白/暗底，两种主题下都协调。
C_TIP_BG = "#37474F"
C_TIP_FG = "#ECEFF1"


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
        _on_search_clear = None      # type: Callable[..., None]
        _toggle_scheduler = None     # type: Callable[..., None]
        _on_arrow = None        # type: Callable[..., None]
        _on_filter_changed = None    # type: Callable[..., None]
        _batch_enable = None    # type: Callable[..., None]
        _batch_run = None       # type: Callable[..., None]
        _clear_selection = None  # type: Callable[..., None]
        _refresh_tasks = None   # type: Callable[..., None]

    def _setup_layout(self):
        # type: () -> None
        """搭建五区布局：品牌栏 + 工具栏 + 卡片列表/日志 + 状态栏。"""
        # 主题登记表在此建立（每次重建布局时重置，避免持有了已销毁部件）
        # 品牌栏为主色实色带，深浅主题一致，故不登记
        self._themed = []  # type: List[Tuple[Any, Optional[str], Optional[str]]]
        # ---- 1. 品牌栏 ----
        brand = tk.Frame(self.root, bg=C_BRAND, height=52)
        brand.pack(fill=tk.X, side=tk.TOP)
        brand.pack_propagate(False)

        # 左：品牌标识（纯文本，避免 emoji 跨平台渲染问题）
        brand_left = tk.Frame(brand, bg=C_BRAND)
        brand_left.pack(side=tk.LEFT, padx=16)
        tk.Label(brand_left, text="FS", font=("", 14, "bold"),
                 bg=C_BRAND, fg=C_WHITE).pack(side=tk.LEFT)
        tk.Label(brand_left, text="filesync", font=("", 14, "bold"),
                 bg=C_BRAND, fg=C_WHITE).pack(side=tk.LEFT, padx=(6, 4))
        tk.Label(brand_left, text="定时文件同步工具", font=("", 10),
                 bg=C_BRAND, fg=C_BRAND_SUB).pack(side=tk.LEFT)

        # 右：全局入口（设置 + 深色模式切换）
        brand_right = tk.Frame(brand, bg=C_BRAND)
        brand_right.pack(side=tk.RIGHT, padx=12)

        self._dark_btn = ttk.Button(brand_right, text="浅色",
                                     style="Brand.TButton",
                                     command=self._toggle_dark_mode)
        self._dark_btn.pack(side=tk.LEFT, padx=(0, 6))
        Tooltip(self._dark_btn, "切换深色/浅色模式 (Ctrl+D)")

        settings_btn = ttk.Button(brand_right, text="设置",
                                  style="Brand.TButton",
                                  command=self._on_settings)
        settings_btn.pack(side=tk.LEFT)
        Tooltip(settings_btn, "打开日志/配置目录")

        # ---- 2. 工具栏 ----
        toolbar = tk.Frame(self.root, bg=C_BG)
        toolbar.pack(fill=tk.X, side=tk.TOP, padx=14, pady=(10, 4))
        self._reg_themed(toolbar, "page")

        self._add_btn = ttk.Button(
            toolbar, text=BTN_ADD_TEXT, style="Accent.TButton",
            command=self._on_add)
        self._add_btn.pack(side=tk.LEFT)
        Tooltip(self._add_btn, "新建同步任务 (Ctrl+N)")

        self._run_all_tb = ttk.Button(
            toolbar, text="▶ 运行全部", style="Outline.TButton",
            command=self._on_run_all)
        self._run_all_tb.pack(side=tk.LEFT, padx=(6, 0))
        Tooltip(self._run_all_tb, "运行所有已启用的任务")

        # 搜索/筛选
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._on_search())
        _search_frame = tk.Frame(toolbar, bg=C_BG)
        _search_frame.pack(side=tk.LEFT, padx=(12, 0))
        self._reg_themed(_search_frame, "page")
        # 经典 tk 部件（Entry/Text）没有 -lightcolor/-darkcolor 选项，
        # relief=SOLID+bd=1 画出的 1px 斜角固定取系统 System3D 色，既不是
        # C_BORDER 也无法随深色模式重配。改用 bd=0 去掉该边，再以
        # highlight 画 1px 描边：非聚焦取 highlightbackground（边框色），
        # 聚焦自动切 highlightcolor（主色），既可主题化又保留聚焦反馈。
        self._search_entry = tk.Entry(
            _search_frame, textvariable=self._search_var,
            font=("", 9), width=20, bd=0, relief=tk.FLAT,
            highlightthickness=1, highlightbackground=C_BORDER,
            highlightcolor=C_BRAND,
            bg=C_CARD_BG, fg=C_TEXT, insertbackground=C_TEXT)
        self._search_entry.pack(side=tk.LEFT)
        # 光标色（insertbackground）随 fg 走，否则深色下是黑线看不见
        self._reg_themed(self._search_entry, "card", "text")
        Tooltip(self._search_entry, "搜索任务名称/路径 (Ctrl+F)")
        # 占位提示：措辞须与实际匹配范围一致（_apply_search_filter 同时
        # 匹配 name/source/target），否则用户搜路径时以为功能坏了
        self._search_placeholder = "搜索名称/路径…"
        self._search_entry.insert(0, self._search_placeholder)
        self._search_entry.config(fg=C_TEXT_DISABLED)
        self._search_entry.bind("<FocusIn>", self._on_search_focus_in)
        self._search_entry.bind("<FocusOut>", self._on_search_focus_out)
        # 一键清除（×）：仅非占位态显示
        self._search_clear = tk.Label(
            _search_frame, text="×", font=("", 10, "bold"),
            bg=C_CARD_BG, fg=C_TEXT_MUTED, padx=5, cursor="hand2")
        self._search_clear.pack(side=tk.LEFT)
        self._search_clear.bind("<Button-1>", lambda e: self._on_search_clear())
        self._search_clear.pack_forget()  # 初始为占位态，隐藏
        self._reg_themed(self._search_clear, "card", "muted")

        # 筛选下拉
        self._filter_var = tk.StringVar(value="全部")
        self._filter_combo = ttk.Combobox(
            toolbar, textvariable=self._filter_var,
            values=["全部", "启用", "禁用", "运行中", "失败"],
            state="readonly", width=8, style="TCombobox")
        self._filter_combo.pack(side=tk.LEFT, padx=(12, 0))
        self._filter_combo.bind("<<ComboboxSelected>>", self._on_filter_changed)

        # 中间占位
        _spacer = tk.Frame(toolbar, bg=C_BG)
        _spacer.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._reg_themed(_spacer, "page")

        # 右：已启用计数 + 调度器开关
        self._enabled_lbl = tk.Label(toolbar, text="0/0 个已启用",
                                     font=("", 9), fg=C_TEXT_MUTED, bg=C_BG)
        self._enabled_lbl.pack(side=tk.RIGHT, padx=(12, 0))
        self._reg_themed(self._enabled_lbl, "page", "muted")

        self._sched_btn = ttk.Button(
            toolbar, text="启动调度", style="Outline.TButton",
            command=self._toggle_scheduler)
        self._sched_btn.pack(side=tk.RIGHT, padx=(6, 0))
        Tooltip(self._sched_btn, "启动/停止定时调度器")

        # ---- 3. 卡片式任务列表 + 日志面板（PanedWindow 可拖拽分隔）----
        # sash 填充色取 PanedWindow 自身的 -bg：若与 pane 同底色，4px 分隔条
        # 完全不可见（只在鼠标移上去变双箭头时才暴露"这里有根看不见的杆"）。
        # 故用 border 色作 sash 底，并改由 _apply_theme 随主题重配。
        self._pane = tk.PanedWindow(
            self.root, orient=tk.VERTICAL, bg=C_BORDER,
            sashwidth=4, sashrelief=tk.SOLID, borderwidth=0)
        self._pane.pack(fill=tk.BOTH, expand=True, padx=14, pady=4)

        # 上半区：卡片列表
        list_outer = tk.Frame(self._pane, bg=C_BG)
        self._pane.add(list_outer, minsize=120, stretch="always")
        self._reg_themed(list_outer, "page")

        self._task_canvas = tk.Canvas(list_outer, bg=C_BG, highlightthickness=0,
                                      bd=0, borderwidth=0)
        self._reg_themed(self._task_canvas, "page")
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

        # ---- 4. 运行日志（PanedWindow 下半区，可拖拽调节高度，可折叠）----
        log_outer = tk.Frame(self._pane, bg=C_BG)
        self._pane.add(log_outer, minsize=60, stretch="never")
        self._reg_themed(log_outer, "page")

        log_header = tk.Frame(log_outer, bg=C_BG)
        log_header.pack(fill=tk.X)
        self._reg_themed(log_header, "page")

        _log_title = tk.Label(log_header, text="运行日志", font=("", 9, "bold"),
                              fg=C_TEXT, bg=C_BG)
        _log_title.pack(side=tk.LEFT)
        self._reg_themed(_log_title, "page", "text")

        self._log_collapsed = False
        self._log_toggle_btn = tk.Label(
            log_header, text="▼", font=("", 8), fg=C_TEXT_MUTED,
            bg=C_BG, cursor="hand2")
        self._log_toggle_btn.pack(side=tk.RIGHT)
        self._log_toggle_btn.bind("<Button-1>", self._toggle_log_panel)
        self._reg_themed(self._log_toggle_btn, "page", "muted")

        self._log_body = tk.Frame(log_outer, bg=C_BG)
        self._log_body.pack(fill=tk.BOTH, expand=True)
        self._reg_themed(self._log_body, "page")

        self.log_text = scrolledtext.ScrolledText(
            self._log_body, height=5, state=tk.DISABLED,
            font=("Consolas", 9), bg=C_CARD_BG, fg=C_TEXT,
            bd=0, relief=tk.FLAT, highlightthickness=1,
            highlightbackground=C_BORDER, highlightcolor=C_BRAND,
            insertbackground=C_TEXT, padx=4, pady=2)
        self.log_text.pack(fill=tk.BOTH, expand=True, pady=(2, 0))
        # log_text 不入 _themed：其底色/字色/级别色需成组重配，
        # 由 _apply_theme 单独处理（含 tag 提亮）

        # P1: 日志按级别着色（ERROR 红 / WARN 橙），提升扫读性
        self.log_text.tag_configure("error", foreground=C_LOG_ERROR)
        self.log_text.tag_configure("warn", foreground=C_LOG_WARN)

        # P1: ↑↓ 方向键在卡片间移动选中（无鼠标操作可达）
        self.root.bind("<Up>", lambda e: self._on_arrow(-1))
        self.root.bind("<Down>", lambda e: self._on_arrow(1))

        # ---- 4.5 批量操作栏（多选时显示）----
        self._batch_bar = tk.Frame(self.root, bg=C_CARD_BG, height=36)
        self._batch_bar.pack(fill=tk.X, side=tk.BOTTOM, padx=14, pady=(0, 4))
        self._batch_bar.pack_forget()
        self._batch_bar.pack_propagate(False)
        self._reg_themed(self._batch_bar, "card")

        batch_lbl = tk.Label(self._batch_bar, text="", font=("", 9),
                             fg=C_TEXT_MUTED, bg=C_CARD_BG)
        batch_lbl.pack(side=tk.LEFT, padx=(12, 8))
        self._batch_count_lbl = batch_lbl
        self._reg_themed(batch_lbl, "card", "muted")

        ttk.Button(self._batch_bar, text="批量启用", style="Outline.TButton",
                   command=lambda: self._batch_enable(True)).pack(side=tk.LEFT, padx=2)
        ttk.Button(self._batch_bar, text="批量禁用", style="Outline.TButton",
                   command=lambda: self._batch_enable(False)).pack(side=tk.LEFT, padx=2)
        ttk.Button(self._batch_bar, text="批量运行", style="Accent.TButton",
                   command=self._batch_run).pack(side=tk.LEFT, padx=2)
        ttk.Button(self._batch_bar, text="清除选择", style="Outline.TButton",
                   command=self._clear_selection).pack(side=tk.RIGHT, padx=2)

        # ---- 5. 底部状态栏 ----
        self._status_bar = tk.Frame(self.root, bg=C_CARD_BG, height=24)
        self._status_bar.pack(fill=tk.X, side=tk.BOTTOM)
        self._status_bar.pack_propagate(False)
        self._reg_themed(self._status_bar, "card")

        self._status_label = tk.Label(self._status_bar, text="调度器：已停止",
                                      font=("", 8), fg=C_TEXT_MUTED,
                                      bg=C_CARD_BG, anchor="w")
        self._status_label.pack(side=tk.LEFT, padx=12)
        self._reg_themed(self._status_label, "card", "muted")

        self._task_count_lbl = tk.Label(self._status_bar, text="任务: 0",
                                        font=("", 8), fg=C_TEXT_MUTED,
                                        bg=C_CARD_BG)
        self._task_count_lbl.pack(side=tk.LEFT, padx=(12, 0))
        self._reg_themed(self._task_count_lbl, "card", "muted")

        self._last_sync_lbl = tk.Label(self._status_bar, text="",
                                       font=("", 8), fg=C_TEXT_MUTED,
                                       bg=C_CARD_BG)
        self._last_sync_lbl.pack(side=tk.LEFT, padx=(12, 0))
        self._reg_themed(self._last_sync_lbl, "card", "muted")

        self._next_lbl_bar = tk.Label(self._status_bar, text="", font=("", 8),
                                      fg=C_TEXT_MUTED, bg=C_CARD_BG)
        self._next_lbl_bar.pack(side=tk.LEFT, padx=(12, 0))
        self._reg_themed(self._next_lbl_bar, "card", "muted")

        self._ver_lbl = tk.Label(self._status_bar,
                                 text="v%s" % APP_VERSION,
                                 font=("", 8), fg=C_TEXT_DISABLED,
                                 bg=C_CARD_BG)
        self._ver_lbl.pack(side=tk.RIGHT, padx=12)
        self._reg_themed(self._ver_lbl, "card", "disabled")

    def _toggle_log_panel(self, _evt=None):
        # type: (object) -> None
        """折叠/展开日志面板：折叠时隐藏日志主体，释放列表空间。"""
        if not hasattr(self, "_log_body") or self._log_body is None:
            return
        try:
            if self._log_collapsed:
                self._log_body.pack(fill=tk.BOTH, expand=True)
                self._log_toggle_btn.config(text="▼")
                self._log_collapsed = False
            else:
                self._log_body.pack_forget()
                self._log_toggle_btn.config(text="▶")
                self._log_collapsed = True
        except tk.TclError:
            pass

    def _reg_themed(self, widget, bg_role=None, fg_role=None):
        # type: (Any, Optional[str], Optional[str]) -> None
        """登记需随深浅主题重配底色/字色的 tk 部件。

        bg_role/fg_role 取 _THEME_BG/_THEME_FG 的键；None 表示该维度
        不参与（如纯容器只有底色）。布局层每新建一个 tk 部件就登记，
        避免新增控件时忘记接主题（这正是深色模式半残的根因）。
        """
        self._themed.append((widget, bg_role, fg_role))

    def _apply_theme(self):
        # type: () -> None
        """按当前 _dark_mode 重配全部已登记部件（ttk 由 _setup_style 负责）。"""
        dark = bool(getattr(self, "_dark_mode", False))
        for w, bg_role, fg_role in self._themed:
            if w is None:
                continue
            try:
                if bg_role:
                    w.configure(bg=_THEME_BG[dark][bg_role])
                if fg_role:
                    w.configure(fg=_THEME_FG[dark][fg_role])
            except tk.TclError:
                pass
        # PanedWindow 不在登记表内：它的 bg 是 sash 填充色而非页面底色，
        # 混入 page 角色会让 4px 分隔条重新变得不可见
        pane = getattr(self, "_pane", None)
        if pane is not None:
            try:
                pane.configure(bg=_THEME_BG[dark]["border"])
            except tk.TclError:
                pass
        # 日志级别色在深底上须用亮色，否则 C_LOG_ERROR 深红几乎不可读；
        # 其 1px 描边同样来自 highlightbackground（bd=0 后已无系统 3D 边）
        log_text = getattr(self, "log_text", None)
        if log_text is not None:
            try:
                log_text.configure(
                    bg=_THEME_BG[dark]["card"],
                    fg=_THEME_FG[dark]["text"],
                    highlightbackground=_THEME_BG[dark]["border"],
                    insertbackground=_THEME_FG[dark]["text"])
                log_text.tag_configure(
                    "error", foreground=(C_LOG_ERROR_LIGHT if dark else C_LOG_ERROR))
                log_text.tag_configure(
                    "warn", foreground=(C_LOG_WARN_LIGHT if dark else C_LOG_WARN))
            except tk.TclError:
                pass
        # 搜索框描边与光标色：bd=0 后其边框完全由 highlightbackground 决定
        entry = getattr(self, "_search_entry", None)
        if entry is not None:
            try:
                entry.configure(
                    highlightbackground=_THEME_BG[dark]["border"],
                    highlightcolor=C_BRAND,
                    insertbackground=_THEME_FG[dark]["text"])
            except tk.TclError:
                pass
        # 占位提示态须保持"禁用色"语义：entry 登记的是正常 text 色，
        # 直接重配会让提示文字与真实输入同色，用户分不清框内是提示还是
        # 自己打的字
        ph = getattr(self, "_search_placeholder", None)
        if entry is not None and ph is not None:
            try:
                if entry.get() == ph:
                    entry.configure(fg=_THEME_FG[dark]["disabled"])
            except tk.TclError:
                pass

    def _toggle_dark_mode(self):
        # type: () -> None
        """切换深色/浅色模式：重配 ttk.Style + 已登记的 tk 部件。"""
        self._dark_mode = not getattr(self, "_dark_mode", False)
        self._dark_btn.config(text="深色" if not self._dark_mode else "浅色")
        self._setup_style()
        self._apply_theme()
        self.root.configure(bg=_THEME_BG[self._dark_mode]["page"])
        self._refresh_tasks(full=True)

    # ---- ttk.Style 配置 ----
    def _setup_style(self):
        # type: () -> None
        style = ttk.Style(self.root)
        # 选一个基础主题再覆盖（跨平台兼容）
        for theme in ("clam", "vista", "winnative", "default"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break

        dark = getattr(self, "_dark_mode", False)
        if dark:
            from gui_tasklist import (
                C_DARK_BG, C_DARK_CARD_BG, C_DARK_CARD_BG_ALT,
                C_DARK_CARD_HOVER, C_DARK_TEXT, C_DARK_TEXT_MUTED,
                C_DARK_TEXT_DISABLED, C_DARK_BORDER, C_DARK_BRAND_LIGHT,
            )
            bg = C_DARK_BG
            card_bg = C_DARK_CARD_BG
            card_bg_alt = C_DARK_CARD_BG_ALT
            card_hover = C_DARK_CARD_HOVER
            text = C_DARK_TEXT
            text_muted = C_DARK_TEXT_MUTED
            text_disabled = C_DARK_TEXT_DISABLED
            border = C_DARK_BORDER
            brand_light = C_DARK_BRAND_LIGHT
        else:
            bg = C_BG
            card_bg = C_CARD_BG
            card_bg_alt = C_CARD_BG_ALT
            card_hover = C_CARD_HOVER
            text = C_TEXT
            text_muted = C_TEXT_MUTED
            text_disabled = C_TEXT_DISABLED
            border = C_BORDER
            brand_light = C_BRAND_LIGHT

        # ---------- 字体约定 ----------
        # F_NORMAL  = ("", 9)      # 正文
        # F_BOLD    = ("", 9, "bold")  # 强调
        # F_TITLE   = ("", 11, "bold") # 卡片标题
        # F_SMALL   = ("", 8)      # 状态栏 / 辅助
        # F_BRAND   = ("", 14, "bold") # 品牌名

        # ---------- 卡片容器 ----------
        # 去边框纯色块风格：卡片不描边，边界完全由"页面底 vs 卡片底"的色差
        # 与卡片间距界定。原先 relief=SOLID+borderwidth=1 的描边会带来两个
        # 问题：一是 SOLID 只在下/右侧显色形成不对称"半边框"，二是边框色
        # 取自 clam 主题暖灰、与本项目中性灰不搭；改为无边框后一并消失。
        # 色差下限由 test_45 的 WCAG 对比度断言守住（浅色须 >= 1.15）。
        style.configure("Card.TFrame", background=card_bg,
                        relief=tk.FLAT, borderwidth=0)
        style.configure("CardSelected.TFrame", background=brand_light,
                        relief=tk.FLAT, borderwidth=0)
        # 悬停态：比选中更淡的主色底（TaskCard 悬停联动用）
        style.configure("CardHover.TFrame", background=card_hover,
                        relief=tk.FLAT, borderwidth=0)
        style.configure("CardHost.TFrame", background=bg)

        # ---------- TButton（全局默认） ----------
        style.configure("TButton", font=("", 9), padding=4)
        style.map("TButton",
                  background=[("active", card_bg_alt)])

        # Accent：主色实心按钮（添加任务 / 运行 / 确认执行）
        style.configure("Accent.TButton", font=("", 9, "bold"),
                        background=C_BRAND, foreground=C_WHITE,
                        padding=(12, 5))
        style.map("Accent.TButton",
                  background=[("active", C_BRAND_DARK),
                              ("disabled", text_disabled)],
                  foreground=[("disabled", C_WHITE)])

        # Outline：白底描边按钮（编辑 / 运行全部 / 调度开关）
        # 描边按钮在卡片上每张 3 个，是全窗口出现频次最高的"线"，同样需对称配色
        style.configure("Outline.TButton", font=("", 9),
                        background=card_bg, foreground=text,
                        relief=tk.SOLID, borderwidth=1, padding=(12, 5),
                        lightcolor=border, darkcolor=border)
        style.map("Outline.TButton",
                  background=[("active", card_bg_alt),
                              ("disabled", bg)],
                  foreground=[("disabled", text_disabled)])

        # Danger：删除按钮（红字白底）
        style.configure("Danger.TButton", font=("", 9),
                        background=card_bg, foreground=C_DELETE,
                        relief=tk.SOLID, borderwidth=1, padding=(12, 5),
                        lightcolor=border, darkcolor=border)
        style.map("Danger.TButton",
                  background=[("active", C_DELETE_HOVER)],
                  foreground=[("disabled", text_disabled)])

        # BrandBtn：品牌栏内白字透明底（设置）
        style.configure("Brand.TButton", font=("", 9),
                        background=C_BRAND, foreground=C_WHITE,
                        relief=tk.FLAT, borderwidth=0, padding=(10, 6))
        style.map("Brand.TButton",
                  background=[("active", C_BRAND_DARK)])

        # ---------- TLabel ----------
        style.configure("TLabel", font=("", 9), foreground=text)
        style.configure("Muted.TLabel", font=("", 9), foreground=text_muted)
        style.configure("Small.TLabel", font=("", 8), foreground=text_muted)
        style.configure("Title.TLabel", font=("", 11, "bold"), foreground=text)
        style.configure("Error.TLabel", font=("", 9), foreground=C_DELETE)
        style.configure("Brand.TLabel", font=("", 14, "bold"),
                        foreground=C_WHITE, background=C_BRAND)
        style.configure("BrandSub.TLabel", font=("", 10),
                        foreground=C_BRAND_SUB, background=C_BRAND)

        # ---------- TLabelframe（对话框分组） ----------
        # clam 默认 relief=raised + borderwidth=2 + 暖灰 #dcdad5 底：2px 立体
        # 斜角且底色不随主题变（深色模式下成一块浅灰立体方块）。用 GROOVE
        # 1px 细边 + 显式 bordercolor/底色。
        style.configure("TLabelframe", font=("", 9), background=bg,
                        bordercolor=border, lightcolor=border,
                        darkcolor=border, borderwidth=1,
                        relief=tk.GROOVE)
        style.configure("TLabelframe.Label", font=("", 9, "bold"),
                        background=bg)

        # ---------- TNotebook（TaskDialog 选项卡） ----------
        style.configure("TNotebook", background=bg)
        style.configure("TNotebook.Tab", font=("", 9), padding=(12, 4))
        style.map("TNotebook.Tab",
                  background=[("selected", card_bg),
                              ("!selected", bg)],
                  foreground=[("selected", text),
                              ("!selected", text_muted)])

        # ---------- Treeview（DiffDialog 差异列表） ----------
        style.configure("Treeview", font=("", 9), rowheight=24,
                        background=card_bg, fieldbackground=card_bg)
        style.configure("Treeview.Heading", font=("", 9, "bold"))
        style.map("Treeview",
                  background=[("selected", brand_light)],
                  foreground=[("selected", text)])

        # ---------- TScrollbar ----------
        # 不给 width 时 clam 令控件总宽恰等于 arrowsize（实测 14px），
        # 叠加 borderwidth=0 后滑块成了两端贴死轨道的无边框窄条
        style.configure("Vertical.TScrollbar", background=border,
                        troughcolor=bg, borderwidth=0,
                        arrowsize=12, width=16)

        # ---------- TCombobox / TEntry ----------
        style.configure("TCombobox", font=("", 9))
        style.configure("TEntry", font=("", 9))

        # ---------- TSpinbox ----------
        style.configure("TSpinbox", font=("", 9))


class Tooltip(object):
    """轻量工具提示：悬停 500ms 后显示，离开即消失。"""

    def __init__(self, widget, text):
        # type: (tk.Widget, str) -> None
        self._widget = widget
        self._text = text
        self._tip = None            # type: Optional[tk.Toplevel]
        self._id = None             # type: Optional[str]
        widget.bind("<Enter>", self._on_enter)
        widget.bind("<Leave>", self._on_leave)

    def _on_enter(self, _evt=None):
        # type: (object) -> None
        if self._id is not None:
            return
        try:
            self._id = self._widget.after(500, self._show)
        except tk.TclError:
            pass

    def _on_leave(self, _evt=None):
        # type: (object) -> None
        if self._id is not None:
            try:
                self._widget.after_cancel(self._id)
            except tk.TclError:
                pass
            self._id = None
        self._hide()

    def _show(self):
        # type: () -> None
        try:
            x = self._widget.winfo_rootx() + 20
            y = self._widget.winfo_rooty() + self._widget.winfo_height() + 4
            tip = tk.Toplevel(self._widget)
            tip.wm_overrideredirect(True)
            tip.wm_geometry("+%d+%d" % (x, y))
            # bd=0 + 1px highlight 描边（与搜索框/日志框同一套路）：
            # relief=SOLID 的斜角边取系统 System3D 色，无法主题化
            tk.Label(tip, text=self._text, font=("", 8),
                     bg=C_TIP_BG, fg=C_TIP_FG,
                     bd=0, relief=tk.FLAT, highlightthickness=1,
                     highlightbackground=C_BORDER, highlightcolor=C_BRAND,
                     padx=6, pady=3).pack()
            self._tip = tip
        except tk.TclError:
            pass

    def _hide(self):
        # type: () -> None
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None
