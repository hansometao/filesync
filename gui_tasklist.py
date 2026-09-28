"""任务卡片组件与共享配色常量。

从 gui_app.py 拆出的 UI 渲染层：
- 配色变量（青绿色主题）
- _short_path 路径截断工具
- TaskCard 单张任务卡片（Canvas 嵌入 Frame，可滚动）
- _MODE_LABEL 模式显示标签

gui_app.App 与 TaskCard 共享同一套配色常量，此处为唯一来源。
"""

import sys

import tkinter as tk
from tkinter import ttk
from typing import Any, Optional

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
C_BG = "#FAFAFA"              # 页面背景
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
        # type: (tk.Widget, Any, str) -> None
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

        # 路径 + 调度信息合并为一行（用 · 分隔）
        detail_row = ttk.Frame(info, style="Card.TFrame")
        detail_row.pack(anchor=tk.W, fill=tk.X, pady=(2, 0))

        self._path_lbl = tk.Label(detail_row, font=("", 9), fg=C_TEXT_MUTED,
                                  bg=C_CARD_BG, anchor="w")
        self._path_lbl.pack(side=tk.LEFT)

        self._sched_sep = tk.Label(detail_row, text=" · ", font=("", 9),
                                   fg=C_TEXT_DISABLED, bg=C_CARD_BG)
        self._sched_sep.pack(side=tk.LEFT)

        self._sched_lbl = tk.Label(detail_row, font=("", 9), fg=C_TEXT_MUTED,
                                   bg=C_CARD_BG, anchor="w")
        self._sched_lbl.pack(side=tk.LEFT)

        # ---- 右侧：上层=状态+下次运行，下层=操作按钮 ----
        right = ttk.Frame(self, style="Card.TFrame")
        right.pack(side=tk.RIGHT, padx=(8, 0))

        # 上层：状态标记 + 下次运行（水平紧凑排列）
        info_row = ttk.Frame(right, style="Card.TFrame")
        info_row.pack(anchor=tk.E)

        self._status_lbl = tk.Label(info_row, font=("", 10, "bold"),
                                    bg=C_CARD_BG)
        self._status_lbl.pack(side=tk.LEFT, padx=(0, 6))

        self._next_lbl = tk.Label(info_row, font=("", 9), fg=C_TEXT_MUTED,
                                  bg=C_CARD_BG)
        self._next_lbl.pack(side=tk.LEFT)

        # 下层：操作按钮组（运行 / 编辑 / 删除）
        self._btn_frame = ttk.Frame(right, style="Card.TFrame")
        self._btn_frame.pack(anchor=tk.E, pady=(4, 0))

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

        # 右键菜单 / 选中 / 双击运行 绑定（整卡都响应）
        # C2 修复：_sw_canvas 必须从本循环剔除——tkinter bind 是替换语义，
        # 若在此对 _sw_canvas 再绑 <Button-1>，会覆盖 __init__ 中开关的切换
        # 绑定，导致开关失效；且会绑 <Double-Button-1> 让快速点两下开关误触
        # 同步。开关仅保留第 98 行的切换绑定与单独右键菜单。
        for w in (self, info, name_row, self._name_lbl,
                  self._mode_lbl, self._path_lbl, self._sched_lbl, right,
                  self._next_lbl, self._btn_frame, self._status_lbl):
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
        self._hover_widgets = (self, info, name_row, self._name_lbl,
                               self._mode_lbl, self._path_lbl, self._sched_lbl,
                               self._sched_sep, right, self._next_lbl,
                               self._btn_frame, self._status_lbl)
        for w in self._hover_widgets:
            try:
                w.bind("<Enter>", self._on_hover_enter)
                w.bind("<Leave>", self._on_hover_leave)
            except tk.TclError:
                pass
        # bg 重绘仅针对 tk.Label（ttk 部件底色由 CardHover.TFrame 样式统一控制，
        # 对 ttk.Frame 逐个 configure(bg=) 是无效选项）
        self._hover_labels = (self._name_lbl, self._mode_lbl, self._path_lbl,
                              self._sched_lbl, self._sched_sep, self._next_lbl,
                              self._status_lbl)

        # ---- 选中指示条：左侧 4px 主色竖条（place 悬浮，不参与 pack 布局） ----
        self._sel_bar = tk.Label(self, bg=C_BRAND, width=3, height=200)

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
            self.configure(style="Card.TFrame")
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
            self.configure(style="CardHover.TFrame")
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
        # 选中态由 App._select_card 控制样式，这里仅保证卡片高度一致
        pass

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
        self._name_lbl.config(text=task.name, fg=text_c, bg=card_bg)
        self._mode_lbl.config(text=_MODE_LABEL.get(task.mode, task.mode), bg=card_bg)
        self._path_lbl.config(
            text="%s → %s" % (_short_path(task.source), _short_path(task.target)),
            bg=card_bg)

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
                      fill=C_WHITE, outline=C_SWITCH_KNOB)

    def set_selected(self, sel):
        # type: (bool) -> None
        """选中态：淡青绿底 + 1px 主色边框 + 左侧主色竖条（三通道指示）。"""
        self._selected = sel
        self._cancel_hover()
        dark = getattr(self._app, "_dark_mode", False)
        brand_light = C_DARK_BRAND_LIGHT if dark else C_BRAND_LIGHT
        card_bg = C_DARK_CARD_BG if dark else C_CARD_BG
        if sel:
            self.configure(style="CardSelected.TFrame")
            for w in (self._name_lbl, self._mode_lbl, self._path_lbl,
                      self._sched_lbl, self._next_lbl, self._status_lbl,
                      self._sw_canvas):
                try:
                    w.configure(bg=brand_light)
                except tk.TclError:
                    pass
            try:
                self._sel_bar.place(x=0, y=0, height=200, width=4)
            except tk.TclError:
                pass
        else:
            self.configure(style="Card.TFrame")
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
