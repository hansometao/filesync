"""差异预览 / 冲突处理对话框。

以 ASCII 标记区分动作类型：
  [+] 新增   [~] 修改   [-] 删除   [!] 冲突   [=] 仅目标多余(保留)
对包含冲突且任务策略为"询问"的任务，提供本次运行的冲突解决策略选择。
"""

import tkinter as tk
from tkinter import ttk
from typing import Optional

from config import (
    CONFLICT_NEWER, CONFLICT_ASK, CONFLICT_POLICIES, Task, POLICY_LABELS,
)
from sync_engine import DiffResult

_KIND_TAG = {
    "copy": "[+]",
    "delete": "[-]",
    "mkdir": "[D+]",
    "rmdir": "[D-]",
    "type_conflict": "[!]",
    "conflict": "[!]",
    "conflict_del": "[!]",
    "extra": "[=]",
}
_KIND_LABEL = {
    "copy": "复制/覆盖",
    "delete": "删除",
    "mkdir": "创建目录",
    "rmdir": "删除目录",
    "type_conflict": "类型冲突",
    "conflict": "冲突",
    "conflict_del": "删除/修改冲突",
    "extra": "仅目标多余",
}


def deletion_warning_text(delete_count, rmdir_count=0):
    # type: (int, int) -> Optional[str]
    """删除类动作的强化警示文案；无删除时返回 None。

    引擎侧已有空根/扫描不完整等误删防线，这里是 UI 层的二次防线：
    预览确认前把"将删除 N 项"从摘要行里提出来醒目展示。纯函数，
    无 tkinter 依赖，可无头测试。
    """
    n = delete_count + rmdir_count
    if n <= 0:
        return None
    return "注意：本次将删除 %d 个文件/目录，请核对下方清单后再确认执行" % n


class DiffDialog(tk.Toplevel):
    def __init__(self, parent, diff_result, task):
        # type: (tk.Misc, DiffResult, Task) -> None
        super(DiffDialog, self).__init__(parent)
        self.title("同步差异预览 - %s" % task.name)
        self.diff = diff_result
        self.task = task
        self.result_policy = None  # type: Optional[str]  # None 表示取消
        self._build()
        self.geometry("720x520")
        self.transient(parent)  # type: ignore[call-overload]  # parent 实际为 Tk/Toplevel（均继承 Wm），标注为 Misc 仅为通用
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

    def _build(self):
        # type: () -> None
        frm = ttk.Frame(self, padding=8)
        frm.pack(fill=tk.BOTH, expand=True)

        info = "待执行动作（共 %d 项）：%s" % (len(self.diff.actions), self.diff.summary())
        has_conflict = bool(self.diff.conflict_count or self.diff.type_conflict_count)
        ttk.Label(frm, text=info, foreground="#b00" if has_conflict else "#333").pack(anchor=tk.W, pady=(0, 6))
        # 删除类动作强化警示（与冲突红色提示并列的二次防线）
        del_warn = deletion_warning_text(self.diff.delete_count, self.diff.rmdir_count)
        if del_warn:
            ttk.Label(frm, text=del_warn, foreground="#b00").pack(anchor=tk.W)

        cols = ("tag", "kind", "rel", "detail")
        # 差异列表可能上千条：Treeview + 垂直滚动条，保证可滚动查看全部
        tree_frame = ttk.Frame(frm)
        tree_frame.pack(fill=tk.BOTH, expand=True, side=tk.TOP)
        tree = ttk.Treeview(tree_frame, columns=cols, show="headings", height=18)
        sb = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        tree.heading("tag", text="")
        tree.heading("kind", text="类型")
        tree.heading("rel", text="相对路径")
        tree.heading("detail", text="说明")
        tree.column("tag", width=6, anchor=tk.CENTER)
        tree.column("kind", width=80)
        tree.column("rel", width=320)
        tree.column("detail", width=240)

        MAX_SHOW = 2000
        shown = 0
        for act in self.diff.actions:
            if shown >= MAX_SHOW:
                break
            tag = _KIND_TAG.get(act.kind, "")
            kind = _KIND_LABEL.get(act.kind, act.kind)
            tree.insert("", tk.END, values=(tag, kind, act.rel, act.detail))
            shown += 1
        if len(self.diff.actions) > MAX_SHOW:
            ttk.Label(frm, text="（共 %d 条动作，仅显示前 %d 条；完整清单见日志）"
                      % (len(self.diff.actions), MAX_SHOW), foreground="#666").pack(anchor=tk.W)

        if not self.diff.actions:
            ttk.Label(frm, text="（无差异，无需同步）").pack(pady=10)

        # 冲突策略选择
        # 有冲突且任务策略为 ask 时，默认预选"新版本胜出"，避免无选中项被静默回退
        default_policy = (CONFLICT_NEWER
                          if (self.diff.conflict_count > 0
                              and self.task.conflict_policy == CONFLICT_ASK)
                          else self.task.conflict_policy)
        self._policy_var = tk.StringVar(value=default_policy)
        if self.diff.conflict_count > 0 and self.task.conflict_policy == CONFLICT_ASK:
            pf = ttk.LabelFrame(frm, text="冲突解决策略（本次运行）", padding=6)
            pf.pack(fill=tk.X, pady=8)
            ttk.Label(pf, text="检测到 %d 个冲突，请选择本次处理方式：" % self.diff.conflict_count).pack(anchor=tk.W)
            for p in CONFLICT_POLICIES:
                if p == CONFLICT_ASK:
                    continue
                ttk.Radiobutton(pf, text=POLICY_LABELS.get(p, p), variable=self._policy_var, value=p).pack(anchor=tk.W)
        elif self.diff.conflict_count > 0:
            ttk.Label(frm, text="冲突将按任务策略处理：%s（落败方会先备份为 .conflict-时间戳 副本）" %
                      POLICY_LABELS.get(self.task.conflict_policy, self.task.conflict_policy),
                      foreground="#b00").pack(anchor=tk.W, pady=6)

        btn = ttk.Frame(frm)
        btn.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(btn, text="确认执行", command=self._on_confirm).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btn, text="取消", command=self._on_cancel).pack(side=tk.RIGHT, padx=4)

    def _on_confirm(self):
        # type: () -> None
        if not self.diff.actions:
            self.result_policy = "_noop_"
            self.destroy()
            return
        self.result_policy = self._policy_var.get()
        self.destroy()

    def _on_cancel(self):
        # type: () -> None
        self.result_policy = None
        self.destroy()
