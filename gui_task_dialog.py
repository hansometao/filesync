"""新增 / 编辑任务对话框。"""

import os
import re
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from config import (
    Task, MODE_ONE_WAY, MODE_TWO_WAY,
    SCHED_INTERVAL, SCHED_DAILY, CONFLICT_POLICIES, CONFLICT_NEWER,
)


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
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky=tk.W, padx=4, pady=3)
        widget.grid(row=row, column=1, sticky=tk.EW, padx=4, pady=3)
        if btn is not None:
            btn.grid(row=row, column=2, padx=4, pady=3)

    def _build(self):
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill=tk.BOTH, expand=True)
        frm.columnconfigure(1, weight=1)

        self._name = ttk.Entry(frm)
        self._src = ttk.Entry(frm)
        self._dst = ttk.Entry(frm)
        self._mode = ttk.Combobox(frm, values=[MODE_ONE_WAY, MODE_TWO_WAY], state="readonly")
        self._mode.set(MODE_ONE_WAY)
        self._ow_del = tk.BooleanVar()
        self._tw_del = tk.BooleanVar()
        self._enabled = tk.BooleanVar(value=True)  # 新任务默认启用，避免被静默存为"禁用"
        self._ow_del_chk = ttk.Checkbutton(frm, text="镜像时删除目标多余文件", variable=self._ow_del)
        self._tw_del_chk = ttk.Checkbutton(frm, text="双向时传播删除", variable=self._tw_del)
        self._sched_on = tk.BooleanVar()
        self._sched_type = ttk.Combobox(frm, values=[SCHED_INTERVAL, SCHED_DAILY], state="readonly")
        self._sched_type.set(SCHED_INTERVAL)
        self._interval = ttk.Spinbox(frm, from_=1, to=99999, increment=1, width=10)
        self._interval.set("60")
        self._times = ttk.Entry(frm)
        self._times.insert(0, "08:00,20:00")
        self._include = ttk.Entry(frm)
        self._exclude = ttk.Entry(frm)
        self._exclude.insert(0, "*.tmp,__pycache__/,node_modules/,.git/")
        self._conflict = ttk.Combobox(frm, values=CONFLICT_POLICIES, state="readonly")
        self._conflict.set(CONFLICT_NEWER)

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
            if self._mode.get() == MODE_TWO_WAY:
                self._ow_del_chk.configure(state=tk.DISABLED)
            else:
                self._ow_del_chk.configure(state=tk.NORMAL)
        self._mode.bind("<<ComboboxSelected>>", _on_mode)

    def _pick(self, entry):
        d = filedialog.askdirectory()
        if d:
            entry.delete(0, tk.END)
            entry.insert(0, d)

    def _load(self, task):
        self._name.insert(0, task.name)
        self._src.insert(0, task.source)
        self._dst.insert(0, task.target)
        self._mode.set(task.mode)
        self._ow_del.set(task.one_way_delete)
        self._tw_del.set(task.two_way_delete)
        self._enabled.set(task.enabled)
        self._sched_on.set(task.schedule.enabled)
        self._sched_type.set(task.schedule.type)
        self._interval.delete(0, tk.END)
        self._interval.insert(0, str(task.schedule.interval_minutes))
        self._times.delete(0, tk.END)
        self._times.insert(0, ",".join(task.schedule.times))
        self._include.delete(0, tk.END)
        self._include.insert(0, ",".join(task.include))
        self._exclude.delete(0, tk.END)
        self._exclude.insert(0, ",".join(task.exclude))
        self._conflict.set(task.conflict_policy)
        if task.mode == MODE_TWO_WAY:
            self._ow_del_chk.configure(state=tk.DISABLED)

    def _on_save(self):
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

        # M-10：interval 必须为正整数，非法则报错，不再静默 coerce
        try:
            interval = int(self._interval.get())
        except ValueError:
            messagebox.showerror(
                "错误", "间隔(分钟)必须是整数，当前值：%s" % self._interval.get())
            return
        if interval < 1:
            messagebox.showerror("错误", "间隔(分钟)必须是正整数（>=1）")
            return

        times = [t.strip() for t in self._times.get().split(",") if t.strip()]
        include = [t.strip() for t in self._include.get().split(",") if t.strip()]
        exclude = [t.strip() for t in self._exclude.get().split(",") if t.strip()]

        # M-9/H2：每日时刻必须合法 HH:MM。只要填了时刻就校验（不限于「启用定时 + daily」），
        # 防止手改 JSON / 旧构建写入非法时刻后在调度线程被静默跳过。
        if times:
            bad = [t for t in times if not re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", t)]
            if bad:
                messagebox.showerror(
                    "错误", "每日时刻格式应为 HH:MM（00:00-23:59），非法值：%s" % ",".join(bad))
                return
        if self._sched_on.get() and self._sched_type.get() == SCHED_DAILY and not times:
            messagebox.showerror("错误", "启用每日定时时请至少填写一个时刻，如 08:00,20:00")
            return

        if self.is_new:
            t = Task()
        else:
            t = self.task
        t.name = name
        t.source = os.path.abspath(src)
        t.target = os.path.abspath(dst)
        t.mode = self._mode.get()
        t.one_way_delete = bool(self._ow_del.get()) and t.mode == MODE_ONE_WAY
        t.two_way_delete = bool(self._tw_del.get()) and t.mode == MODE_TWO_WAY
        t.enabled = bool(self._enabled.get())
        t.schedule.enabled = bool(self._sched_on.get())
        t.schedule.type = self._sched_type.get()
        t.schedule.interval_minutes = interval
        t.schedule.times = times
        t.include = include
        t.exclude = exclude
        t.conflict_policy = self._conflict.get()
        self.result = t
        self.destroy()

    def _on_cancel(self):
        self.result = None
        self.destroy()
