"""目录扫描与文件哈希。

要点（兼容性）：
- 不跟随符号链接，避免循环并规避 Windows 7 建链需管理员的问题。
- 支持 include / exclude glob（如 *.tmp、__pycache__/）。
- 哈希优先用 xxhash，缺失回退 hashlib（md5），分块读取以节省内存。
- 排除工具自身配置/日志目录，避免把备份工具自己同步进去。
- 单个文件/目录出错不影响整体，记录后跳过。

线程契约
--------
scan / hash_file 在 worker 线程运行。传入的 `progress` 回调在 worker 线程触发，
必须线程安全且**不得触碰 tkinter**（GUI 侧只入队，参考 gui_app._progress_cb）。
`cancel_event`（threading.Event）置位后在文件/目录/分块边界抛 ScanCancelled。
"""

import os
import fnmatch
from typing import Any, Callable, Dict, List, Optional, Set

from utils.paths import longpath, is_longpath_supported
from logger import get_logger


class ScanCancelled(Exception):
    """用户取消扫描/同步时抛出，由 GUI 层捕获。"""


class FileMeta(object):
    __slots__ = ("size", "mtime", "hash", "is_dir")

    def __init__(self, size, mtime, h=None, is_dir=False):
        # type: (int, float, Optional[str], bool) -> None
        self.size = size
        self.mtime = mtime
        self.hash = h
        self.is_dir = is_dir


def hash_file(path, chunk=1 << 20, cancel_event=None):
    # type: (str, int, Optional[Any]) -> Optional[str]
    """计算文件内容哈希。优先 xxhash，回退 md5。失败返回 None。

    cancel_event 为 threading.Event；置位时在两个分块之间抛出 ScanCancelled，
    使大文件哈希可被取消（不阻塞同步取消）。
    """
    lp = longpath(path)
    try:
        try:
            import xxhash  # mypy: ignore_missing_imports 全局处理，无需逐处 ignore
            h = xxhash.xxh64()
            with open(lp, "rb") as f:
                for block in iter(lambda: f.read(chunk), b""):
                    h.update(block)
                    if cancel_event is not None and cancel_event.is_set():
                        raise ScanCancelled()
            return h.hexdigest()
        except ImportError:
            import hashlib
            h = hashlib.md5()
            with open(lp, "rb") as f:
                for block in iter(lambda: f.read(chunk), b""):
                    h.update(block)
                    if cancel_event is not None and cancel_event.is_set():
                        raise ScanCancelled()
            return h.hexdigest()
    except ScanCancelled:
        raise
    except OSError as e:
        get_logger().warn("哈希计算失败 %s: %s" % (path, e))
        return None


def _matched(name, rel, patterns):
    # type: (str, str, List[str]) -> bool
    if not patterns:
        return False
    for p in patterns:
        p2 = p.rstrip("/")
        if fnmatch.fnmatch(name, p) or fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(name, p2):
            return True
    return False


def _included(rel, name, include):
    # type: (str, str, List[str]) -> bool
    if not include:
        return True
    return _matched(name, rel, include)


def _excluded(rel, name, exclude):
    # type: (str, str, List[str]) -> bool
    return _matched(name, rel, exclude)


def scan(directory, include=None, exclude=None, self_paths=None, with_hash=False, progress=None,
         cancel_event=None):
    # type: (str, Optional[List[str]], Optional[List[str]], Optional[Set[str]], bool, Optional[Callable[[str], None]], Optional[Any]) -> Dict[str, FileMeta]
    """递归扫描目录，返回 {相对路径: FileMeta}。

    - self_paths：要跳过的目录绝对路径集合（工具自身 config/logs）。
    - with_hash：是否直接计算每个文件的哈希（默认 False，按需计算）。
    - progress：可选回调 progress(relpath) 用于刷新 UI。
    - cancel_event：threading.Event；置位后在下一个文件/目录边界抛 ScanCancelled。
    """
    result = {}  # type: Dict[str, FileMeta]
    if include is None:
        include = []
    if exclude is None:
        exclude = []
    if self_paths is None:
        self_paths = set()
    directory = os.path.abspath(directory)

    def _cancelled():
        # type: () -> bool
        return cancel_event is not None and cancel_event.is_set()

    def recurse(root, rel_prefix):
        # type: (str, str) -> None
        if _cancelled():
            raise ScanCancelled()
        try:
            entries = os.scandir(longpath(root) if is_longpath_supported() else root)
        except (OSError, PermissionError) as e:
            get_logger().warn("无法扫描目录 %s: %s" % (root, e))
            return
        for entry in entries:
            if _cancelled():
                raise ScanCancelled()
            name = entry.name
            rel = (rel_prefix + "/" + name) if rel_prefix else name
            try:
                is_link = entry.is_symlink()
            except OSError:
                is_link = False
            if is_link:
                # 不跟随软链；若被显式包含则当作普通记录跳过内容
                get_logger().debug("跳过符号链接(不跟随): %s" % rel)
                continue
            try:
                is_dir = entry.is_dir()
            except OSError:
                is_dir = False
            if is_dir:
                full = os.path.join(root, name)
                if full in self_paths:
                    continue
                # 目录级排除（如 __pycache__/）
                if _excluded(rel, name, exclude):
                    continue
                # Windows 目录联接/挂载点等重解析点：is_symlink 抓不到它们，
                # 环状联接会让递归扫描无限循环；与软链同策略跳过（不跟随）。
                # follow_symlinks=False 对普通目录取值与 stat() 相同；
                # Windows 上 scandir 的 stat 来自目录枚举缓存，无额外开销。
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError as e:
                    get_logger().warn("无法读取目录信息 %s: %s" % (rel, e))
                    continue
                if (os.name == "nt"
                        and getattr(st, "st_file_attributes", 0) & 0x400):  # FILE_ATTRIBUTE_REPARSE_POINT
                    get_logger().debug("跳过目录联接/重解析点(防循环): %s" % rel)
                    continue
                # include 过滤下不记录目录条目：空目录不含任何匹配文件，
                # 同步它只会制造"空目录被传播"的噪音；含匹配文件的目录
                # 由复制时的 ensure_dir 自动创建。无 include 时照常记录
                # 目录条目（同步空目录 / 清理目标多余目录需要它）。
                if not include:
                    result[rel] = FileMeta(st.st_size, st.st_mtime, h=None, is_dir=True)
                recurse(full, rel)
            else:
                if _excluded(rel, name, exclude):
                    continue
                # 工具保留名：冲突备份只保留在落败方本地，不参与同步/传播；
                # .tmp~ 为原子复制的临时文件残留（异常中断时可能遗留）
                if fnmatch.fnmatch(name, "*.conflict-*") or name.endswith(".tmp~"):
                    continue
                if not _included(rel, name, include):
                    continue
                try:
                    st = entry.stat()
                except OSError as e:
                    get_logger().warn("无法读取文件信息 %s: %s" % (rel, e))
                    continue
                meta = FileMeta(st.st_size, st.st_mtime)
                if with_hash:
                    meta.hash = hash_file(os.path.join(root, name),
                                          cancel_event=cancel_event)
                result[rel] = meta
                if progress is not None:
                    try:
                        progress(rel)
                    except Exception:
                        pass

    if not os.path.isdir(directory):
        get_logger().warn("目录不存在: %s" % directory)
        return result

    recurse(directory, "")
    return result
