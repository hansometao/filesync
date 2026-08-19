"""结构化日志：同时输出到文件（强制 UTF-8）与注册的回调（GUI 日志面板）。

设计要点：
- 文件始终以 UTF-8 写入，规避 Windows 7 控制台默认编码问题。
- 线程安全（内部加锁），供调度器后台线程调用。
- 通过 add_callback 把每条日志推给 GUI 刷新。
"""

import os
import sys
import time
import threading
from typing import Callable, List, Optional

from utils.paths import longpath

LEVEL_INFO = "INFO"
LEVEL_WARN = "WARN"
LEVEL_ERROR = "ERROR"
LEVEL_DEBUG = "DEBUG"

_MAX_FILE_BYTES = 2 * 1024 * 1024  # 2MB 后轮转


class AppLogger(object):
    def __init__(self, log_dir, quiet=False):
        # type: (str, bool) -> None
        self._log_dir = log_dir
        self._quiet = quiet  # GUI 模式置 True，抑制控制台打印（文件 + 回调照常）
        self._lock = threading.Lock()
        self._callbacks = []  # type: List[Callable[[str, str], None]]
        self._file = None  # type: Optional[object]
        self._bytes_written = 0
        ensure_dir_safe(log_dir)
        self._path = os.path.join(log_dir, "foldersync.log")
        try:
            self._reopen()
            try:
                self._bytes_written = os.path.getsize(longpath(self._path))
            except OSError:
                self._bytes_written = 0
        except OSError:
            self._file = None

    def _reopen(self):
        # 轮转：超过阈值则备份为 .1
        try:
            if os.path.exists(self._path) and os.path.getsize(self._path) > _MAX_FILE_BYTES:
                backup = self._path + ".1"
                if os.path.exists(backup):
                    os.remove(backup)
                os.rename(self._path, backup)
        except OSError:
            pass
        self._file = open(longpath(self._path), "a", encoding="utf-8")

    def add_callback(self, cb):
        # type: (Callable[[str, str], None]) -> None
        with self._lock:
            self._callbacks.append(cb)

    def _emit(self, level, msg):
        # type: (str, str) -> None
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = "[%s] %s %s" % (ts, level, msg)
        # 锁内：仅写文件 + 拷贝回调列表；回调在锁外调用，
        # 避免持锁期间回调重入（如回调内再打日志/做耗时操作）造成死锁
        with self._lock:
            self._write_line(line)
            cbs = list(self._callbacks)
        for cb in cbs:
            try:
                cb(level, line)
            except Exception:
                pass
        # 控制台：尽量以 UTF-8 输出（GUI 模式 quiet 时跳过，避免终端噪声）
        if self._quiet:
            return
        try:
            if hasattr(sys.stdout, "reconfigure"):
                try:
                    sys.stdout.reconfigure(encoding="utf-8")
                except Exception:
                    pass
            print(line)
        except Exception:
            pass

    def _write_line(self, line):
        # type: (str) -> None
        """写一行并在超限时轮转（调用方须持锁）。"""
        if self._file is None:
            return
        try:
            self._file.write(line + "\n")
            self._file.flush()
            self._bytes_written += len(line.encode("utf-8")) + 1
        except OSError:
            return
        if self._bytes_written > _MAX_FILE_BYTES:
            self._rotate()

    def _rotate(self):
        # type: () -> None
        """运行期轮转：关闭旧文件、rename .1、重开、清零计数（调用方须持锁）。"""
        try:
            self._file.close()
        except OSError:
            pass
        backup = self._path + ".1"
        try:
            if os.path.exists(backup):
                os.remove(backup)
            os.rename(self._path, backup)
        except OSError:
            pass
        try:
            self._file = open(longpath(self._path), "a", encoding="utf-8")
            self._bytes_written = 0
        except OSError:
            self._file = None

    def debug(self, msg):
        self._emit(LEVEL_DEBUG, msg)

    def info(self, msg):
        self._emit(LEVEL_INFO, msg)

    def warn(self, msg):
        self._emit(LEVEL_WARN, msg)

    def error(self, msg):
        self._emit(LEVEL_ERROR, msg)

    def close(self):
        with self._lock:
            if self._file is not None:
                try:
                    self._file.close()
                except OSError:
                    pass
                self._file = None


def ensure_dir_safe(path):
    try:
        lp = longpath(path)
        if not os.path.isdir(lp):
            os.makedirs(lp, exist_ok=True)
    except OSError:
        pass


# 单例：在 main 中初始化
_logger = None  # type: Optional[AppLogger]


def init_logger(log_dir, quiet=False):
    # type: (str, bool) -> AppLogger
    global _logger
    _logger = AppLogger(log_dir, quiet=quiet)
    return _logger


def get_logger():
    # type: () -> AppLogger
    global _logger
    if _logger is None:
        # 兜底：日志目录放在当前工作目录
        _logger = AppLogger(os.path.join(os.getcwd(), "logs"))
    return _logger
