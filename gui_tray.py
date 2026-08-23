"""菜单栏 / 系统托盘 / 最小化后台运行混入。

职责边界
--------
从 gui_app.App 拆出的"窗口可见性与生命周期入口"：菜单栏（最小化到
后台 / 开机自启 / 退出）、托盘图标创建与回调、X 关闭转后台、托盘恢复、
真正退出请求。关闭时序本体（on_close/_finish_close）在 gui_close.py。

宿主状态契约
------------
root / logger / _tray / _tray_hidden / _quitting / _closing（读），
以及核心层方法 _refresh_tasks 与 CloseSeqMixin.on_close。
"""

import os

import tkinter as tk
from tkinter import messagebox

import autostart
import tray as tray_mod
from utils.paths import app_dir
from typing import Any, Callable, Optional, TYPE_CHECKING
from logger import AppLogger

# 图标路径基准目录：与 gui_app.APP_DIR 同源（utils.paths.app_dir 的进程内
# 稳定值）。不反向 import gui_app——混入模块必须保持无环依赖。
_APP_DIR = app_dir()


class TrayMenuMixin(object):
    """菜单栏与托盘/后台运行（见模块 docstring 的宿主状态契约）。"""

    # ---------- 宿主状态契约（仅类型声明，运行期被实例属性遮蔽） ----------
    root = None                 # type: tk.Tk
    logger = None               # type: AppLogger
    _tray = None                # type: Optional[Any]  # noqa: E501  (tray_mod.TrayIcon)
    _tray_hidden = False        # type: bool
    _quitting = False           # type: bool
    _closing = False            # type: bool
    if TYPE_CHECKING:  # 方法契约仅类型层：类级赋值会按 MRO 遮蔽其他混入的真实现
        _refresh_tasks = None   # type: Callable[..., None]
        on_close = None         # type: Callable[..., None]

    # ---------- 菜单栏 / 托盘 / 最小化后台运行 ----------
    def _build_menu(self):
        # type: () -> None
        """菜单栏「文件」：最小化到后台 / 开机自启 / 退出（全平台可用）。"""
        menubar = tk.Menu(self.root)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="最小化到后台", command=self._hide_to_background)
        self._autostart_var = tk.BooleanVar(value=autostart.is_enabled())
        file_menu.add_checkbutton(label="开机自启", variable=self._autostart_var,
                                  command=self._toggle_autostart)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self._request_quit)
        menubar.add_cascade(label="文件", menu=file_menu)
        self.root.config(menu=menubar)

    def _toggle_autostart(self):
        # type: () -> None
        # 菜单栏勾选项：勾选即注册、取消即反注册；失败回滚勾选并提示
        want = self._autostart_var.get()
        ok = autostart.enable() if want else autostart.disable()
        if not ok:
            self._autostart_var.set(not want)
            messagebox.showerror("错误", "设置开机自启失败，请检查权限或手动配置")
            return
        self.logger.info("开机自启已%s" % ("启用" if want else "关闭"))

    def _init_tray(self):
        # type: () -> None
        """创建托盘图标（失败不致命：降级为任务栏最小化）。"""
        try:
            self._tray = tray_mod.TrayIcon(
                "文件夹同步备份工具",
                menu=[(1, "显示主窗口"), (2, "退出")],
                on_menu=self._on_tray_menu,
                on_activate=self._restore_from_tray,
                icon_path=os.path.join(_APP_DIR, "app.ico"),
                main_hwnd=self.root.winfo_id())  # 主窗口句柄，托盘菜单关闭后还焦点
        except Exception as e:
            self._tray = None
            self.logger.warn("托盘图标创建失败（降级为任务栏最小化）: %s" % e)

    def _on_tray_menu(self, item_id):
        # type: (int) -> None
        # 托盘回调在 Windows 消息泵（主线程）中执行，可直接调 tkinter
        if item_id == 1:
            self._restore_from_tray()
        elif item_id == 2:
            self._request_quit()

    def _hide_to_background(self):
        # type: () -> None
        """最小化后台运行：Windows 隐藏到托盘；非 Windows 最小化到任务栏。

        只隐藏窗口，**不**停止调度器、不退出进程——后台继续按计划同步。
        """
        if self._closing:
            return
        if self._tray is not None:
            self._tray_hidden = True
            try:
                self.root.withdraw()
            except tk.TclError:
                self._tray_hidden = False
        else:
            # 非 Windows 无托盘：弹确认框告知后台运行状态与退出路径
            self._ask_minimize_or_quit()

    def _ask_minimize_or_quit(self):
        # type: () -> None
        """非 Windows 平台：点 X 后弹三选一对话框，明确退出路径。

        - 最小化到后台（默认）：iconify，调度器照常
        - 退出程序：置位后走完整关闭流程
        - 取消：保持窗口前台
        """
        if self._closing:
            return
        try:
            ans = messagebox.askyesnocancel(
                "最小化到后台",
                "点关闭按钮不会退出程序，而是最小化到后台继续运行。\n\n"
                "• 任务栏图标可恢复窗口\n"
                "• 后台期间调度器照常工作\n"
                "• 恢复窗口后从菜单栏「文件→退出」真正退出\n\n"
                "最小化到后台？（选「否」直接退出程序，选「取消」保持窗口）",
                icon="question",
                default="yes")
        except tk.TclError:
            return
        if ans is True:                 # Yes → 最小化到后台
            try:
                self.root.iconify()
            except tk.TclError:
                pass
        elif ans is False:              # No → 退出程序
            self._request_quit()
        # None（Cancel）→ 保持窗口前台，什么都不做

    def _restore_from_tray(self):
        # type: () -> None
        """从托盘恢复主窗口（左键单击 / 菜单"显示主窗口"）。"""
        self._tray_hidden = False
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except tk.TclError:
            pass
        self._refresh_tasks(full=True)

    def _request_quit(self):
        # type: () -> None
        """真正退出（托盘菜单/菜单栏"退出"）：置位后走完整关闭流程。"""
        self._quitting = True
        self.on_close()

    def _on_unmap(self, event):
        # type: (object) -> None
        # Windows 最小化（-）时触发 <Unmap>：隐藏到托盘。
        # 需判 state()=="iconic"：withdraw 也会触发 <Unmap>，但状态为
        # withdrawn，且 _tray_hidden 已置位防重入。
        if self._closing or self._tray is None or self._tray_hidden:
            return
        try:
            if self.root.state() == "iconic":
                self._hide_to_background()
        except tk.TclError:
            pass
