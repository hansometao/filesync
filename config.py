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
    times = [t.strip() for t in times_text.split(",") if t.strip()]
    for t in times:
        if not re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", t):
            return "每日时刻格式应为 HH:MM（00:00-23:59），非法值：%s" % t
    if sched_enabled and sched_type in (SCHED_DAILY, SCHED_WEEKLY) and not times:
        return "启用每日/每周定时时请至少填写一个时刻，如 08:00,20:00"
    weekdays = []
    for w in [x.strip() for x in weekdays_text.split(",") if x.strip()]:
        if not re.match(r"^[1-7]$", w):
            return "每周(1-7)应为 1-7 的数字（1=周一…7=周日），非法值：%s" % w
        weekdays.append(int(w))
    if sched_enabled and sched_type == SCHED_WEEKLY and not weekdays:
        return "启用每周定时时请至少填写一个周几，如 1,3,5（1=周一）"
    return None


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
        d = d or {}
        return cls(
            enabled=bool(d.get("enabled", False)),
            type=d.get("type", SCHED_INTERVAL),
            interval_minutes=int(d.get("interval_minutes", 60)),
            times=list(d.get("times", []) or []),
            weekdays=list(d.get("weekdays", []) or []),
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
        return cls(
            id=d.get("id") or uuid.uuid4().hex,
            name=d.get("name", ""),
            source=d.get("source", ""),
            target=d.get("target", ""),
            mode=d.get("mode", MODE_ONE_WAY),
            one_way_delete=bool(d.get("one_way_delete", False)),
            two_way_delete=bool(d.get("two_way_delete", False)),
            compare=d.get("compare", "auto"),
            schedule=Schedule.from_dict(d.get("schedule")),
            include=list(d.get("include", []) or []),
            exclude=list(d.get("exclude", []) or []),
            conflict_policy=d.get("conflict_policy", CONFLICT_NEWER),
            last_run=d.get("last_run"),
            last_status=d.get("last_status", ""),
            last_summary=d.get("last_summary", ""),
            baseline=d.get("baseline", {}) or {},
            enabled=bool(d.get("enabled", True)),
        )


def _safe_print(msg):
    # type: (str) -> None
    """pythonw 无控制台时 sys.stdout 为 None，print 会抛异常；安全输出。"""
    try:
        print(msg)
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
        self.load()

    # ---------- baseline 独立持久化 ----------
    def _baseline_path(self, task_id):
        # type: (str) -> str
        return os.path.join(self.baseline_dir, task_id + ".json")

    def save_baseline(self, task):
        # type: (Task) -> None
        """把 task.baseline 原子写到 config/baseline/<task_id>.json。"""
        try:
            if not os.path.isdir(self.baseline_dir):
                os.makedirs(self.baseline_dir, exist_ok=True)
            tmp = self._baseline_path(task.id) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(task.baseline or {}, f, ensure_ascii=False)
            os.replace(tmp, self._baseline_path(task.id))
        except OSError as e:
            _safe_print("保存 baseline 失败: %s" % e)

    def _load_baseline(self, task):
        # type: (Task) -> None
        """存在独立 baseline 文件时回填（优先于旧的内嵌格式）。"""
        p = self._baseline_path(task.id)
        if not os.path.exists(p):
            return
        try:
            with open(p, "r", encoding="utf-8") as f:
                task.baseline = json.load(f) or {}
        except (ValueError, OSError):
            pass

    def _backup_corrupt(self):
        # type: () -> None
        """损坏的配置先保留现场副本，避免后续保存把唯一可修复的原文件覆盖掉。"""
        try:
            ts = unique_stamp()
            dst = "%s.corrupt-%s" % (self.path, ts)
            shutil.copy2(self.path, dst)
            _safe_print("检测到损坏的配置文件，已备份为 %s" % dst)
        except OSError:
            pass

    def load(self):
        # type: () -> None
        # 此时 self.path 仍是上一次的完好版本，删 tmp 无副作用。
        try:
            tmp = self.path + ".tmp"
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
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
            self._backup_corrupt()
            self.tasks = []
            _safe_print("加载任务配置失败(已保留损坏副本): %s" % e)
            return
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
            try:
                d = os.path.dirname(self.path)
                if d and not os.path.isdir(d):
                    os.makedirs(d, exist_ok=True)
                # 先写临时文件再原子替换，避免写中途崩溃/断电损坏配置；
                # 持锁防止 GUI 线程与 worker 线程并发写同一个 .tmp
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(
                        {"tasks": [t.to_dict() for t in self.tasks]},
                        f,
                        ensure_ascii=False,
                        indent=2,
                    )
                os.replace(tmp, self.path)
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
