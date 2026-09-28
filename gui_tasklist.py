"""任务卡片组件与共享配色常量。

从 gui_app.py 拆出的 UI 渲染层：
- 配色变量（青绿色主题）
- _ellipsize_end/_ellipsize_mid 按像素宽度省略文本
- TaskCard 单张任务卡片（Canvas 嵌入 Frame，可滚动）
- _MODE_LABEL 模式显示标签

gui_app.App 与 TaskCard 共享同一套配色常量，此处为唯一来源。
"""

import sys

import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Optional, Tuple

from config import MODE_ONE_WAY, MODE_TWO_WAY, Task
from scheduler import Scheduler
from utils.timeutil import format_epoch

_MODE_LABEL = {MODE_ONE_WAY: "单向镜像", MODE_TWO_WAY: "双向同步"}

# ---------- 配色变量（青绿色主题） ----------
C_BRAND = "#26A69A"           # 主色：青绿色（按钮/开关激活/状态对勾）
C_BRAND_DARK = "#1E8E84"      # 主色 hover
C_BRAND_LIGHT = "#E0F2F1"     # 主色淡底（卡片选中态）
C_DELETE = "#E57373"          # 删除按钮边框
C_WARN = "#FFB74D"            # 警告/待处理
C_OK = "#26A69A"              # 成功对勾（同主色）
C_BG = "#ECECEC"              # 页面背景（去边框风格：须与卡片底有可辨色差）
C_CARD_BG = "#FFFFFF"         # 卡片背景
C_CARD_BG_ALT = "#F5F5F5"     # 卡片交替背景
C_CARD_HOVER = "#F0F7F6"      # 卡片悬停底色（比选中态 C_BRAND_LIGHT 更淡）
C_TEXT = "#212121"            # 主文字
C_TEXT_MUTED = "#757575"      # 辅助文字（路径/调度信息）
C_TEXT_DISABLED = "#BDBDBD"   # 禁用态文字
C_BORDER = "#E0E0E0"          # 分隔线
C_SWITCH_OFF = "#BDBDBD"      # 开关关闭
C_SWITCH_ON = "#26A69A"       # 开关打开
C_WHITE = "#FFFFFF"           # 白（品牌栏文字 / 输入框底）
C_BRAND_SUB = "#E0F2F1"       # 品牌栏副标题文字（同 C_BRAND_LIGHT）
C_LOG_ERROR = "#C62828"       # 日志 ERROR 级别（深红，比 C_DELETE 更醒目）
C_LOG_WARN = "#E65100"        # 日志 WARN 级别（深橙）
# 深色底上的日志级别色：浅色版的深红/深橙在暗背景上对比度不足，
# 需提亮一档才能扫读（见 LayoutMixin._apply_theme）
C_LOG_ERROR_LIGHT = "#FF8A80"
C_LOG_WARN_LIGHT = "#FFB74D"
C_DELETE_HOVER = "#FFEBEE"    # 危险按钮 hover 底色
C_SWITCH_KNOB = "#D0D0D0"     # 开关滑块描边

# ---------- 深色模式配色 ----------
C_DARK_BG = "#1E1E1E"          # 深色页面背景
C_DARK_CARD_BG = "#2D2D2D"     # 深色卡片背景
C_DARK_CARD_BG_ALT = "#333333" # 深色卡片交替背景
C_DARK_CARD_HOVER = "#383838"  # 深色卡片悬停底色
C_DARK_TEXT = "#E0E0E0"        # 深色主文字
C_DARK_TEXT_MUTED = "#A0A0A0"  # 深色辅助文字
C_DARK_TEXT_DISABLED = "#606060" # 深色禁用态文字
C_DARK_BORDER = "#404040"      # 深色分隔线
C_DARK_BRAND_LIGHT = "#1A3A38" # 深色选中态底色

def _switch_radius(ch):
    # type: (int) -> int
    """开关滑块半径（按轨道高推导，纯函数可无头测试）。

    轨道上下各留 2px，轨道高 = ch - 8。滑块半径须由轨道高反推而非 Canvas
    高：按 ch//2-2 算（ch=24 → r=10，直径 20）会比轨道（16px）高出 4px，
    白球上下各溢出 2px 压出轨道边缘。这里再留 1px 呼吸位使滑块不贴边。
    """
    track_h = ch - 8
    return max(1, track_h // 2 - 1)


# ---------- 卡片间距（单一来源） ----------
# 去边框后卡片边界全靠色差 + 间距界定，间距过紧会让相邻卡片糊成一片；
# 三个 pack 点必须同值，故收敛为常量而非各写各的。
CARD_PADX = 4
CARD_PADY = 5

# ---------- 工具：按像素宽度省略文本 ----------
# tk.Label 没有原生 ellipsize，超宽即硬裁（无省略号）；按固定字符数截断
# 又与实际字号/DPI 脱节（中英文宽度差近一倍）。故按字体实测像素做二分
# 裁剪。measure 由调用方注入（tkfont.Font.measure），保持纯函数可无头测试。

_ELLIPSIS = "…"


def _ellipsize_end(text, measure, max_px):
    # type: (str, Callable[[str], int], int) -> str
    """末尾省略：保留前缀 + 省略号（用于任务名，尾部用于区分同名任务）。

    严格契约：返回值宽度恒 <= max_px。连一个省略号都放不下时返回空串
    （而非硬塞省略号越界）——实践中卡片可用宽度有数百 px，该分支仅防御。
    """
    if max_px <= 0:
        return ""
    if measure(text) <= max_px:
        return text
    if measure(_ELLIPSIS) > max_px:
        return ""
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if measure(text[:mid]) + measure(_ELLIPSIS) <= max_px:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + _ELLIPSIS


def _ellipsize_mid(text, measure, max_px):
    # type: (str, Callable[[str], int], int) -> str
    """中间省略：保留首尾段 + 省略号（用于路径，首尾目录名才是识别线索）。

    两段按保留字符数均分（头多一个），随保留字符数单调变宽，故可二分。
    同样遵守"返回宽度恒 <= max_px"；"首字符+省略号"都放不下时返回空串，
    避免出现"…/…"这种既丢头又丢尾的最差结果。
    """
    if max_px <= 0:
        return ""
    if measure(text) <= max_px:
        return text
    if measure(_ELLIPSIS) + measure(text[:1]) > max_px:
        return ""
    lo, hi = 1, len(text) - 1
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        head = (mid + 1) // 2
        tail = mid - head
        cand = (text[:head] + _ELLIPSIS + text[len(text) - tail:]) if tail \
            else (text[:head] + _ELLIPSIS)
        if measure(cand) <= max_px:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if best == 0:
        return ""
    head = (best + 1) // 2
    tail = best - head
    return text[:head] + _ELLIPSIS + (text[len(text) - tail:] if tail else "")


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
        # type: (tk.Widget, Any, str) -> None
        # 卡片本体 padding=0：ttk.Frame 的 -padding 同时充当 place 的坐标原点
        # 与内边距，padding 非零时选中指示条 place(x=0, relheight=1.0) 会落在
        # x=14/y=10 处、且高度只算 padding 后的内容高，永远贴不到卡片左缘。
        # 故把内缩移到内层 body，边框交给本体、内容布局交给 body。
        super().__init__(master, style="Card.TFrame", padding=0)
        self._app = app
        self._id = task_id

        # ---- 选中指示条：左侧 4px 主色竖条 ----
        # 先于 body 创建并用 place 悬浮（不参与 pack，不挤压内容）。
        # 高度用 relheight 跟随卡片实测高度——写死像素会在卡片变高/变矮时
        # 越界，曾用 height=200 而卡片仅 ~70px 高，导致竖线横穿后续卡片。
        self._sel_bar = tk.Frame(self, bg=C_BRAND, width=4)

        # ---- 内层 body：承载内容内缩 ----
        body = ttk.Frame(self, style="Card.TFrame", padding=(14, 8))
        body.pack(fill=tk.BOTH, expand=True)

        # ---- 右侧固定区：状态 / 下次运行 / 操作按钮 ----
        # pack 顺序关键：固定的右侧必须先于可伸缩的左侧 pack。tk.pack 按调用
        # 顺序分配 cavity，空间不足时先到的 expand 控件会吃掉全部剩余、
        # 后 pack 的固定控件被压成 0 宽（按钮直接消失）。此前顺序相反，
        # 最小窗口宽度(760px)下路径行需求超出可用宽度 78px，运行/编辑/删除
        # 三键即被裁掉。
        self._right = ttk.Frame(body, style="Card.TFrame")
        self._right.pack(side=tk.RIGHT, padx=(8, 0), anchor=tk.N)

        # 上：状态标记
        self._status_lbl = tk.Label(self._right, font=("", 10, "bold"),
                                    bg=C_CARD_BG, anchor="e")
        self._status_lbl.pack(anchor=tk.E)

        # 中：下次运行
        self._next_lbl = tk.Label(self._right, font=("", 9), fg=C_TEXT_MUTED,
                                  bg=C_CARD_BG, anchor="e")
        self._next_lbl.pack(anchor=tk.E)

        # 下：操作按钮组（运行 / 编辑 / 删除）
        self._btn_frame = ttk.Frame(self._right, style="Card.TFrame")
        self._btn_frame.pack(anchor=tk.E, pady=(6, 0))

        self._run_btn = ttk.Button(
            self._btn_frame, text="▶ 运行", width=6,
            style="Accent.TButton", command=self._on_run)
        self._run_btn.pack(side=tk.LEFT, padx=(0, 4))

        self._edit_btn = ttk.Button(
            self._btn_frame, text="编辑", width=6,
            style="Outline.TButton", command=self._on_edit)
        self._edit_btn.pack(side=tk.LEFT, padx=(0, 4))

        self._del_btn = ttk.Button(
            self._btn_frame, text="删除", width=6,
            style="Danger.TButton", command=self._on_delete)
        self._del_btn.pack(side=tk.LEFT)

        # ---- 左侧可伸缩区：四层信息（层1 识别 / 层2 路径 / 层3 调度） ----
        self._left = ttk.Frame(body, style="Card.TFrame")
        self._left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # 层1：开关 + 任务名 + 模式徽标
        name_row = ttk.Frame(self._left, style="Card.TFrame")
        name_row.pack(anchor=tk.W, fill=tk.X)

        self._sw_canvas = tk.Canvas(name_row, width=44, height=24,
                                    bg=C_CARD_BG, highlightthickness=0,
                                    bd=0)
        self._sw_canvas.pack(side=tk.LEFT, anchor=tk.W, padx=(0, 10))
        self._sw_canvas.bind("<Button-1>", self._on_toggle_switch)

        self._name_lbl = tk.Label(name_row, font=("", 11, "bold"),
                                  fg=C_TEXT, bg=C_CARD_BG, anchor="w")
        self._name_lbl.pack(side=tk.LEFT)

        # P0-5 修复：模式徽标（原表格有"方向"列，卡片化后回归）
        self._mode_lbl = tk.Label(name_row, font=("", 8),
                                  fg=C_TEXT_MUTED, bg=C_CARD_BG, anchor="w")
        self._mode_lbl.pack(side=tk.LEFT, padx=(8, 0))

        # 层2：路径独占一行（最易变化且需完整可读，独立成行不与时间信息挤占）
        detail_row = ttk.Frame(self._left, style="Card.TFrame")
        detail_row.pack(anchor=tk.W, fill=tk.X)

        self._path_lbl = tk.Label(detail_row, font=("", 9), fg=C_TEXT_MUTED,
                                  bg=C_CARD_BG, anchor="w")
        self._path_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # 层3：调度 + 上次运行
        sched_row = ttk.Frame(self._left, style="Card.TFrame")
        sched_row.pack(anchor=tk.W, fill=tk.X)

        self._sched_lbl = tk.Label(sched_row, font=("", 9), fg=C_TEXT_MUTED,
                                   bg=C_CARD_BG, anchor="w")
        self._sched_lbl.pack(side=tk.LEFT)

        # 右键菜单 / 选中 / 双击运行 绑定（整卡都响应）
        # C2 修复：_sw_canvas 必须从本循环剔除——tkinter bind 是替换语义，
        # 若在此对 _sw_canvas 再绑 <Button-1>，会覆盖 __init__ 中开关的切换
        # 绑定，导致开关失效；且会绑 <Double-Button-1> 让快速点两下开关误触
        # 同步。开关仅保留单独的切换绑定与右键菜单。
        for w in (self, body, self._left, self._right, name_row,
                  detail_row, sched_row, self._name_lbl, self._mode_lbl,
                  self._path_lbl, self._sched_lbl, self._next_lbl,
                  self._btn_frame, self._status_lbl):
            try:
                w.bind("<Button-3>", self._on_context_menu)
                w.bind("<Button-1>", self._on_click)
                # P1: 双击卡片 = 立即同步（与"运行"按钮同路径）
                w.bind("<Double-Button-1>", self._on_dblclick)
            except tk.TclError:
                pass
        # 开关自身的右键菜单（不覆盖其 <Button-1> 切换绑定）
        try:
            self._sw_canvas.bind("<Button-3>", self._on_context_menu)
        except tk.TclError:
            pass

        # 选择态高亮：绑定 1px 实线边框
        self._selected = False
        self.bind("<Configure>", self._on_configure)

        # ---- 悬停高亮（视觉质感）：非选中态悬停显极淡主色底 ----
        # Enter/Leave 在子部件间移动会成对触发，用防抖 after 合并，
        # 避免高频重绘抖动；选中态不参与（保留选中底色）
        self._hover = False
        self._hover_after = None  # type: Optional[str]
        self._hover_widgets = (self, body, self._left, self._right, name_row,
                               detail_row, sched_row, self._name_lbl,
                               self._mode_lbl, self._path_lbl, self._sched_lbl,
                               self._next_lbl, self._btn_frame,
                               self._status_lbl)
        for w in self._hover_widgets:
            try:
                w.bind("<Enter>", self._on_hover_enter)
                w.bind("<Leave>", self._on_hover_leave)
            except tk.TclError:
                pass
        # bg 重绘仅针对 tk.Label（ttk 部件底色由 CardHover.TFrame 样式统一控制，
        # 对 ttk.Frame 逐个 configure(bg=) 是无效选项）
        self._hover_labels = (self._name_lbl, self._mode_lbl, self._path_lbl,
                              self._sched_lbl, self._next_lbl,
                              self._status_lbl)

        # 卡片内所有 ttk 容器：ttk.Frame 背景不透明，style 固定在
        # Card.TFrame 就不会跟随卡片自身 style 变化——选中/悬停时卡片本体
        # 变淡青绿而这些容器仍为白底，文字之间露出白色间隙（斑驳感）。
        # 故统一登记，set_selected/_apply_hover/_on_hover_leave 一并切换。
        self._card_frames = (body, self._left, self._right, name_row,
                             detail_row, sched_row, self._btn_frame)

        # ---- 文本自适应：按可用像素重算省略（不再按固定字符数硬截） ----
        # refresh 只存全量文本到 *_full，由 _refit_text 结合卡片实测宽度
        # 决定显示多少；宽度变化时 <Configure> 会重算。
        self._name_full = ""
        self._path_full = ""
        self._fonts = None  # type: Optional[Tuple[Any, Any, Any]]

    def _set_card_style(self, style_name):
        # type: (str) -> None
        """同步切换卡片本体与全部内部 ttk 容器的样式。"""
        try:
            self.configure(style=style_name)
            for f in self._card_frames:
                f.configure(style=style_name)
        except tk.TclError:
            pass

    # ---- 悬停高亮 ----
    def _on_hover_enter(self, _evt=None):
        # type: (object) -> None
        self._hover = True
        if self._hover_after is not None:
            return
        # 40ms 防抖：极短延迟内 Leave 会撤销，未撤销才真正重绘
        self._hover_after = self.after(40, self._apply_hover)

    def _on_hover_leave(self, _evt=None):
        # type: (object) -> None
        self._hover = False
        if self._hover_after is not None:
            # 悬停尚未生效即离开：撤销待执行的重绘即可
            try:
                self.after_cancel(self._hover_after)
            except Exception:
                pass
            self._hover_after = None
            return
        if self._selected:
            return
        # 悬停已生效后离开：恢复白底
        try:
            self._set_card_style("Card.TFrame")
            for w in self._hover_labels:
                try:
                    w.configure(bg=C_CARD_BG)
                except tk.TclError:
                    pass
            self._sw_canvas.configure(bg=C_CARD_BG)
        except tk.TclError:
            pass

    def _apply_hover(self):
        # type: () -> None
        self._hover_after = None
        if self._selected or not self._hover:
            return
        try:
            self._set_card_style("CardHover.TFrame")
            for w in self._hover_labels:
                try:
                    w.configure(bg=C_CARD_HOVER)
                except tk.TclError:
                    pass
            self._sw_canvas.configure(bg=C_CARD_HOVER)
        except tk.TclError:
            pass

    def _cancel_hover(self):
        # type: () -> None
        """撤销未触发的悬停重绘（选中/取消选中时调用）。"""
        if self._hover_after is not None:
            try:
                self.after_cancel(self._hover_after)
            except Exception:
                pass
            self._hover_after = None
        self._hover = False

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
        add = False
        if _evt is not None:
            state = getattr(_evt, "state", 0)
            add = bool(state & 0x0004)
        self._app._select_card(self._id, add=add)

    def _on_dblclick(self, _evt=None):
        self._app._select_card(self._id)
        self._app._run_card_task(self._id)

    def _on_configure(self, _evt=None):
        # 卡片宽度变化（窗口缩放/重排）时按新宽度重算名称与路径的省略量
        self._refit_text()

    def _measure_fonts(self):
        # type: () -> Tuple[Any, Any, Any]
        """惰性创建三个字体的测量器（tkfont.Font 持有需长期存活，故缓存）。"""
        if self._fonts is None:
            import tkinter.font as tkfont
            self._fonts = (
                tkfont.Font(family="", size=11, weight="bold"),   # 名称
                tkfont.Font(family="", size=9),                    # 路径
                tkfont.Font(family="", size=8),                    # 模式徽标
            )
        return self._fonts

    def _refit_text(self):
        # type: () -> None
        """按当前可用像素重算名称（末尾省略）与路径（中间省略）显示文本。

        可用宽度 = 卡片实测宽 - 右侧固定区 - 开关 - 模式徽标 - 间距。
        未完成布局时（宽度为 1）直接返回，等 <Configure> 再来一次。
        """
        total = self.winfo_width()
        if total <= 1:
            return
        try:
            right_w = self._right.winfo_reqwidth()
        except tk.TclError:
            return
        f_name, f_path, f_mode = self._measure_fonts()
        # 卡片内边距(body 左右各 14) + 右侧区间距(8) + 开关(44) + 开关后间距(10)
        avail = total - 28 - 8 - 44 - 10
        if avail <= 24:
            return
        # 名称需为模式徽标留出实测宽度
        mode_w = f_mode.measure(self._mode_lbl.cget("text")) + 8
        name_px = avail - mode_w
        if name_px > 10:
            self._name_lbl.config(text=_ellipsize_end(
                self._name_full, f_name.measure, name_px))
        if avail > 10:
            self._path_lbl.config(text=_ellipsize_mid(
                self._path_full, f_path.measure, avail))

    # ---- 渲染：刷新内容 ----
    def _card_bg(self):
        # type: () -> str
        return C_DARK_CARD_BG if getattr(self._app, "_dark_mode", False) else C_CARD_BG

    def _text_color(self):
        # type: () -> str
        return C_DARK_TEXT if getattr(self._app, "_dark_mode", False) else C_TEXT

    def _text_muted_color(self):
        # type: () -> str
        return C_DARK_TEXT_MUTED if getattr(self._app, "_dark_mode", False) else C_TEXT_MUTED

    def _text_disabled_color(self):
        # type: () -> str
        return C_DARK_TEXT_DISABLED if getattr(self._app, "_dark_mode", False) else C_TEXT_DISABLED

    def refresh(self, task):
        # type: (Task) -> None
        """按 Task 快照刷新卡片显示。"""
        card_bg = self._card_bg()
        text_c = self._text_color()
        muted_c = self._text_muted_color()
        dis_c = self._text_disabled_color()
        running = self._app.scheduler.is_task_running(task.id)
        # 名称与路径存全量文本，显示量交给 _refit_text 按实测宽度决定
        # （原按固定 36 字符硬截，与字号/DPI 脱节且窄窗口下仍会溢出）
        self._name_full = task.name
        self._path_full = "%s → %s" % (task.source, task.target)
        self._name_lbl.config(fg=text_c, bg=card_bg)
        self._mode_lbl.config(text=_MODE_LABEL.get(task.mode, task.mode), bg=card_bg)
        self._path_lbl.config(bg=card_bg)
        self._refit_text()

        if task.schedule.enabled and task.enabled:
            stype = task.schedule.type
            if stype == "interval":
                sched_txt = "每 %d 分钟" % task.schedule.interval_minutes
            elif stype == "daily" and task.schedule.times:
                sched_txt = "每日 %s" % ",".join(task.schedule.times)
            elif stype == "weekly" and task.schedule.weekdays:
                days = "一二三四五六日"
                sched_txt = "每周 %s" % " ".join(days[n - 1] for n in task.schedule.weekdays)
            elif stype == "monthly" and task.schedule.monthdays:
                sched_txt = "每月 %s 号" % ",".join(str(d) for d in task.schedule.monthdays)
            else:
                sched_txt = "未安排定时"
            last_txt = "上次: %s" % (format_epoch(task.last_run) or "-")
            self._sched_lbl.config(text="%s · %s" % (sched_txt, last_txt), bg=card_bg)
        else:
            self._sched_lbl.config(
                text="未启用" if not task.enabled else "未安排定时", bg=card_bg)

        if task.schedule.enabled and task.enabled and task.next_run:
            self._next_lbl.config(text="下次: %s" % format_epoch(task.next_run),
                                  fg=muted_c, bg=card_bg)
        else:
            self._next_lbl.config(text="", fg=dis_c, bg=card_bg)

        if running:
            self._status_lbl.config(text="● 运行中", fg=C_BRAND, bg=card_bg)
        elif not task.enabled:
            self._status_lbl.config(text="● 已禁用", fg=dis_c, bg=card_bg)
        elif not task.last_run:
            self._status_lbl.config(text="● 未运行", fg=dis_c, bg=card_bg)
        elif task.last_status in ("成功", None):
            self._status_lbl.config(text="● 成功", fg=C_OK, bg=card_bg)
        elif task.last_status in ("部分失败", "已取消"):
            self._status_lbl.config(text="● %s" % task.last_status, fg=C_WARN, bg=card_bg)
        else:
            self._status_lbl.config(text="● %s" % (task.last_status or "失败"),
                                    fg=C_DELETE, bg=card_bg)

        self._run_btn.config(state=tk.DISABLED if running else tk.NORMAL)

        self._draw_switch(task.enabled)

        if not task.enabled:
            self._name_lbl.config(fg=dis_c)
            self._path_lbl.config(fg=dis_c)
            self._sched_lbl.config(fg=dis_c)
        else:
            self._name_lbl.config(fg=text_c)
            self._path_lbl.config(fg=muted_c)
            self._sched_lbl.config(fg=muted_c)

    def _draw_switch(self, on):
        # type: (bool) -> None
        """自绘圆形开关：on=青绿色填充，off=灰色。"""
        c = self._sw_canvas
        c.delete("all")
        cw = int(c["width"])
        ch = int(c["height"])
        # 轨道（上下各留 2px）
        track_x1 = 2
        track_x2 = cw - 2
        track_y1 = 4
        track_y2 = ch - 4
        c.create_oval(track_x1, track_y1, track_x2, track_y2,
                      fill=(C_SWITCH_ON if on else C_SWITCH_OFF),
                      outline="")
        # 滑块：半径由轨道高反推，保证不溢出轨道上下缘
        r = _switch_radius(ch)
        if on:
            cx = track_x2 - r - 1
        else:
            cx = track_x1 + r + 1
        cy = ch // 2
        c.create_oval(cx - r, cy - r, cx + r, cy + r,
                      fill=C_WHITE, outline=C_SWITCH_KNOB)

    def set_selected(self, sel):
        # type: (bool) -> None
        """选中态：淡青绿底 + 左侧主色竖条。

        去边框后卡片无描边，选中由两个通道指示：专属底色 + 左侧 4px 主色
        竖条（place(relheight=1.0) 贴合全高）。曾有第三条"1px 主色边框"
        通道，但统一细边框时把它也设成了灰色 border，通道实际已不存在，
        故此处不再声称。
        """
        self._selected = sel
        self._cancel_hover()
        dark = getattr(self._app, "_dark_mode", False)
        brand_light = C_DARK_BRAND_LIGHT if dark else C_BRAND_LIGHT
        card_bg = C_DARK_CARD_BG if dark else C_CARD_BG
        if sel:
            self._set_card_style("CardSelected.TFrame")
            for w in (self._name_lbl, self._mode_lbl, self._path_lbl,
                      self._sched_lbl, self._next_lbl, self._status_lbl,
                      self._sw_canvas):
                try:
                    w.configure(bg=brand_light)
                except tk.TclError:
                    pass
            try:
                # relheight=1.0 让竖条精确贴合卡片全高（含边框），随窗口缩放与
                # 内容变化自动跟随；写死像素会在卡片高度与之不符时溢出并压到
                # 相邻卡片上（曾用 height=200 而卡片仅 ~70px）
                self._sel_bar.place(x=0, y=0, relheight=1.0, width=4)
            except tk.TclError:
                pass
        else:
            self._set_card_style("Card.TFrame")
            for w in (self._name_lbl, self._mode_lbl, self._path_lbl,
                      self._sched_lbl, self._next_lbl, self._status_lbl,
                      self._sw_canvas):
                try:
                    w.configure(bg=card_bg)
                except tk.TclError:
                    pass
            try:
                self._sel_bar.place_forget()
            except tk.TclError:
                pass
