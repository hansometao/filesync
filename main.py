"""文件夹同步备份工具 - 入口。

最低运行环境：CPython 3.8.x（兼容 Windows 7）。
仅依赖标准库 + tkinter，哈希可选 xxhash 加速。

用法：
  python main.py                 # 启动图形界面
  python main.py --list          # 列出全部任务（无头）
  python main.py --sync <名称或ID>  # 立即执行一次指定任务（无头，可配合
                                # Windows 任务计划程序 / cron 做无人值守备份）
  python main.py --help          # 显示帮助

退出码：0=成功/已列出  1=未找到或已禁用  2=部分失败(有文件未同步成功)
       3=同步过程异常(未捕获的业务错误)
"""

import os
import sys
from typing import Any, Callable, Optional

# Windows 7 DPI 适配（最佳努力，失败不影响功能）
try:
    if sys.platform == "win32":
        from ctypes import windll  # type: ignore
        try:
            windll.user32.SetProcessDPIAware()
        except Exception:
            pass
except Exception:
    pass


_HELP = """文件夹同步备份工具

用法:
  python main.py                 启动图形界面
  python main.py --list          列出全部任务（无头）
  python main.py --sync <名称或ID>   立即执行一次指定任务（无头）
  python main.py --help          显示本帮助

退出码: 0=成功/已列出  1=未找到或已禁用  2=部分失败  3=同步异常
"""


def _print_help():
    # type: () -> None
    sys.stdout.write(_HELP)


def _is_windowed_frozen():
    # type: () -> bool
    """windowed 打包（无控制台）时 sys.stdout 为 None，print 无处可去。"""
    return getattr(sys, "frozen", False) and sys.stdout is None


def _cli_output_popup(fn):
    # type: (Callable[[], Any]) -> int
    """windowed 打包下把 CLI 输出捕获后用弹窗展示，返回 fn 的退出码。

    仅 windowed 单文件 exe 走此路径；源码运行 / console 打包仍直接打印到控制台。
    """
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn()
    text = buf.getvalue().strip() or "（无输出）"
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    try:
        messagebox.showinfo("文件夹同步备份工具", text)
    finally:
        root.destroy()
    return rc


def run_cli(argv, app_dir=None):
    # type: (list, Optional[str]) -> int
    """无头模式：--list / --sync <名称或ID>。返回进程退出码。

    app_dir 可注入（测试用），默认取 main.py 所在目录。
    """
    from config import TaskStore
    from logger import init_logger
    from sync_engine import perform_sync, finalize_sync
    from utils.paths import app_dir as _app_dir_fn

    if app_dir is None:
        app_dir = _app_dir_fn()
    store = TaskStore(os.path.join(app_dir, "config", "tasks.json"))

    if argv[0] == "--list":
        # --list 只读配置，无需初始化日志
        if not store.tasks:
            print("（暂无任务，请先用 GUI 创建）")
            return 0
        for t in store.tasks:
            print("%-10s %s  [%s] %s -> %s  %s" % (
                t.id[:8], t.name, "启用" if t.enabled else "禁用",
                t.source, t.target, t.mode))
        return 0

    init_logger(os.path.join(app_dir, "logs"))

    key = argv[1] if len(argv) > 1 else ""
    task = None
    if key:
        # P1 修复：--list 显示 8 位短 ID，--sync 需支持前缀匹配（README 承诺
        # ID 可用 --list 查看后直接使用）；空 key 不匹配任何任务，避免误触发
        for t in store.tasks:
            if t.id == key or t.id.startswith(key) or t.name == key:
                task = t
                break
    if task is None:
        print("未找到任务: %r（用 --list 查看已有任务）" % key)
        return 1
    if not task.enabled:
        print("任务 '%s' 已禁用，跳过执行（可在 GUI 中启用）" % task.name)
        return 1

    # P1 修复：CLI 与 GUI 一致地排除工具自身 config/logs/baseline 目录，
    # 避免把工具自己同步进备份（此前仅 GUI 路径排除）
    cfg_dir = os.path.dirname(store.path)
    self_paths = {
        os.path.abspath(os.path.join(app_dir, "logs")),
        os.path.abspath(cfg_dir),
        os.path.abspath(os.path.join(cfg_dir, "baseline")),
    }

    try:
        res = perform_sync(task, self_paths=self_paths)
    except Exception as e:
        # 无头模式任何业务异常都要可控退出，避免未捕获 traceback
        print("同步失败 [%s]: %s" % (task.name, e))
        store.update_runtime(task)
        return 3
    finalize_sync(task, res, store)
    print("%s: %s" % (task.name, task.last_summary))
    return 0 if res.get("fail_count", 0) == 0 else 2


def main():
    # type: () -> None
    argv = sys.argv[1:]
    if argv and argv[0] in ("--list", "--sync", "--help", "-h"):
        popup = _is_windowed_frozen()
        if argv[0] in ("--help", "-h"):
            if popup:
                _cli_output_popup(_print_help)
            else:
                _print_help()
            sys.exit(0)
        if popup:
            sys.exit(_cli_output_popup(lambda: run_cli(argv)))
        sys.exit(run_cli(argv))

    import tkinter as tk
    from tkinter import ttk
    from gui_app import App

    root = tk.Tk()
    try:
        style = ttk.Style()
        # 使用系统原生主题（Win7 默认 vista/winnative，无需额外依赖）
        available = style.theme_names()
        for prefer in ("vista", "winnative", "clam", "default"):
            if prefer in available:
                style.theme_use(prefer)
                break
    except Exception:
        pass

    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
