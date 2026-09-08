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
from typing import Optional

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
        # type: (tk.Widget, object, str) -> None
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
        detail_row = tk.Frame(info, style="Card.TFrame")
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
            self._btn_frame, text="运行", width=6,
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
            elif stype == "monthly" and task.schedule.monthdays:
                sched_txt = "每月 %s 号" % ",".join(str(d) for d in task.schedule.monthdays)
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
