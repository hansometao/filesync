"""任务数据模型与 JSON 持久化。

约定：
- 所有时间以 epoch 秒（浮点）存储，避免 Python 3.8 下 fromisoformat 的时区短板。
- baseline：上次成功同步后两端一致的快照，relpath -> {"size": int, "mtime": float}。
  用于双向同步判定"自上次同步后哪一侧被改动"。
"""

import os
import json
import re
import shutil
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from dataclasses import dataclass, field, asdict

from utils.timeutil import unique_stamp

MODE_ONE_WAY = "one_way"
MODE_TWO_WAY = "two_way"

CONFLICT_NEWER = "newer_wins"
CONFLICT_SOURCE = "source_wins"
CONFLICT_TARGET = "target_wins"
CONFLICT_SKIP = "skip"
CONFLICT_ASK = "ask"
CONFLICT_POLICIES = [
    CONFLICT_NEWER,
    CONFLICT_SOURCE,
    CONFLICT_TARGET,
    CONFLICT_SKIP,
    CONFLICT_ASK,
]

# 冲突策略中文标签：GUI 两个对话框（新增/编辑、差异预览）共用一份，
# 避免重复定义导致新增策略时漏改一处的漂移
POLICY_LABELS = {
    CONFLICT_NEWER: "新版本胜出",
    CONFLICT_SOURCE: "源侧胜出",
    CONFLICT_TARGET: "目标侧胜出",
    CONFLICT_SKIP: "跳过(不处理)",
    CONFLICT_ASK: "逐个询问",
}

SCHED_INTERVAL = "interval"
SCHED_DAILY = "daily"
SCHED_WEEKLY = "weekly"

# HH:MM 与周几(1-7)的格式校验正则：表单校验（validate_schedule_input）、
# 表单解析（gui_task_dialog._on_save）与持久层清洗（Schedule.from_dict）共用一份
HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
WEEKDAY_RE = re.compile(r"^[1-7]$")


def validate_schedule_input(sched_enabled, sched_type, interval_text, times_text, weekdays_text):
    # type: (bool, str, str, str, str) -> Optional[str]
    """校验任务调度表单输入（interval / times / weekdays）。

    返回错误消息；None 表示合法。GUI（gui_task_dialog._on_save）与无头测试共用，
    把校验逻辑从 tkinter 层抽离以便无界面测试。只做校验不做解析，
    调用方在通过后按既有逻辑解析（格式已保证合法）。
    """
    # 间隔仅 interval 类型使用：daily/weekly 下间隔栏可为空/任意值，
    # 不应因无关字段拦截保存（解析侧对非 interval 类型有兜底）
    if sched_enabled and sched_type == SCHED_INTERVAL:
        try:
            interval = int(interval_text)
        except ValueError:
            return "间隔(分钟)必须是整数，当前值：%s" % interval_text
        if interval < 1:
            return "间隔(分钟)必须是正整数（>=1）"
    # 时刻/周几的格式校验同样只对启用中的 daily/weekly 生效：
    # interval 类型下这两栏的残留输入与当前任务无关，不应拦截保存
    # （解析侧同样只保留合法值，垃圾输入不会进入 Task）
    if sched_enabled and sched_type in (SCHED_DAILY, SCHED_WEEKLY):
        times = [t.strip() for t in times_text.split(",") if t.strip()]
        for t in times:
            if not HHMM_RE.match(t):
                return "每日时刻格式应为 HH:MM（00:00-23:59），非法值：%s" % t
        if not times:
            return "启用每日/每周定时时请至少填写一个时刻，如 08:00,20:00"
        weekdays = []
        for w in [x.strip() for x in weekdays_text.split(",") if x.strip()]:
            if not WEEKDAY_RE.match(w):
                return "每周(1-7)应为 1-7 的数字（1=周一…7=周日），非法值：%s" % w
            weekdays.append(int(w))
        if sched_enabled and sched_type == SCHED_WEEKLY and not weekdays:
            return "启用每周定时时请至少填写一个周几，如 1,3,5（1=周一）"
    return None


def sync_identity_changed(prev_src, prev_dst, prev_mode, new_src, new_dst, new_mode):
    # type: (str, str, str, str, str, str) -> bool
    """判断任务编辑是否改变了"同步身份"（源路径/目标路径/同步方向）。

    身份变更后旧 baseline 描述的仍是旧路径对的一致性快照，继续沿用会把
    新目标侧既有文件按 baseline 误分类（removed/modified）：双向删除开启
    时源侧文件被普通 delete 动作**无备份删除**，目标侧同名异容文件被无备
    份覆盖、多余文件反向拷入源。调用方（gui_app._on_edit）据此作废
    baseline，下次同步按首同步语义重分类（同内容 no-op；异内容走冲突流程，
    先备份后覆盖）。include/exclude 变更无需作废：union 分类下新增可见
    文件走 added+added 冲突流程（先备份），不再可见条目两侧同缺为 no-op，
    天然安全。纯函数无 tkinter 依赖，GUI 与无头测试共用。
    """
    return (os.path.normcase(os.path.abspath(prev_src))
            != os.path.normcase(os.path.abspath(new_src))
            or os.path.normcase(os.path.abspath(prev_dst))
            != os.path.normcase(os.path.abspath(new_dst))
            or prev_mode != new_mode)


@dataclass
class Schedule(object):
    enabled: bool = False
    type: str = SCHED_INTERVAL          # interval | daily | weekly
    interval_minutes: int = 60
    times: List[str] = field(default_factory=list)  # ["08:00", "20:00"]
    weekdays: List[int] = field(default_factory=list)  # weekly: [1..7] 1=周一

    def to_dict(self):
        # type: () -> Dict[str, Any]
        return {
            "enabled": self.enabled,
            "type": self.type,
            "interval_minutes": self.interval_minutes,
            "times": list(self.times),
            "weekdays": list(self.weekdays),
        }

    @classmethod
    def from_dict(cls, d):
        # type: (Optional[Dict[str, Any]]) -> "Schedule"
        """从 dict 重建 Schedule，逐字段防御性清洗。

        tasks.json 可能被手工编辑或外部工具写坏：JSON 语法合法但字段类型
        错误（如 interval_minutes="abc"、times=[{"x":1}]）的值若原样通过，
        会在调度循环里每秒抛 TypeError/AttributeError——不仅这一个任务失效，
        _poll_once 的 for 循环被打断后排在后面的所有任务都得不到轮询。
        清洗原则：类型不对/取值越界就回退默认，坏数据不让调度瘫痪。
        """
        if not isinstance(d, dict):
            # 非 dict 真值（如字符串 "abc"）同样防护：d or {} 只拦假值，
            # 字符串真值会直接 AttributeError（load() 的 except 兜住了，
            # 但直接调用 from_dict 的用户不应崩溃）
            d = {}
        stype = d.get("type", SCHED_INTERVAL)
        if stype not in (SCHED_INTERVAL, SCHED_DAILY, SCHED_WEEKLY):
            stype = SCHED_INTERVAL
        try:
            interval = int(d.get("interval_minutes", 60))
        except (TypeError, ValueError):
            interval = 60
        if interval < 1:
            interval = 60
        raw_times = d.get("times", [])
        times = [t.strip() for t in raw_times
                 if isinstance(t, str) and HHMM_RE.match(t.strip())] \
            if isinstance(raw_times, list) else []
        raw_wd = d.get("weekdays", [])
        weekdays = []  # type: List[int]
        if isinstance(raw_wd, list):
            for w in raw_wd:
                try:
                    n = int(w)
                except (TypeError, ValueError):
                    continue
                if 1 <= n <= 7 and n not in weekdays:
                    weekdays.append(n)
        return cls(
            enabled=bool(d.get("enabled", False)),
            type=stype,
            interval_minutes=interval,
            times=times,
            weekdays=weekdays,
        )


@dataclass
class Task(object):
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = ""
    source: str = ""
    target: str = ""
    mode: str = MODE_ONE_WAY
    one_way_delete: bool = False
    two_way_delete: bool = False       # 双向同步时是否传播删除
    compare: str = "auto"            # auto = mtime+size 再哈希确认
    schedule: Schedule = field(default_factory=Schedule)
    include: List[str] = field(default_factory=list)
    exclude: List[str] = field(default_factory=list)
    conflict_policy: str = CONFLICT_NEWER
    last_run: Optional[float] = None
    last_status: str = ""
    last_summary: str = ""
    baseline: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    enabled: bool = True
    # 运行期计算字段（不持久化）
    next_run: Optional[float] = None

    def to_dict(self):
        # type: () -> Dict[str, Any]
        return {
            "id": self.id,
            "name": self.name,
            "source": self.source,
            "target": self.target,
            "mode": self.mode,
            "one_way_delete": self.one_way_delete,
            "two_way_delete": self.two_way_delete,
            "compare": self.compare,
            "schedule": self.schedule.to_dict(),
            "include": list(self.include),
            "exclude": list(self.exclude),
            "conflict_policy": self.conflict_policy,
            "last_run": self.last_run,
            "last_status": self.last_status,
            "last_summary": self.last_summary,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, d):
        # type: (Dict[str, Any]) -> "Task"
        """从 dict 重建 Task。与 Schedule.from_dict 同理做字段清洗：
        last_run 非数值（如 "abc"）会让调度器的 interval 锚点/补跑判定抛
        TypeError；mode/conflict_policy 白名单外取值回退默认。"""
        last_run = d.get("last_run")
        # bool 是 int 子类：True 会被当成合法 epoch(1)，一并清洗掉
        if isinstance(last_run, bool) or not isinstance(last_run, (int, float)):
            last_run = None
        mode = d.get("mode", MODE_ONE_WAY)
        if mode not in (MODE_ONE_WAY, MODE_TWO_WAY):
            mode = MODE_ONE_WAY
        policy = d.get("conflict_policy", CONFLICT_NEWER)
        if policy not in CONFLICT_POLICIES:
            policy = CONFLICT_NEWER
        # compare 白名单："auto"/"fast" 之外的值静默按 auto 处理
        # （否则未知值会让 diff 的 tolerant 判定行为与配置意图不符）
        compare = d.get("compare", "auto")
        if compare not in ("auto", "fast"):
            compare = "auto"
        # include/exclude 元素类型清洗：与 Schedule.times 同理，坏数据（如
        # include=[123]）原样通过会让 scanner 的 fnmatch.fnmatch(pattern=123)
        # 抛 TypeError，导致同步整体失败。仅保留 str 且 strip 后非空的条目。
        raw_inc = d.get("include", []) or []
        include = [x.strip() for x in raw_inc
                   if isinstance(x, str) and x.strip()] \
            if isinstance(raw_inc, list) else []
        raw_exc = d.get("exclude", []) or []
        exclude = [x.strip() for x in raw_exc
                   if isinstance(x, str) and x.strip()] \
            if isinstance(raw_exc, list) else []
        # 内嵌旧格式 baseline 结构清洗：非 dict 根 / 非 dict 条目一律丢弃，
        # 与独立 baseline 文件加载（_load_baseline）的校验对齐。坏条目若原样
        # 通过，diff 的 bl.get("size") 会抛 AttributeError 使该任务同步失败；
        # 文件路径在重启后会被结构校验自愈，内嵌路径此前没有同等防线。
        raw_bl = d.get("baseline", {})
        if not isinstance(raw_bl, dict):
            raw_bl = {}
        baseline = dict((k, v) for k, v in raw_bl.items() if isinstance(v, dict))
        # id 必须是非空 str：手编配置出现数字等非 str id 时若原样通过，
        # _baseline_path 的 task_id + ".json" 会抛 TypeError（save_baseline
        # 只捕 OSError，异常会冲出 load()/finalize_sync），--list 的
        # t.id[:8] 切片同样崩；空/非法 id 一律重新生成（旧格式无 id 的
        # 任务本就走这条路径）
        raw_id = d.get("id")
        task_id = raw_id if isinstance(raw_id, str) and raw_id else uuid.uuid4().hex
        return cls(
            id=task_id,
            name=d.get("name", "") if isinstance(d.get("name"), str) else "",
            source=d.get("source", "") if isinstance(d.get("source"), str) else "",
            target=d.get("target", "") if isinstance(d.get("target"), str) else "",
            mode=mode,
            one_way_delete=bool(d.get("one_way_delete", False)),
            two_way_delete=bool(d.get("two_way_delete", False)),
            compare=compare,
            schedule=Schedule.from_dict(d.get("schedule")),
            include=include,
            exclude=exclude,
            conflict_policy=policy,
            last_run=last_run,
            last_status=d.get("last_status", "") if isinstance(d.get("last_status"), str) else "",
            last_summary=d.get("last_summary", "") if isinstance(d.get("last_summary"), str) else "",
            baseline=baseline,
            enabled=bool(d.get("enabled", True)),
        )


def _safe_print(msg):
    # type: (str) -> None
    """持久层告警的双通道输出：stdout + 日志文件（尽力而为，绝不抛异常）。

    仅打 stdout 在生产环境不可见：GUI quiet 模式抑制控制台；windowed 打包
    （pythonw/无控制台 exe）下 sys.stdout 为 None，print 直接抛异常被吞——
    baseline 写失败、配置损坏隔离等关键告警用户将无从察觉。因此再尽力写入
    foldersync.log：logger 不反向依赖本模块（仅依赖 utils.paths），无循环
    导入；get_logger 未初始化时兜底建 cwd/logs，与 scanner 等模块行为一致。
    """
    try:
        print(msg)
    except Exception:
        pass
    try:
        from logger import get_logger
        get_logger().warn(msg)
    except Exception:
        pass


class TaskStore(object):
    def __init__(self, path):
        # type: (str) -> None
        self.path = path
        self.baseline_dir = os.path.join(
            os.path.dirname(os.path.abspath(path)), "baseline")
        self.tasks = []  # type: List[Task]
        # RLock 可重入：save() 可在 add/update/remove 持锁时嵌套调用
        self._lock = threading.RLock()
        # 损坏配置未能隔离时置位：save() 拒写以防覆盖唯一可修复的原件
        self._load_failed = False
        self.load()

    # ---------- baseline 独立持久化 ----------
    def _baseline_path(self, task_id):
        # type: (str) -> str
        return os.path.join(self.baseline_dir, task_id + ".json")

    def _atomic_write_json(self, path, obj, indent=None):
        # type: (str, Any, Optional[int]) -> None
        """原子写 JSON：先写 tmp 再 os.replace。

        tmp 名带进程号 + 线程号（与 sync_engine._do_copy 同思路）：同一进程内
        GUI 线程与多个 worker 线程可能并发写不同路径的配置/基线，仅用固定
        ".tmp" 会让并发写互踩同一 tmp 产生混写（os.replace 原子只保证最终
        文件完整，不保证 tmp 内容不被交叉写坏）。线程号在同进程内唯一隔离。
        """
        tmp = "%s.%d.%d.tmp~" % (path, os.getpid(), threading.get_ident())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=indent)
            # 断电防护：flush+fsync 强制数据落盘后再 os.replace。部分文件
            # 系统（ext4 延迟分配等）在断电时可能留下空/半截的替换后新文件，
            # 配置/基线丢失会让双向同步退化为全量冲突。写入频率为每次任务
            # 完成/配置变更一次，fsync 开销可忽略。仅 fsync 文件本身：
            # 目录项持久化需 fsync 目录，Windows 无可移植做法，从简。
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def save_baseline(self, task):
        # type: (Task) -> None
        """把 task.baseline 原子写到 config/baseline/<task_id>.json。

        持锁：save_baseline 可能被 GUI 线程（编辑任务）与 worker 线程
        （同步完成）并发调用，写同一任务的 baseline 文件时必须互斥。
        """
        with self._lock:
            try:
                if not os.path.isdir(self.baseline_dir):
                    os.makedirs(self.baseline_dir, exist_ok=True)
                self._atomic_write_json(self._baseline_path(task.id),
                                        task.baseline or {})
            except OSError as e:
                _safe_print("保存 baseline 失败: %s" % e)

    def _load_baseline(self, task):
        # type: (Task) -> None
        """存在独立 baseline 文件时回填（优先于旧的内嵌格式）。

        加载后做结构校验：JSON 合法但形状错误（如 {"a":"b"} 的根、条目
        不是 dict）会让 diff 的 bl.get("size") 抛 AttributeError 崩掉整个
        同步，因此非 dict 根 / 非 dict 条目一律丢弃该文件（记录日志）。
        """
        p = self._baseline_path(task.id)
        if not os.path.exists(p):
            return
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (ValueError, OSError):
            return
        if not isinstance(data, dict):
            _safe_print("baseline 文件格式错误(已忽略): %s" % p)
            return
        bad = [rel for rel, v in data.items() if not isinstance(v, dict)]
        if bad:
            _safe_print("baseline 存在非法条目(已忽略 %d 条): %s" % (len(bad), p))
            data = {k: v for k, v in data.items() if isinstance(v, dict)}
        task.baseline = data

    def _quarantine_corrupt(self):
        # type: () -> bool
        """把损坏的配置移出原路径（保留现场副本），返回现场是否已保住。

        优先改名（原路径不复存在，后续 save 写全新文件）；改名失败（如跨设备
        /权限）退回复制副本。两者都失败返回 False——调用方必须保持只读保护，
        否则后续任何 save() 都会把唯一可修复的原件覆盖掉。
        """
        ts = unique_stamp()
        dst = "%s.corrupt-%s" % (self.path, ts)
        try:
            os.replace(self.path, dst)
            _safe_print("检测到损坏的配置文件，已隔离为 %s" % dst)
            return True
        except OSError:
            pass
        try:
            shutil.copy2(self.path, dst)
            _safe_print("检测到损坏的配置文件，已保留副本 %s" % dst)
            return True
        except OSError as e:
            _safe_print("配置损坏现场保留失败（保持只读保护）: %s" % e)
            return False

    def _cleanup_stale_tmp(self):
        # type: () -> None
        """清理原子写临时文件的崩溃残留（config/ 与 baseline/ 目录）。

        tmp 命名为 "<name>.<pid>.<tid>.tmp~"，进程在写出 tmp 与 os.replace
        之间被杀时会遗留。load() 在启动期调用，本进程尚未开始任何写入，
        存量 .tmp~ 必为残留。仅删除修改时间超过 1 小时的文件：更晚的可能是
        另一个并发进程实例正在写入的 tmp，误删会使其 os.replace 失败
        （该次保存丢失，已有数据不受影响），但仍以不碰为宜。
        同时兼容清理旧版固定命名 "<tasks.json>.tmp"。
        """
        cutoff = time.time() - 3600

        def sweep(d):
            # type: (str) -> None
            try:
                names = os.listdir(d)
            except OSError:
                return
            for fn in names:
                if not fn.endswith(".tmp~"):
                    continue
                fp = os.path.join(d, fn)
                try:
                    if os.path.getmtime(fp) < cutoff:
                        os.remove(fp)
                except OSError:
                    pass

        sweep(os.path.dirname(os.path.abspath(self.path)))
        if os.path.isdir(self.baseline_dir):
            sweep(self.baseline_dir)
        legacy = self.path + ".tmp"
        try:
            if os.path.exists(legacy):
                os.remove(legacy)
        except OSError:
            pass

    def load(self):
        # type: () -> None
        self._load_failed = False
        # 清理上次运行崩溃遗留的原子写临时文件（含旧版固定命名，见方法注释）
        self._cleanup_stale_tmp()
        if not os.path.exists(self.path):
            self.tasks = []
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data = data.get("tasks", [])
            if not isinstance(data, list):
                raise ValueError("配置格式不正确")
            self.tasks = [Task.from_dict(t) for t in data]
        except (ValueError, OSError, TypeError, AttributeError) as e:
            # 隔离失败时置只读保护：save() 会拒写并重试隔离，
            # 保证"要么现场已移走，要么绝不覆盖原件"
            self._load_failed = not self._quarantine_corrupt()
            self.tasks = []
            _safe_print("加载任务配置失败(已保留损坏副本): %s" % e)
            return
        # 手编配置可能出现重复 id：GUI 任务列表以 id 作 Treeview iid，
        # 重复会使 _refresh_tasks 的 insert 抛 TclError，列表刷新中断；
        # store.get 也只会命中第一个。为重复的后续副本重新生成 id——
        # 其 baseline 关联随之失效，下次同步按首同步语义重分类
        # （同内容 no-op；异内容走冲突流程先备份后覆盖，不丢数据）。
        seen_ids = set()  # type: set
        for t in self.tasks:
            if t.id in seen_ids:
                _safe_print("任务[%s] 存在重复 id，已为该副本重新生成" % t.name)
                t.id = uuid.uuid4().hex
            seen_ids.add(t.id)
        # baseline：独立文件优先；旧内嵌格式自动迁移为独立文件
        migrated = False
        for t in self.tasks:
            if t.baseline:
                migrated = True
            self._load_baseline(t)
        if migrated:
            for t in self.tasks:
                if t.baseline:
                    self.save_baseline(t)
            self.save()  # 重写 tasks.json，剥离内嵌 baseline

    def save(self):
        # type: () -> None
        with self._lock:
            if self._load_failed:
                # 损坏现场尚未保住：先重试隔离，仍失败则放弃本次写入。
                # 宁可暂时改不了配置，也不能把唯一可修复的原件覆盖掉。
                if not self._quarantine_corrupt():
                    return
                self._load_failed = False
            try:
                d = os.path.dirname(self.path)
                if d and not os.path.isdir(d):
                    os.makedirs(d, exist_ok=True)
                # 先写临时文件再原子替换，避免写中途崩溃/断电损坏配置；
                # 持锁防止 GUI 线程与 worker 线程并发写同一个 .tmp
                self._atomic_write_json(
                    self.path,
                    {"tasks": [t.to_dict() for t in self.tasks]},
                    indent=2,
                )
            except OSError as e:
                _safe_print("保存任务配置失败: %s" % e)

    def add(self, task):
        # type: (Task) -> None
        with self._lock:
            self.tasks.append(task)
        self.save()

    def update(self, task):
        # type: (Task) -> None
        with self._lock:
            for i, t in enumerate(self.tasks):
                if t.id == task.id:
                    self.tasks[i] = task
                    break
        self.save()

    def update_runtime(self, task):
        # type: (Task) -> None
        """仅持久化运行期字段（last_*），避免整对象覆盖并发的配置编辑。

        baseline 的持久化走 save_baseline，与本接口分离。
        """
        cur = self.get(task.id)
        if cur is None:
            _safe_print("update_runtime: 任务 %s 已不存在，跳过运行期更新" % task.id)
        elif cur is not task:
            cur.last_run = task.last_run
            cur.last_status = task.last_status
            cur.last_summary = task.last_summary
        self.save()

    def remove(self, task_id):
        # type: (str) -> None
        with self._lock:
            self.tasks = [t for t in self.tasks if t.id != task_id]
        try:
            bp = self._baseline_path(task_id)
            if os.path.exists(bp):
                os.remove(bp)
        except OSError:
            pass
        self.save()

    def get(self, task_id):
        # type: (str) -> Optional[Task]
        for t in self.tasks:
            if t.id == task_id:
                return t
        return None

    def snapshot(self):
        # type: () -> List[Task]
        """持锁返回任务列表副本，供跨线程安全迭代（调度器轮询等）。

        直接迭代 self.tasks 时，若 GUI 线程并发 add/remove 原地修改列表，
        会抛 RuntimeError: list changed size during iteration。
        """
        with self._lock:
            return list(self.tasks)
