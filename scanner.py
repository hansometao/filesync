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
import re
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


def _make_hasher():
    # type: () -> Any
    """进程启动时探测一次哈希实现：优先 xxhash，回退 hashlib.md5。

    固定为模块级工厂：一是省去热路径上每次调用的 import 查找；
    二是保证同一进程内算法不漂移（xxh64 与 md5 摘要互不通用，
    中途切换会让 baseline 已缓存的哈希全部失效，触发一轮无谓 modified）。
    """
    try:
        import xxhash
        return xxhash.xxh64
    except (ImportError, AttributeError):
        import hashlib
        return hashlib.md5


_HASHER = _make_hasher()  # type: Any

# 冲突备份文件名：<原文件>.conflict-<YYYYMMDD-HHMMSS.mmm>[-序号]
# 精确匹配避免误伤真实用户文件（如 report.conflict-final.docx 不应被排除）
_CONFLICT_BACKUP_RE = re.compile(r"\.conflict-\d{8}-\d{6}\.\d{3}(?:-\d+)?$")


def hash_file(path, chunk=1 << 20, cancel_event=None):
    # type: (str, int, Optional[Any]) -> Optional[str]
    """计算文件内容哈希（算法由 _make_hasher 固定）。失败返回 None。

    cancel_event 为 threading.Event；置位时在两个分块之间抛出 ScanCancelled，
    使大文件哈希可被取消（不阻塞同步取消）。
    """
    lp = longpath(path)
    h = _HASHER()
    try:
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
        # 对 name 与 rel 分别去尾斜杠匹配：嵌套目录模式如 "foo/bar/" 必须能
        # 命中 rel="foo/bar" 的目录；仅对 name 去斜杠会让嵌套模式完全失效
        if (fnmatch.fnmatch(name, p) or fnmatch.fnmatch(rel, p)
                or fnmatch.fnmatch(name, p2) or fnmatch.fnmatch(rel, p2)):
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


class _ScanCtx(object):
    """scan() 的共享扫描上下文。

    原实现把 result/include/exclude 等封闭在 scan 的嵌套闭包里，recurse
    层层嵌套难以阅读。拆分为模块级函数后用本对象显式传参，语义不变。
    """

    __slots__ = ("result", "include", "exclude", "self_paths", "with_hash",
                 "progress", "cancel_event", "error_sink")

    def __init__(self, include, exclude, self_paths, with_hash, progress,
                 cancel_event, error_sink):
        # type: (List[str], List[str], Set[str], bool, Optional[Callable[[str], None]], Optional[Any], Optional[List[str]]) -> None
        self.result = {}  # type: Dict[str, FileMeta]
        self.include = include
        self.exclude = exclude
        self.self_paths = self_paths
        self.with_hash = with_hash
        self.progress = progress
        self.cancel_event = cancel_event
        self.error_sink = error_sink


def _scan_err(ctx, msg):
    # type: (_ScanCtx, str) -> None
    """扫描错误双通道上报：写日志 + 追加到 error_sink（供调用方中止同步）。"""
    get_logger().warn(msg)
    if ctx.error_sink is not None:
        ctx.error_sink.append(msg)


def _iter_scandir(entries, root, on_err):
    # type: (Any, str, Callable[[str], None]) -> Any
    """安全迭代 scandir 结果：迭代器在 next() 时也可能抛 OSError
    （目录被并发删除等），捕获后记录错误并正常结束，不让异常冲出 scan
    打到调用方；快照不完整由 error_sink 机制交给 perform_sync 中止。
    """
    try:
        while True:
            yield next(entries)
    except StopIteration:
        return
    except (OSError, PermissionError) as e:
        on_err("扫描目录迭代中断 %s: %s" % (root, e))
        return


def _handle_dir(ctx, entry, root, name, rel):
    # type: (_ScanCtx, Any, str, str, str) -> bool
    """处理目录条目，返回是否递归进入。

    检查顺序与拆分前一致：self_paths 跳过 -> 目录级排除 -> stat 取
    mtime（失败计错误）-> Windows 重解析点防循环跳过 -> 无 include 时
    记录目录条目（同步空目录 / 清理目标多余目录需要它）。
    """
    full = os.path.join(root, name)
    if full in ctx.self_paths:
        return False
    if _excluded(rel, name, ctx.exclude):
        return False
    # Windows 目录联接/挂载点等重解析点：is_symlink 抓不到它们，
    # 环状联接会让递归扫描无限循环；与软链同策略跳过（不跟随）。
    # follow_symlinks=False 对普通目录取值与 stat() 相同；
    # Windows 上 scandir 的 stat 来自目录枚举缓存，无额外开销。
    try:
        st = entry.stat(follow_symlinks=False)
    except OSError as e:
        _scan_err(ctx, "无法读取目录信息 %s: %s" % (rel, e))
        return False
    if (os.name == "nt"
            and getattr(st, "st_file_attributes", 0) & 0x400):  # FILE_ATTRIBUTE_REPARSE_POINT
        get_logger().debug("跳过目录联接/重解析点(防循环): %s" % rel)
        return False
    # include 过滤下不记录目录条目：空目录不含任何匹配文件，
    # 同步它只会制造"空目录被传播"的噪音；含匹配文件的目录
    # 由复制时的 ensure_dir 自动创建。无 include 时照常记录
    # 目录条目（同步空目录 / 清理目标多余目录需要它）。
    if not ctx.include:
        ctx.result[rel] = FileMeta(st.st_size, st.st_mtime, h=None, is_dir=True)
    return True


def _handle_file(ctx, entry, root, name, rel):
    # type: (_ScanCtx, Any, str, str, str) -> None
    """处理文件条目并按需触发 progress 回调。

    检查顺序与拆分前一致：排除规则 -> 工具保留名 -> include 过滤 ->
    stat（FileNotFoundError 瞬时竞态静默跳过）-> 可选哈希 -> 记录。
    """
    if _excluded(rel, name, ctx.exclude):
        return
    # 工具保留名：冲突备份只保留在落败方本地，不参与同步/传播；
    # .tmp~ 为原子复制的临时文件残留（异常中断时可能遗留）。
    # 用精确格式匹配备份（*.conflict-* 会误伤 report.conflict-final.docx
    # 这类真实用户文件）；文件 stat 失败（扫描间隙被删/被锁）是瞬时
    # 竞态，静默跳过即可，不记入 error_sink 让整个同步中止。
    if _CONFLICT_BACKUP_RE.search(name) or name.endswith(".tmp~"):
        return
    if not _included(rel, name, ctx.include):
        return
    try:
        st = entry.stat()
    except FileNotFoundError:
        return  # 扫描间隙文件被删：瞬时竞态，跳过即可
    except OSError as e:
        get_logger().debug("无法读取文件信息(跳过) %s: %s" % (rel, e))
        return
    meta = FileMeta(st.st_size, st.st_mtime)
    if ctx.with_hash:
        meta.hash = hash_file(os.path.join(root, name),
                              cancel_event=ctx.cancel_event)
    ctx.result[rel] = meta
    if ctx.progress is not None:
        try:
            ctx.progress(rel)
        except Exception:
            pass


def _recurse(ctx, root, rel_prefix):
    # type: (_ScanCtx, str, str) -> None
    """递归扫描 root，把条目写入 ctx.result（相对路径以 '/' 分隔）。"""
    if ctx.cancel_event is not None and ctx.cancel_event.is_set():
        raise ScanCancelled()
    try:
        entries = os.scandir(longpath(root) if is_longpath_supported() else root)
    except (OSError, PermissionError) as e:
        _scan_err(ctx, "无法扫描目录 %s: %s" % (root, e))
        return
    for entry in _iter_scandir(entries, root,
                               lambda msg: _scan_err(ctx, msg)):
        if ctx.cancel_event is not None and ctx.cancel_event.is_set():
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
            if _handle_dir(ctx, entry, root, name, rel):
                _recurse(ctx, os.path.join(root, name), rel)
        else:
            _handle_file(ctx, entry, root, name, rel)


def scan(directory, include=None, exclude=None, self_paths=None, with_hash=False, progress=None,
         cancel_event=None, error_sink=None):
    # type: (str, Optional[List[str]], Optional[List[str]], Optional[Set[str]], bool, Optional[Callable[[str], None]], Optional[Any], Optional[List[str]]) -> Dict[str, FileMeta]
    """递归扫描目录，返回 {相对路径: FileMeta}。

    - self_paths：要跳过的目录绝对路径集合（工具自身 config/logs）。
    - with_hash：是否直接计算每个文件的哈希（默认 False，按需计算）。
    - progress：可选回调 progress(relpath) 用于刷新 UI。
    - cancel_event：threading.Event；置位后在下一个文件/目录边界抛 ScanCancelled。
    - error_sink：可选 list；扫描中途的错误（目录打不开/读不了 stat）除写日志外
      追加到该列表。调用方（perform_sync）据此识别"快照不完整"并中止同步，
      防止缺失条目被 diff 判成 removed 后经删除传播误删对侧。根目录不存在
      **不**计入（目标侧不存在是首次同步的合法场景），源侧由调用方自行校验。
    """
    if include is None:
        include = []
    if exclude is None:
        exclude = []
    if self_paths is None:
        self_paths = set()
    directory = os.path.abspath(directory)
    ctx = _ScanCtx(include, exclude, self_paths, with_hash, progress,
                   cancel_event, error_sink)

    if not os.path.isdir(longpath(directory) if is_longpath_supported() else directory):
        get_logger().warn("目录不存在: %s" % directory)
        return ctx.result

    _recurse(ctx, directory, "")
    return ctx.result
