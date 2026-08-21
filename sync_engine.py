"""同步引擎：差异对比（diff）+ 执行（apply）+ 冲突处理。

核心算法
--------
- 单向镜像（one_way，源 A -> 目标 B）：以 A 为准；B 多余文件在开启
  one_way_delete 时删除，否则仅记录。
- 双向同步（two_way，A <-> B）：以上次同步快照 baseline 判定"自上次同步后
  哪一侧被改动"，避免把未改动侧误覆盖。冲突（两侧都改且内容不同）按策略处理，
  默认"新版本胜出"，并且**先备份落败方**再覆盖，绝不静默丢数据。

兼容要点
--------
- FAT32 mtime 2 秒精度：size 相同且 mtime 仅差容差时，回退到内容哈希比较。
- 冲突"新版本"比较同样带容差。

线程契约
--------
本模块的 diff / apply 运行在 worker 线程。传入的回调 `on_ask` / `progress`
（以及 cancel_event 的检查）都在 worker 线程触发，因此：
- 回调必须线程安全，且**不得触碰任何 tkinter / GUI 对象**（tkinter 非线程安全）。
- GUI 侧应只做 `queue.put`（入队）让主线程渲染，参考 gui_app 的 `_progress_cb`。
"""


import os
import shutil
import stat as stat_mod
from typing import Any, Dict, List, Optional, Set, Tuple

from utils.paths import longpath, join_rel, ensure_dir
from utils.timeutil import is_newer, now_epoch, unique_stamp, MTIME_TOLERANCE
from scanner import scan, hash_file, FileMeta, ScanCancelled
from config import (
    Task, MODE_ONE_WAY, MODE_TWO_WAY,
    CONFLICT_NEWER, CONFLICT_ASK, CONFLICT_SOURCE, CONFLICT_TARGET, CONFLICT_SKIP,
)
from logger import get_logger


class Action(object):
    __slots__ = ("kind", "rel", "detail", "from_path", "to_path", "is_conflict")

    def __init__(self, kind, rel, detail, from_path=None, to_path=None, is_conflict=False):
        # type: (str, str, str, Optional[str], Optional[str], bool) -> None
        self.kind = kind            # copy | delete | conflict | extra
        self.rel = rel
        self.detail = detail
        self.from_path = from_path  # copy: 源文件；conflict: A 侧文件
        self.to_path = to_path      # copy: 目标文件；delete: 待删文件；conflict: B 侧文件
        self.is_conflict = is_conflict

    def __repr__(self):
        # type: () -> str
        return "<Action %s %s>" % (self.kind, self.rel)


class DiffResult(object):
    def __init__(self):
        # type: () -> None
        self.actions = []  # type: List[Action]
        self.copy_count = 0
        self.delete_count = 0
        self.conflict_count = 0
        self.extra_count = 0
        self.mkdir_count = 0
        self.rmdir_count = 0
        self.type_conflict_count = 0

    def add(self, action):
        # type: (Action) -> None
        self.actions.append(action)
        if action.kind == "copy":
            self.copy_count += 1
        elif action.kind == "delete":
            self.delete_count += 1
        elif action.kind == "conflict":
            self.conflict_count += 1
        elif action.kind == "extra":
            self.extra_count += 1
        elif action.kind == "mkdir":
            self.mkdir_count += 1
        elif action.kind == "rmdir":
            self.rmdir_count += 1
        elif action.kind == "type_conflict":
            self.type_conflict_count += 1

    def is_empty(self):
        # type: () -> bool
        return len(self.actions) == 0

    def summary(self):
        # type: () -> str
        # 只列出非零项：避免空目录同步/类型冲突等场景摘要全是 0 造成漏报
        parts = []
        if self.copy_count:
            parts.append("新增/覆盖 %d" % self.copy_count)
        if self.delete_count:
            parts.append("删除 %d" % self.delete_count)
        if self.mkdir_count:
            parts.append("创建目录 %d" % self.mkdir_count)
        if self.rmdir_count:
            parts.append("删除目录 %d" % self.rmdir_count)
        if self.conflict_count:
            parts.append("冲突 %d" % self.conflict_count)
        if self.type_conflict_count:
            parts.append("类型冲突 %d" % self.type_conflict_count)
        if self.extra_count:
            parts.append("仅目标多余 %d" % self.extra_count)
        return "，".join(parts) if parts else "无差异"


def _content_differs(a, b, a_path, b_path, cancel_event=None, tolerant=False):
    # type: (FileMeta, FileMeta, str, str, Optional[Any], bool) -> bool
    """size 不同即判不同；size 相同且 mtime 完全相等视为相同（copy2 保留 mtime）。

    size 相同但 mtime 有差异（含 FAT32 2 秒粒度、<2 秒快速编辑的微差）时，
    用内容哈希确认，避免漏同步。cancel_event 置位时经 hash_file 抛 ScanCancelled。
    tolerant=True（task.compare="fast"）：|Δmtime| 在 FAT32 容差内且 size 相同
    直接判相同，跳过双侧哈希读盘——代价是 2 秒内连续两次编辑可能漏判一次，
    换取大目录在 FAT32 目标（mtime 被 copy2 截断到 2 秒粒度）上每轮
    全量重哈希的开销。
    """
    if a.size != b.size:
        return True
    if a.mtime == b.mtime:
        return False
    if tolerant and abs(a.mtime - b.mtime) <= MTIME_TOLERANCE:
        return False
    if a.hash is None:
        a.hash = hash_file(a_path, cancel_event=cancel_event)
    if b.hash is None:
        b.hash = hash_file(b_path, cancel_event=cancel_event)
    if a.hash is None or b.hash is None:
        # 哈希失败（如权限）时保守认为不同，触发复制（覆盖）以保护数据
        return True
    return a.hash != b.hash


def _classify(snap, rel, baseline, root=None, cancel_event=None, tolerant=False):
    # type: (Dict[str, FileMeta], str, Dict[str, Dict[str, Any]], Optional[str], Optional[object], bool) -> str
    bl = baseline.get(rel)
    if rel not in snap:
        return "removed" if bl is not None else "absent"
    cur = snap[rel]
    if bl is None:
        return "added"
    if cur.size != bl.get("size"):
        return "modified"
    bl_mtime = bl.get("mtime")
    if cur.mtime == bl_mtime:
        return "same"
    if (tolerant and isinstance(bl_mtime, (int, float))
            and abs(cur.mtime - bl_mtime) <= MTIME_TOLERANCE):
        # fast 模式：size 相同且 mtime 差在 FAT32 容差内，免哈希判 same
        return "same"
    # mtime 微差：用内容哈希兜底，避免 FAT32(2s 精度) / NTFS 快速编辑被容差误判为未改
    bh = bl.get("hash")
    if bh is not None:
        if cur.hash is None and root is not None:
            cur.hash = hash_file(join_rel(root, rel), cancel_event=cancel_event)
        if cur.hash is not None and cur.hash == bh:
            return "same"
        return "modified"
    # baseline 无哈希可比对（如首次/单向）时保守判 modified，宁可多复制也不漏同步
    return "modified"


def diff(src_snap, dst_snap, task, src_root, dst_root, baseline=None,
         cancel_event=None):
    # type: (Dict[str, FileMeta], Dict[str, FileMeta], Task, str, str, Optional[Dict[str, Dict[str, Any]]], Optional[object]) -> DiffResult
    """根据源/目标快照与任务配置计算待执行动作。

    目录条目（FileMeta.is_dir=True）单独处理：单向镜像创建/删除目标侧目录，
    双向同步合并两侧目录（不传播目录删除，删除交给文件删除后的空目录清理）。
    文件与目录同名（类型冲突）生成 type_conflict 动作，由 apply 明确失败，
    避免每次运行都因 os.replace(file→dir) 失败而不收敛。
    """
    result = DiffResult()
    if baseline is None:
        baseline = task.baseline
    # fast 比较模式（task.compare="fast"）：mtime 容差内免哈希（见 _content_differs）
    tolerant = getattr(task, "compare", "auto") == "fast"

    if task.mode == MODE_ONE_WAY:
        for rel in sorted(set(src_snap) | set(dst_snap)):
            sp = join_rel(src_root, rel)
            dp = join_rel(dst_root, rel)
            in_src = rel in src_snap
            in_dst = rel in dst_snap
            src_isdir = in_src and src_snap[rel].is_dir
            dst_isdir = in_dst and dst_snap[rel].is_dir
            # SE1：一侧文件、一侧目录 -> 类型冲突，明确失败而非不收敛
            if in_src and in_dst and src_isdir != dst_isdir:
                result.add(Action("type_conflict", rel,
                                  "类型冲突:源为%s/目标为%s" % (
                                      "目录" if src_isdir else "文件",
                                      "目录" if dst_isdir else "文件"), sp, dp))
                continue
            # S1：目录创建/删除
            if src_isdir or dst_isdir:
                if in_src and not in_dst:
                    result.add(Action("mkdir", rel, "创建目录", None, dp))
                elif not in_src and in_dst:
                    if task.one_way_delete:
                        result.add(Action("rmdir", rel, "删除目录", None, dp))
                    else:
                        result.add(Action("extra", rel, "目标多余目录(保留)", None, dp))
                continue
            # 文件逻辑
            if in_src and not in_dst:
                result.add(Action("copy", rel, "新增", sp, dp))
            elif in_src and in_dst:
                if _content_differs(src_snap[rel], dst_snap[rel], sp, dp,
                                    cancel_event=cancel_event, tolerant=tolerant):
                    result.add(Action("copy", rel, "修改", sp, dp))
            elif not in_src and in_dst:
                if task.one_way_delete:
                    result.add(Action("delete", rel, "删除(目标多余)", dst_root, dp))
                else:
                    result.add(Action("extra", rel, "目标多余(保留)", None, dp))
        return result

    # two_way
    rels = sorted(set(src_snap) | set(dst_snap) | set(baseline))
    for rel in rels:
        sp = join_rel(src_root, rel)
        dp = join_rel(dst_root, rel)
        in_src = rel in src_snap
        in_dst = rel in dst_snap
        src_isdir = in_src and src_snap[rel].is_dir
        dst_isdir = in_dst and dst_snap[rel].is_dir
        if in_src and in_dst and src_isdir != dst_isdir:
            result.add(Action("type_conflict", rel, "类型冲突:文件/目录", sp, dp))
            continue
        # S1：目录双向合并（只创建，不传播删除）
        if src_isdir or dst_isdir:
            if in_src and not in_dst:
                result.add(Action("mkdir", rel, "A->B 创建目录", None, dp))
            elif in_dst and not in_src:
                result.add(Action("mkdir", rel, "B->A 创建目录", None, sp))
            continue
        sa = _classify(src_snap, rel, baseline, src_root,
                       cancel_event=cancel_event, tolerant=tolerant)
        sb = _classify(dst_snap, rel, baseline, dst_root,
                       cancel_event=cancel_event, tolerant=tolerant)
        for act in _reconcile_two(rel, sa, sb, src_snap, dst_snap, sp, dp, task,
                                  src_root, dst_root, cancel_event=cancel_event,
                                  tolerant=tolerant):
            result.add(act)
    return result


def _reconcile_two(rel, sa, sb, src_snap, dst_snap, sp, dp, task, src_root, dst_root,
                   cancel_event=None, tolerant=False):
    # type: (str, str, str, Dict[str, FileMeta], Dict[str, FileMeta], str, str, Task, str, str, Optional[object], bool) -> List[Action]
    copy_a = Action("copy", rel, "A->B 同步", sp, dp)   # 源 -> 目标
    copy_b = Action("copy", rel, "B->A 同步", dp, sp)   # 目标 -> 源

    if sa == "added" and sb == "added":
        if _content_differs(src_snap[rel], dst_snap[rel], sp, dp,
                            cancel_event=cancel_event, tolerant=tolerant):
            return [Action("conflict", rel, "两侧均新增且内容不同", sp, dp, True)]
        return []
    if sa == "added":
        return [copy_a]
    if sb == "added":
        return [copy_b]
    if sa == "modified" and sb == "modified":
        if _content_differs(src_snap[rel], dst_snap[rel], sp, dp,
                            cancel_event=cancel_event, tolerant=tolerant):
            return [Action("conflict", rel, "两侧均修改且内容不同", sp, dp, True)]
        return []
    if sa == "modified":
        return [copy_a]
    if sb == "modified":
        return [copy_b]
    if sa == "removed" and sb == "removed":
        return []
    if sa == "removed":
        if getattr(task, "two_way_delete", False):
            return [Action("delete", rel, "删除(B 侧)", dst_root, dp)]
        return []
    if sb == "removed":
        if getattr(task, "two_way_delete", False):
            return [Action("delete", rel, "删除(A 侧)", src_root, sp)]
        return []
    return []


def _do_copy(from_path, to_path):
    # type: (str, str) -> None
    """原子复制：先写临时文件再替换，进程中途被杀不会留下半截目标文件。

    tmp 名带进程号：调度器允许不同任务并行，两个任务写同一目标文件时
    固定名 tmp 会互踩产生交叉损坏；后缀保持 .tmp~ 与 scanner 的排除
    规则（endswith(".tmp~")）兼容。
    """
    ensure_dir(os.path.dirname(to_path))
    tmp = "%s.%d.tmp~" % (to_path, os.getpid())
    try:
        shutil.copy2(longpath(from_path), longpath(tmp))
        os.replace(longpath(tmp), longpath(to_path))
    except OSError:
        try:
            os.remove(longpath(tmp))
        except OSError:
            pass
        raise


def _do_delete(path):
    # type: (str) -> None
    """删除一个文件（或符号链接）。目录删除走专门的 rmdir action，不经此函数。"""
    lp = longpath(path)
    if os.path.isdir(lp) and not os.path.islink(lp):
        # diff 之后路径被外部改成目录（race）：按文件删除要么失败要么误删整目录，
        # 不能静默假成功——计为失败，留给下次同步按类型冲突重新判定
        raise OSError("待删除路径已变为目录，拒绝按文件删除: %s" % path)
    if os.path.isfile(lp) or os.path.islink(lp):
        os.remove(lp)
    # 既非文件也非目录（已消失）视为成功：删除动作幂等


def _prune_empty_dirs(path, root):
    # type: (str, str) -> None
    """从 path 向上删除空目录，直到 root 或非空（统一走长路径前缀）。"""
    cur = os.path.dirname(path)
    root_abs = os.path.abspath(root)
    while cur and os.path.abspath(cur) != root_abs:
        lp = longpath(cur)
        try:
            if os.path.isdir(lp) and not os.listdir(lp):
                os.rmdir(lp)
                cur = os.path.dirname(cur)
            else:
                break
        except OSError:
            break


def _resolve_conflict(action, policy, on_ask=None):
    # type: (Action, str, Optional[Any]) -> Tuple[str, bool, bool]
    """处理冲突。返回 (结果描述, 是否失败, 是否未解决)。

    备份落败方后再覆盖，确保不丢数据：
    - 备份落败方失败时不覆盖（README 承诺"绝不静默丢数据"），计为失败，
      冲突保持原状，下次同步重试；
    - 覆盖失败同样计为失败（计入 fail_count，任务状态才不会误报"成功"）。
    - "未解决"（skip / 备份失败 / 覆盖失败）的 rel 由 apply_actions 汇总，
      重建 baseline 时排除，保证下次同步重新进入冲突流程，而不是把
      冲突"固化"成一次无备份的单侧覆盖。
    """
    logger = get_logger()
    a_path = action.from_path  # 源侧
    b_path = action.to_path    # 目标侧
    # 冲突动作必然携带两侧路径（diff 构造时保证），assert 仅为类型收窄
    assert a_path is not None and b_path is not None
    if policy == CONFLICT_ASK and on_ask is not None:
        # 询问回调抛异常或返回无效值时保守跳过：用户"取消询问"的直觉语义
        # 是"这次先不动"，而不是激进的 newer 覆盖
        try:
            got = on_ask(action)
        except Exception:
            got = None
        valid = (CONFLICT_NEWER, CONFLICT_SOURCE, CONFLICT_TARGET, CONFLICT_SKIP)
        policy = got if got in valid else CONFLICT_SKIP
    if policy == "skip":
        logger.warn("冲突未处理(跳过): %s" % action.rel)
        return "跳过冲突", False, True
    # 选定胜者
    if policy == "source_wins":
        winner, loser = a_path, b_path
    elif policy == "target_wins":
        winner, loser = b_path, a_path
    else:  # newer_wins（默认）
        ma = _safe_mtime(a_path)
        mb = _safe_mtime(b_path)
        if is_newer(ma, mb):
            winner, loser = a_path, b_path
        elif is_newer(mb, ma):
            winner, loser = b_path, a_path
        else:
            winner, loser = a_path, b_path  # mtime 平局取源侧
    # 备份落败方（与原文件同目录）。毫秒级时间戳 + 序号，避免同秒多个冲突互相覆盖
    ts = unique_stamp()
    backup = loser + ".conflict-" + ts
    seq = 1
    while os.path.exists(longpath(backup)):
        backup = loser + ".conflict-" + ts + "-%d" % seq
        seq += 1
    try:
        shutil.copy2(longpath(loser), longpath(backup))
    except OSError as e:
        # 备份失败则不覆盖：覆盖成功而备份缺失会静默丢落败方数据，
        # 违背"先备份再覆盖"的承诺；保持冲突原状，下次同步重试
        logger.error("冲突备份失败，跳过覆盖 %s: %s" % (loser, e))
        return "冲突处理失败(备份不成,未覆盖)", True, True
    # 用胜者覆盖落败方
    try:
        _do_copy(winner, loser)
    except OSError as e:
        logger.error("冲突覆盖失败 %s: %s" % (loser, e))
        return "冲突处理失败", True, True
    logger.info("冲突已解决(胜者:%s, 备份:%s): %s" % (
        "源" if winner is a_path else "目标", backup, action.rel))
    return "冲突已解决", False, False


def _safe_mtime(path):
    # type: (str) -> Optional[float]
    try:
        return os.path.getmtime(longpath(path))
    except OSError:
        return None


def apply_actions(actions, conflict_policy, on_ask=None, cancel_event=None):
    # type: (List[Action], str, Optional[Any], Optional[Any]) -> Tuple[List[str], int, Set[str]]
    """执行动作列表。返回 (日志行列表, 失败数, 未解决冲突 rel 集合)；
    cancel_event 置位时抛 ScanCancelled。

    删除动作的 from_path 记录其所属根目录，执行后在该根内清理空目录。
    未解决冲突（skip / 备份失败 / 覆盖失败）的 rel 由调用方在重建 baseline
    时排除，确保下次同步重新进入冲突流程，而非静默单侧覆盖。
    """
    logger = get_logger()
    logs = []
    fail = 0
    unresolved = set()  # type: Set[str]
    for action in actions:
        if cancel_event is not None and cancel_event.is_set():
            raise ScanCancelled()
        try:
            if action.kind == "copy":
                # copy 动作必有源/目标路径（diff 构造时保证），assert 仅为类型收窄
                assert action.from_path is not None and action.to_path is not None
                _do_copy(action.from_path, action.to_path)
                logs.append("%s %s" % (action.detail, action.rel))
            elif action.kind == "delete":
                assert action.to_path is not None
                _do_delete(action.to_path)
                logs.append("%s %s" % (action.detail, action.rel))
                if action.from_path:
                    assert action.to_path is not None
                    _prune_empty_dirs(action.to_path, action.from_path)
            elif action.kind == "mkdir":
                assert action.to_path is not None
                ensure_dir(action.to_path)
                logs.append("%s %s" % (action.detail, action.rel))
            elif action.kind == "rmdir":
                assert action.to_path is not None
                # best-effort：目录非空则跳过（非空目录由其子文件删除 + 空目录清理收敛），
                # 不计入失败，避免误报
                try:
                    os.rmdir(longpath(action.to_path))
                    logs.append("%s %s" % (action.detail, action.rel))
                except OSError:
                    logger.debug("目录非空或删除失败,跳过: %s" % action.rel)
            elif action.kind == "type_conflict":
                logger.error("类型冲突未处理(文件/目录同名): %s" % action.rel)
                logs.append("[类型冲突] %s: %s" % (action.rel, action.detail))
                fail += 1
            elif action.kind == "conflict":
                msg, conflict_failed, unresolved_kept = _resolve_conflict(
                    action, conflict_policy, on_ask)
                logs.append(msg)
                if conflict_failed:
                    fail += 1
                if unresolved_kept:
                    unresolved.add(action.rel)
            elif action.kind == "extra":
                logs.append("[保留] %s" % action.rel)
        except ScanCancelled:
            raise
        except OSError as e:
            logger.error("执行失败 %s: %s" % (action.rel, e))
            logs.append("[失败] %s: %s" % (action.rel, e))
            fail += 1
    return logs, fail, unresolved


def _stat_meta(root, rel):
    # type: (str, str) -> Optional[FileMeta]
    """取 root 下某相对路径的当前文件状态；不存在/不可访问/是目录返回 None。

    dirty 条目在执行阶段刚被改写/删除，直接 stat 即可，无需为少量
    变更路径对整个目标树做全量重扫。
    - follow_symlinks=False：与 scan 的"不跟随软链"策略一致；
    - 目录返回 None：dirty 路径在执行期被外部改成目录（type_conflict 场景
      恰会发生），构造出的 is_dir=False 条目会绕过 baseline 的目录过滤。
    """
    try:
        st = os.stat(longpath(join_rel(root, rel)), follow_symlinks=False)
    except OSError:
        return None
    if stat_mod.S_ISDIR(st.st_mode):
        return None
    return FileMeta(st.st_size, st.st_mtime)


def _dst_dirty_rels(actions, src_root, dst_root):
    # type: (List[Action], str, str) -> set
    """收集本次执行在目标侧产生新增/覆盖/删除的相对路径（重建 baseline 时需重扫）。"""
    dirty = set()
    for act in actions:
        tp = os.path.abspath(act.to_path) if act.to_path else None
        if tp is None:
            continue
        in_dst = tp == dst_root or tp.startswith(dst_root + os.sep)
        if act.kind == "copy" and in_dst:
            dirty.add(act.rel)
        elif act.kind == "delete" and in_dst:
            dirty.add(act.rel)
        elif act.kind == "conflict":
            # 冲突解决可能覆盖任一侧；该条目统一重扫，代价极小
            dirty.add(act.rel)
    return dirty


def build_baseline_after(task, dst_root, self_paths=None, snap=None,
                         old_baseline=None, dirty_rels=None, cancel_event=None,
                         exclude_rels=None):
    # type: (Task, str, Optional[Any], Optional[Dict[str, FileMeta]], Optional[Dict[str, Dict[str, Any]]], Optional[Any], Optional[object], Optional[Set[str]]) -> Dict[str, Dict[str, Any]]
    """同步完成后重建 baseline（双向同步两端一致）。

    - snap：diff 阶段的目标侧快照（可含缓存哈希），避免全量重扫重读。
    - dirty_rels：本次在目标侧新增/覆盖/删除的相对路径；快照对这些条目已过期，
      仅对它们直接 stat 取真实状态（含删除后消失的条目），不做全量重扫。
    - old_baseline：旧 baseline；size/mtime 与快照一致的条目直接沿用其哈希
      （内容不可能变化），把全量哈希降为"仅变更文件"。
    - exclude_rels：未解决冲突的条目**不写入**（旧的也不保留）。这样下次
      两侧都判 added -> 重新进入冲突流程；否则 baseline 记住落败方现状后，
      冲突会"退化"成一次无备份的单侧覆盖（静默丢数据）。
    - 目录条目（is_dir=True）不写入 baseline：目录只做创建/合并，不做内容比对，
      写入只会污染双向同步的 classify。
    - cancel_event：置位时经 scan/hash_file 抛 ScanCancelled，重建可被取消。
    """
    dirty = set(dirty_rels or set())
    exclude = set(exclude_rels or set())
    fresh = {}  # type: Dict[str, FileMeta]
    if snap is None:
        # 无快照可用（如直接调用 apply_diff 的旧路径）：全量扫一次目标侧
        fresh = scan(dst_root, include=task.include, exclude=task.exclude,
                     self_paths=self_paths, with_hash=False,
                     cancel_event=cancel_event)
        for rel in exclude:
            fresh.pop(rel, None)
    else:
        for rel, meta in snap.items():
            if rel in exclude:
                continue
            if rel not in dirty:
                fresh[rel] = meta
        for rel in dirty:
            if rel in exclude:
                continue
            m = _stat_meta(dst_root, rel)
            if m is not None:
                fresh[rel] = m
    old_baseline = old_baseline or {}
    base = {}  # type: Dict[str, Dict[str, Any]]
    for rel, meta in fresh.items():
        if meta.is_dir:
            continue
        old = old_baseline.get(rel)
        if (old is not None and old.get("size") == meta.size
                and old.get("mtime") == meta.mtime and old.get("hash") is not None):
            base[rel] = {"size": meta.size, "mtime": meta.mtime, "hash": old["hash"]}
            continue
        h = meta.hash
        if h is None:
            h = hash_file(join_rel(dst_root, rel), cancel_event=cancel_event)
        entry = {"size": meta.size, "mtime": meta.mtime}  # type: Dict[str, Any]
        if h is not None:
            entry["hash"] = h
        base[rel] = entry
    return base


def apply_diff(task, diff_result, conflict_policy=None, self_paths=None,
               logger=None, on_ask=None, cancel_event=None,
               dst_snap=None):
    # type: (Any, Any, Optional[str], Optional[Any], Any, Optional[Any], Optional[object], Optional[Dict[str, FileMeta]]) -> Dict[str, Any]
    """执行既有的 DiffResult（所见即所得：执行的就是预览确认过的动作集合）。

    内部完成动作执行、失败统计、baseline 重建与任务状态更新。
    """
    if logger is None:
        logger = get_logger()
    dst_root = os.path.abspath(task.target)
    src_root = os.path.abspath(task.source)
    if diff_result.is_empty():
        task.last_run = now_epoch()
        task.last_status = "无需同步"
        task.last_summary = "无差异"
        logger.info("任务[%s] 无需同步" % task.name)
        return {"diff": diff_result, "logs": [], "changed": False, "fail_count": 0}
    policy = conflict_policy or task.conflict_policy
    if policy == CONFLICT_ASK and on_ask is None:
        # 无人值守（调度器）时回退默认，避免卡死
        policy = CONFLICT_NEWER
    logs, fail, unresolved = apply_actions(diff_result.actions, policy,
                                           on_ask=on_ask,
                                           cancel_event=cancel_event)
    summary = diff_result.summary()
    if fail:
        summary += ", 失败 %d" % fail
        task.last_status = "部分失败"
    else:
        task.last_status = "成功"
    if task.mode == MODE_TWO_WAY:
        old_bl = task.baseline or {}
        dirty = _dst_dirty_rels(diff_result.actions, src_root, dst_root)
        task.baseline = build_baseline_after(
            task, dst_root, self_paths=self_paths,
            snap=dst_snap, old_baseline=old_bl, dirty_rels=dirty,
            cancel_event=cancel_event, exclude_rels=unresolved)
    task.last_run = now_epoch()
    task.last_summary = summary
    logger.info("任务[%s] 完成: %s" % (task.name, summary))
    return {"diff": diff_result, "logs": logs, "changed": True, "fail_count": fail}


def perform_sync(task, logger=None, conflict_override=None, dry_run=False,
                 self_paths=None, progress=None, on_ask=None, cancel_event=None):
    # type: (Task, Any, Optional[str], bool, Optional[Any], Optional[Any], Optional[Any], Optional[object]) -> Dict[str, Any]
    """完整同步流程：扫描 -> 对比 ->（dry_run 预览 | 交 apply_diff 执行）。

    返回 dict：{diff, logs, changed, src_snap, dst_snap, fail_count}。
    dry_run=True 时返回的 diff 与快照可直接交给 apply_diff 复用（所见即所得）。
    """
    if logger is None:
        logger = get_logger()
    src_root = os.path.abspath(task.source)
    dst_root = os.path.abspath(task.target)
    logger.info("开始同步任务[%s] 模式=%s 预览=%s" % (task.name, task.mode, dry_run))

    def _abort(msg):
        # type: (str) -> Dict[str, Any]
        """中止同步：不产生任何动作、不动 baseline，任务计为失败。"""
        logger.error("任务[%s] 已中止: %s" % (task.name, msg))
        task.last_run = now_epoch()
        task.last_status = "失败"
        task.last_summary = msg
        return {"diff": DiffResult(), "logs": ["[中止] %s" % msg],
                "changed": False, "src_snap": {}, "dst_snap": {},
                "fail_count": 1, "aborted": True}

    # C1 防线一：源目录不可达（U 盘未插入/盘符漂移/权限被拒）时，空快照与
    # "源真的是空目录"无法区分；继续执行会让 one_way_delete 生成对目标的
    # 全量删除动作。目标侧不校验：目标不存在是首次同步的合法场景（copy
    # 的 ensure_dir 会自动创建）。
    if not os.path.isdir(longpath(src_root)):
        return _abort("源目录不可达: %s" % src_root)
    # C1 防线一（目标侧）：目标不存在仅在**首次同步**（baseline 为空）时合法。
    # 若已有非空 baseline（此前同步成功过）而目标根缺失——U 盘被拔/路径漂移/
    # 权限被拒——扫描返回空快照，diff 会把 baseline 内全部条目判成 removed，
    # 配合 two_way_delete 生成"删除(A 侧)"动作，**误删源文件**（与源侧同型风险）。
    if task.baseline and not os.path.isdir(longpath(dst_root)):
        return _abort("目标目录不可达: %s" % dst_root)
    if progress:
        progress("扫描源目录...")
    src_errors = []  # type: List[str]
    src_snap = scan(src_root, include=task.include, exclude=task.exclude,
                    self_paths=self_paths, with_hash=False, progress=progress,
                    cancel_event=cancel_event, error_sink=src_errors)
    if progress:
        progress("扫描目标目录...")
    dst_errors = []  # type: List[str]
    dst_snap = scan(dst_root, include=task.include, exclude=task.exclude,
                    self_paths=self_paths, with_hash=False, progress=progress,
                    cancel_event=cancel_event, error_sink=dst_errors)
    # C1 防线二：任一侧扫描有错误（如子目录权限被拒）时快照不完整，diff
    # 会把缺失条目判成 removed，配合删除传播即误删对侧。宁可失败重试。
    scan_errors = src_errors + dst_errors
    if scan_errors:
        return _abort("扫描不完整(%d 处错误，如 %s)" % (
            len(scan_errors), scan_errors[0]))
    if progress:
        progress("对比差异中...")
    result = diff(src_snap, dst_snap, task, src_root, dst_root,
                  cancel_event=cancel_event)
    if dry_run:
        logs = ["[预览] %s %s" % (a.detail, a.rel) for a in result.actions]
        return {"diff": result, "logs": logs, "changed": not result.is_empty(),
                "src_snap": src_snap, "dst_snap": dst_snap, "fail_count": 0}
    res = apply_diff(task, result, conflict_policy=conflict_override,
                     self_paths=self_paths, logger=logger, on_ask=on_ask,
                     cancel_event=cancel_event, dst_snap=dst_snap)
    res["src_snap"] = src_snap
    res["dst_snap"] = dst_snap
    return res


def finalize_sync(task, result, store, logger=None):
    # type: (Task, Dict[str, Any], Any, Any) -> None
    """同步完成后的统一收尾：审计日志入库 + 持久化运行期字段与 baseline。

    CLI（main.run_cli）与 GUI（gui_app._run_task/_apply_worker）共用，
    消除两处重复实现带来的行为漂移风险。异常路径不调用本函数：
    同步未完成时不更新 baseline（由下次同步重扫兜底）。
    """
    if logger is None:
        logger = get_logger()
    for ln in (result or {}).get("logs", []):
        logger.info(ln)
    store.update_runtime(task)
    store.save_baseline(task)
