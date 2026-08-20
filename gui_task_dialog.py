"""新增 / 编辑任务对话框。"""

import os
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from config import (
    Task, MODE_ONE_WAY, MODE_TWO_WAY,
    SCHED_INTERVAL, SCHED_DAILY, SCHED_WEEKLY,
    validate_schedule_input,
    CONFLICT_POLICIES, CONFLICT_NEWER, CONFLICT_SOURCE,
    CONFLICT_TARGET, CONFLICT_SKIP, CONFLICT_ASK,
)

# 下拉框显示中文标签，保存/加载时与内部值双向映射
# （标签文案与 gui_diff 的 _POLICY_LABELS 保持一致）
_MODE_LABELS = {MODE_ONE_WAY: "单向镜像", MODE_TWO_WAY: "双向同步"}
_SCHED_LABELS = {SCHED_INTERVAL: "间隔定时", SCHED_DAILY: "每日时刻", SCHED_WEEKLY: "每周时刻"}
_POLICY_LABELS = {
    CONFLICT_NEWER: "新版本胜出",
    CONFLICT_SOURCE: "源侧胜出",
    CONFLICT_TARGET: "目标侧胜出",
    CONFLICT_SKIP: "跳过(不处理)",
    CONFLICT_ASK: "逐个询问",
}
_MODE_REV = {v: k for k, v in _MODE_LABELS.items()}
_SCHED_REV = {v: k for k, v in _SCHED_LABELS.items()}
_POLICY_REV = {v: k for k, v in _POLICY_LABELS.items()}


class TaskDialog(tk.Toplevel):
    def __init__(self, parent, task, store):
        # type: (tk.Widget, object, object) -> None
        super(TaskDialog, self).__init__(parent)
        self.store = store
        self.task = task
        self.is_new = task is None
        self.result = None  # 保存后的 Task 或 None
        self.title("新建同步任务" if self.is_new else "编辑同步任务")
        self._build()
        self.geometry("560x600")
        self.minsize(520, 560)  # M-11：高 DPI（125%/150%）下防止按钮被压缩出界，且允许手动放大
        self.transient(parent)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        if not self.is_new:
            self._load(task)

    def _row(self, parent, row, label, widget, btn=None):
        # type: (tk.Widget, int, str, tk.Widget, object) -> None
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky=tk.W, padx=4, pady=3)
        widget.grid(row=row, column=1, sticky=tk.EW, padx=4, pady=3)
        if btn is not None:
            btn.grid(row=row, column=2, padx=4, pady=3)

    def _build(self):
        # type: () -> None
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill=tk.BOTH, expand=True)
        frm.columnconfigure(1, weight=1)

        self._name = ttk.Entry(frm)
        self._src = ttk.Entry(frm)
        self._dst = ttk.Entry(frm)
        self._mode = ttk.Combobox(frm, values=list(_MODE_LABELS.values()), state="readonly")
        self._mode.set(_MODE_LABELS[MODE_ONE_WAY])
        self._ow_del = tk.BooleanVar()
        self._tw_del = tk.BooleanVar()
        self._enabled = tk.BooleanVar(value=True)  # 新任务默认启用，避免被静默存为"禁用"
        self._ow_del_chk = ttk.Checkbutton(frm, text="镜像时删除目标多余文件", variable=self._ow_del)
        self._tw_del_chk = ttk.Checkbutton(frm, text="双向时传播删除", variable=self._tw_del)
        self._sched_on = tk.BooleanVar()
        self._sched_type = ttk.Combobox(frm, values=list(_SCHED_LABELS.values()), state="readonly")
        self._sched_type.set(_SCHED_LABELS[SCHED_INTERVAL])
        self._interval = ttk.Spinbox(frm, from_=1, to=99999, increment=1, width=10)
        self._interval.set("60")
        self._times = ttk.Entry(frm)
        self._times.insert(0, "08:00,20:00")
        self._weekdays = ttk.Entry(frm)
        self._weekdays.insert(0, "1,3,5")  # 周一、三、五（1=周一 … 7=周日）
        self._include = ttk.Entry(frm)
        self._exclude = ttk.Entry(frm)
        self._exclude.insert(0, "*.tmp,__pycache__/,node_modules/,.git/")
        self._conflict = ttk.Combobox(frm, values=list(_POLICY_LABELS.values()), state="readonly")
        self._conflict.set(_POLICY_LABELS[CONFLICT_NEWER])

        r = 0
        self._row(frm, r, "任务名称", self._name); r += 1
        self._row(frm, r, "源目录", self._src, ttk.Button(frm, text="浏览", command=lambda: self._pick(self._src))); r += 1
        self._row(frm, r, "目标目录", self._dst, ttk.Button(frm, text="浏览", command=lambda: self._pick(self._dst))); r += 1
        self._row(frm, r, "启用任务", ttk.Checkbutton(frm, text="启用（取消则暂停该任务）", variable=self._enabled)); r += 1
        self._row(frm, r, "同步方向", self._mode); r += 1
        self._row(frm, r, "单向选项", self._ow_del_chk); r += 1
        self._row(frm, r, "双向选项", self._tw_del_chk); r += 1

        # 调度
        self._row(frm, r, "启用定时", ttk.Checkbutton(frm, text="启用", variable=self._sched_on)); r += 1
        self._row(frm, r, "定时类型", self._sched_type); r += 1
        self._row(frm, r, "间隔(分钟)", self._interval); r += 1
        self._row(frm, r, "每日时刻", self._times); r += 1
        self._row(frm, r, "每周(1-7)", self._weekdays); r += 1

        self._row(frm, r, "包含规则", self._include); r += 1
        self._row(frm, r, "排除规则", self._exclude); r += 1
        self._row(frm, r, "冲突策略", self._conflict); r += 1

        ttk.Label(frm, text="提示：包含/排除用逗号分隔，如 *.tmp,__pycache__/；每日时刻如 08:00,20:00").grid(
            row=r, column=0, columnspan=3, sticky=tk.W, padx=4, pady=8); r += 1

        btn = ttk.Frame(frm)
        btn.grid(row=r, column=0, columnspan=3, pady=10)
        ttk.Button(btn, text="保存", command=self._on_save).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btn, text="取消", command=self._on_cancel).pack(side=tk.RIGHT, padx=4)

        # 双向时禁用单向删除勾选，反之亦然（UI 友好，不强制）
        def _on_mode(*_):
            # type: (*object) -> None
            if self._mode.get() == _MODE_LABELS[MODE_TWO_WAY]:
                self._ow_del_chk.configure(state=tk.DISABLED)
            else:
                self._ow_del_chk.configure(state=tk.NORMAL)
        self._mode.bind("<<ComboboxSelected>>", _on_mode)

    def _pick(self, entry):
        # type: (tk.Entry) -> None
        d = filedialog.askdirectory()
        if d:
            entry.delete(0, tk.END)
            entry.insert(0, d)

    def _load(self, task):
        # type: (Task) -> None
        self._name.insert(0, task.name)
        self._src.insert(0, task.source)
        self._dst.insert(0, task.target)
        self._mode.set(_MODE_LABELS.get(task.mode, task.mode))
        self._ow_del.set(task.one_way_delete)
        self._tw_del.set(task.two_way_delete)
        self._enabled.set(task.enabled)
        self._sched_on.set(task.schedule.enabled)
        self._sched_type.set(_SCHED_LABELS.get(task.schedule.type, task.schedule.type))
        self._interval.delete(0, tk.END)
        self._interval.insert(0, str(task.schedule.interval_minutes))
        self._times.delete(0, tk.END)
        self._times.insert(0, ",".join(task.schedule.times))
        self._weekdays.delete(0, tk.END)
        self._weekdays.insert(0, ",".join(str(w) for w in task.schedule.weekdays))
        self._include.delete(0, tk.END)
        self._include.insert(0, ",".join(task.include))
        self._exclude.delete(0, tk.END)
        self._exclude.insert(0, ",".join(task.exclude))
        self._conflict.set(_POLICY_LABELS.get(task.conflict_policy, task.conflict_policy))
        if task.mode == MODE_TWO_WAY:
            self._ow_del_chk.configure(state=tk.DISABLED)

    def _on_save(self):
        # type: () -> None
        name = self._name.get().strip()
        src = self._src.get().strip()
        dst = self._dst.get().strip()
        if not name:
            messagebox.showerror("错误", "请填写任务名称")
            return
        if not src or not dst:
            messagebox.showerror("错误", "请选择源目录与目标目录")
            return
        if not os.path.isdir(src):
            messagebox.showerror("错误", "源目录不存在：%s" % src)
            return
        if not os.path.isdir(dst):
            messagebox.showerror("错误", "目标目录不存在：%s" % dst)
            return
        # L8/M-8：源/目标不能相同或互为子目录（否则自我递归复制）。
        # 用 normcase 三值比对：Windows 下大小写不同的同一/父子路径也不漏判。
        asrc = os.path.normcase(os.path.abspath(src))
        adst = os.path.normcase(os.path.abspath(dst))
        if asrc == adst:
            messagebox.showerror("错误", "源目录与目标目录不能相同")
            return
        try:
            common = os.path.normcase(os.path.commonpath([asrc, adst]))
            if common in (asrc, adst):
                messagebox.showerror("错误", "源目录与目标目录不能互为子目录（会导致自我递归复制）")
                return
        except ValueError:
            pass  # Windows 不同盘符等，不可能互为子路径

        # 调度输入校验：interval/times/weekdays 抽为纯函数 validate_schedule_input
        # （无 tkinter 依赖，可无头测试），此处仅负责错误弹窗展示
        err = validate_schedule_input(
            bool(self._sched_on.get()), self._sched_type.get(),
            self._interval.get(), self._times.get(), self._weekdays.get())
        if err:
            messagebox.showerror("错误", err)
            return

        # 校验已保证格式合法，按既有逻辑解析（times/weekdays 为空时得到空列表）
        interval = int(self._interval.get())
        times = [t.strip() for t in self._times.get().split(",") if t.strip()]
        include = [t.strip() for t in self._include.get().split(",") if t.strip()]
        exclude = [t.strip() for t in self._exclude.get().split(",") if t.strip()]
        weekdays = [int(w) for w in self._weekdays.get().split(",") if w.strip()]

        if self.is_new:
            t = Task()
        else:
            t = self.task
        t.name = name
        t.source = os.path.abspath(src)
        t.target = os.path.abspath(dst)
        # Combobox 显示中文标签，保存时用反向映射还原内部值
        # （此前直接存 self._mode.get() 会把 "单向镜像" 等标签写进 Task，
        #   同步引擎按 MODE_ONE_WAY 等内部值判断会失效）
        t.mode = _MODE_REV.get(self._mode.get(), self._mode.get())
        t.one_way_delete = bool(self._ow_del.get()) and t.mode == MODE_ONE_WAY
        t.two_way_delete = bool(self._tw_del.get()) and t.mode == MODE_TWO_WAY
        t.enabled = bool(self._enabled.get())
        t.schedule.enabled = bool(self._sched_on.get())
        t.schedule.type = _SCHED_REV.get(self._sched_type.get(), self._sched_type.get())
        t.schedule.interval_minutes = interval
        t.schedule.times = times
        t.schedule.weekdays = weekdays
        t.include = include
        t.exclude = exclude
        t.conflict_policy = _POLICY_REV.get(self._conflict.get(), self._conflict.get())
        self.result = t
        self.destroy()

    def _on_cancel(self):
        # type: () -> None
        self.result = None
        self.destroy()
