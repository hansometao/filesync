# mypy: ignore-errors
# 测试脚本不做脚本级类型检查：含大量 fake tkinter 类与动态装配，无类型价值
"""无界面自测：覆盖 diff / apply / 冲突 / 调度器核心逻辑。

运行：python test_sync.py
（不依赖 tkinter，可在任意 3.8+ 环境执行）
"""

import os
import sys
import json
import time
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Task, MODE_ONE_WAY, MODE_TWO_WAY, CONFLICT_NEWER, CONFLICT_SOURCE
from sync_engine import perform_sync
from scheduler import Scheduler
from logger import init_logger

import sync_engine as _sync_engine
from main import run_cli

# 日志写到临时目录，避免污染项目 logs/
init_logger(tempfile.mkdtemp())

failures = []


def check(cond, msg):
    # type: (bool, str) -> None
    if cond:
        print("  OK  - %s" % msg)
    else:
        print("  FAIL- %s" % msg)
        failures.append(msg)


def write(path, content, mtime=None):
    # type: (str, str, Optional[float]) -> None
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def fresh_task(mode, src, dst, **kw):
    # type: (str, str, str, **Any) -> Task
    t = Task()
    t.name = "test"
    t.source = src
    t.target = dst
    t.mode = mode
    for k, v in kw.items():
        setattr(t, k, v)
    return t


# ---------- 1. 单向镜像：新增 / 修改 / 删除 ----------
print("[1] 单向镜像")
d = tempfile.mkdtemp()
src = os.path.join(d, "src")
dst = os.path.join(d, "dst")
os.makedirs(src)
os.makedirs(dst)
write(os.path.join(src, "a.txt"), "hello")

t = fresh_task(MODE_ONE_WAY, src, dst, one_way_delete=True)
res = perform_sync(t, dry_run=True)
check(res["diff"].copy_count == 1, "dry-run 检测到 1 个新增")
perform_sync(t)  # 执行
check(os.path.exists(os.path.join(dst, "a.txt")), "新增文件已复制到目标")

write(os.path.join(src, "a.txt"), "hello-updated")
t2 = fresh_task(MODE_ONE_WAY, src, dst, one_way_delete=True)
perform_sync(t2)
with open(os.path.join(dst, "a.txt"), encoding="utf-8") as f:
    check(f.read() == "hello-updated", "修改已同步到目标")

# 目标多余文件应被删除
write(os.path.join(dst, "extra.txt"), "x")
t3 = fresh_task(MODE_ONE_WAY, src, dst, one_way_delete=True)
perform_sync(t3)
check(not os.path.exists(os.path.join(dst, "extra.txt")), "目标多余文件已删除(one_way_delete)")

# 目标多余但关闭删除 -> 保留
write(os.path.join(dst, "keep.txt"), "x")
t4 = fresh_task(MODE_ONE_WAY, src, dst, one_way_delete=False)
res4 = perform_sync(t4)
check(os.path.exists(os.path.join(dst, "keep.txt")), "关闭删除时目标多余文件保留")
check(res4["diff"].extra_count == 1, "记录 1 个仅目标多余")

# ---------- 2. 双向同步：合并 + 冲突 ----------
print("[2] 双向同步")
d = tempfile.mkdtemp()
src = os.path.join(d, "src")
dst = os.path.join(d, "dst")
os.makedirs(src)
os.makedirs(dst)
write(os.path.join(src, "only_a.txt"), "A")
write(os.path.join(dst, "only_b.txt"), "B")

t = fresh_task(MODE_TWO_WAY, src, dst)  # baseline 为空 -> 首次合并
perform_sync(t)
check(os.path.exists(os.path.join(src, "only_b.txt")), "B 侧文件合并到 A")
check(os.path.exists(os.path.join(dst, "only_a.txt")), "A 侧文件合并到 B")

# 冲突：两侧都改，内容不同
write(os.path.join(src, "c.txt"), "src-version", mtime=1000.0)
write(os.path.join(dst, "c.txt"), "dst-version", mtime=2000.0)  # 目标更新
t2 = fresh_task(MODE_TWO_WAY, src, dst, conflict_policy=CONFLICT_NEWER)
perform_sync(t2)
with open(os.path.join(src, "c.txt"), encoding="utf-8") as f:
    ca = f.read()
with open(os.path.join(dst, "c.txt"), encoding="utf-8") as f:
    cb = f.read()
check(ca == cb == "dst-version", "冲突按'新版本胜出'解决，两端一致")
# 落败方为源侧文件，备份生成在 src 目录，命名为 c.txt.conflict-<ts>
backups = [x for x in os.listdir(src) if x.startswith("c.txt.conflict-")]
check(len(backups) == 1, "冲突落败方已备份为 .conflict- 副本")

# 源侧胜出策略
write(os.path.join(src, "d.txt"), "src-wins", mtime=1000.0)
write(os.path.join(dst, "d.txt"), "dst-loses", mtime=2000.0)
t3 = fresh_task(MODE_TWO_WAY, src, dst, conflict_policy=CONFLICT_SOURCE)
perform_sync(t3)
with open(os.path.join(dst, "d.txt"), encoding="utf-8") as f:
    check(f.read() == "src-wins", "冲突按'源侧胜出'解决")

# ---------- 3. 调度器：run_now 触发 ----------
print("[3] 调度器")
d = tempfile.mkdtemp()
src = os.path.join(d, "src")
dst = os.path.join(d, "dst")
os.makedirs(src)
os.makedirs(dst)
write(os.path.join(src, "x.txt"), "x")
import threading
from config import TaskStore
store = TaskStore(os.path.join(d, "tasks.json"))
tk = fresh_task(MODE_ONE_WAY, src, dst, one_way_delete=True)
tk.schedule.enabled = True
tk.schedule.type = "interval"
tk.schedule.interval_minutes = 1
store.add(tk)
ran = []


def _sched_run(task):
    # type: (Task) -> None
    try:
        perform_sync(task)
    finally:
        ran.append(task.id)


sched = Scheduler(store, _sched_run)
ok = sched.run_now(tk.id)
check(ok, "run_now 成功入队")
deadline = time.time() + 10
while tk.id not in ran and time.time() < deadline:
    time.sleep(0.2)
check(tk.id in ran, "调度器在后台线程执行了任务")
check(os.path.exists(os.path.join(dst, "x.txt")), "调度执行后文件已同步")
sched.stop()

# ---------- 2b. 回归 D：双向 mtime 微差(<2s) 的小修改不应漏判 ----------
print("[2b] 回归 D: 双向 mtime 微差漏判修复")
d = tempfile.mkdtemp()
src = os.path.join(d, "src")
dst = os.path.join(d, "dst")
os.makedirs(src)
os.makedirs(dst)
base_mtime = 1700000000.0
write(os.path.join(src, "y.txt"), "v1", mtime=base_mtime)
write(os.path.join(dst, "y.txt"), "v1", mtime=base_mtime)
write(os.path.join(src, "seed.txt"), "seed", mtime=base_mtime)  # 让首次同步有动作 -> 重建 baseline
t = fresh_task(MODE_TWO_WAY, src, dst)
perform_sync(t)
check(os.path.exists(os.path.join(dst, "seed.txt")), "首次双向合并建立基线")
check(bool(t.baseline) and t.baseline.get("y.txt", {}).get("hash"),
      "双向 baseline 携带内容哈希(供微差兜底)")
# 源快速改内容，mtime 仅 +1.5s（<2s FAT32 容差）
write(os.path.join(src, "y.txt"), "v2", mtime=base_mtime + 1.5)
res = perform_sync(t, dry_run=True)
check(any(a.rel == "y.txt" for a in res["diff"].actions),
      "mtime 微差的小修改被判定需同步(未漏判)")
perform_sync(t)
with open(os.path.join(dst, "y.txt"), encoding="utf-8") as f:
    check(f.read() == "v2", "小修改已同步到目标")

# ---------- 1b. 回归 F：单向同步不重建 baseline（避免无用全量扫描/JSON 写）----------
print("[1b] 回归 F: 单向 baseline 不重建")
d = tempfile.mkdtemp()
src = os.path.join(d, "src")
dst = os.path.join(d, "dst")
os.makedirs(src)
os.makedirs(dst)
write(os.path.join(src, "z.txt"), "z")
t = fresh_task(MODE_ONE_WAY, src, dst, one_way_delete=False)
t.baseline = {}
perform_sync(t)
check(t.baseline == {}, "单向同步后 baseline 保持为空(不浪费重建)")

# ---------- 3b. H2: 非法每日时刻容错 ----------
print("[3b] H2: 非法每日时刻")
from utils.timeutil import next_daily_times
check(next_daily_times(["8点", "晚8"], time.time()) is None, "时刻全非法 -> 返回 None(调度线程不崩)")
check(next_daily_times(["08:00"], time.time()) is not None, "合法时刻正常返回")

# ---------- 3c. 调度: interval 锚定 last_run 自动触发 + 运行中顺延(M9) ----------
print("[3c] 调度: interval 自动触发 / 到点运行中顺延")
d = tempfile.mkdtemp()
src = os.path.join(d, "src")
dst = os.path.join(d, "dst")
os.makedirs(src)
os.makedirs(dst)
store2 = TaskStore(os.path.join(d, "tasks.json"))
ti = fresh_task(MODE_ONE_WAY, src, dst)
ti.schedule.enabled = True
ti.schedule.type = "interval"
ti.schedule.interval_minutes = 1
ti.last_run = time.time() - 3600  # 早已到期
store2.add(ti)
# 用可观测的阻塞式 run_task 消除断言竞态：
# no-op lambda 会微秒级完成并自清理，主线程再查 is_task_running 必然 False。
started_evt = threading.Event()
finish_evt = threading.Event()

def blocking_run(task):
    # type: (Task) -> None
    started_evt.set()
    finish_evt.wait(timeout=10)   # 兜底超时，断言失败也不会卡死测试

sched2 = Scheduler(store2, blocking_run)
sched2._poll_once()
check(started_evt.wait(timeout=5), "到期 interval 任务被自动触发(锚定 last_run)")
check(sched2.is_task_running(ti.id), "触发后占用运行槽(worker 阻塞中)")
finish_evt.set()
deadline = time.time() + 10
while sched2.is_task_running(ti.id) and time.time() < deadline:
    time.sleep(0.1)
check(not sched2.is_task_running(ti.id), "worker 正常退出且自清理")
# 到点但任务运行中 -> 顺延 60 秒而非丢弃周期
sched2.acquire(ti.id)
ti.last_run = time.time() - 3600
ti.next_run = None  # 强制重算：粘性 next_run(上次 poll 置为 now+1)可能尚未到期
sched2._poll_once()
check(ti.next_run is not None and abs(ti.next_run - (time.time() + 60)) < 5,
      "到期但运行中 -> 计划顺延 60 秒(非跳到下个周期)")
sched2.release(ti.id)
sched2.stop()

# ---------- 4. H3: 损坏配置保留现场副本 ----------
print("[4] H3: 损坏配置")
d = tempfile.mkdtemp()
cfg = os.path.join(d, "tasks.json")
with open(cfg, "w", encoding="utf-8") as f:
    f.write("{broken json!!!")
store3 = TaskStore(cfg)
check(store3.tasks == [], "损坏配置不崩溃，任务列表为空")
corrupts = [x for x in os.listdir(d) if x.startswith("tasks.json.corrupt-")]
check(len(corrupts) == 1, "损坏文件已备份为 .corrupt- 副本(可手工找回)")

# ---------- 5. H4: 并发 save 不损坏 ----------
print("[5] H4: 并发 save")
store4 = TaskStore(os.path.join(tempfile.mkdtemp(), "tasks.json"))
store4.add(fresh_task(MODE_ONE_WAY, "/tmp/a1", "/tmp/b1"))


def _saver():
    # type: () -> None
    for _ in range(50):
        store4.save()


ths = [threading.Thread(target=_saver) for _ in range(3)]
for t in ths:
    t.start()
for t in ths:
    t.join()
ok = True
try:
    with open(store4.path, "r", encoding="utf-8") as f:
        json.load(f)
except Exception:
    ok = False
check(ok, "3 线程各 save 50 次后 JSON 仍完整可解析")

# ---------- 6. M4: 冲突备份不被同步传播 ----------
print("[6] M4: 冲突备份不传播")
d = tempfile.mkdtemp()
src = os.path.join(d, "src")
dst = os.path.join(d, "dst")
os.makedirs(src)
os.makedirs(dst)
T = 1700000000.0
write(os.path.join(src, "c.txt"), "v1", mtime=T)
write(os.path.join(dst, "c.txt"), "v1", mtime=T)
write(os.path.join(src, "seed.txt"), "seed", mtime=T)
t = fresh_task(MODE_TWO_WAY, src, dst)
perform_sync(t)   # 建立 baseline
write(os.path.join(src, "c.txt"), "v2", mtime=T + 10)
write(os.path.join(dst, "c.txt"), "v3", mtime=T + 20)   # 目标更新 -> 冲突, 目标胜
perform_sync(t)
backups_in_src = [x for x in os.listdir(src) if x.startswith("c.txt.conflict-")]
check(len(backups_in_src) == 1, "冲突落败方在源侧生成备份")
res = perform_sync(t, dry_run=True)
check(res["diff"].is_empty(), "再次双向无差异(备份未产生新动作)")
check(not [x for x in os.listdir(dst) if ".conflict-" in x], "备份未被传播到对端")

# ---------- 7. M8: 单向 <2s 同尺寸修改被检出 ----------
print("[7] M8: 单向容差内微差")
d = tempfile.mkdtemp()
src = os.path.join(d, "src")
dst = os.path.join(d, "dst")
os.makedirs(src)
os.makedirs(dst)
T = 1700000100.0
write(os.path.join(src, "a.txt"), "AAAA", mtime=T)
t = fresh_task(MODE_ONE_WAY, src, dst)
perform_sync(t)
write(os.path.join(src, "a.txt"), "BBBB", mtime=T + 1.5)   # 同尺寸, mtime 仅 +1.5s
res = perform_sync(t, dry_run=True)
check(any(a.rel == "a.txt" for a in res["diff"].actions), "单向 <2s 同尺寸修改被判定需同步")
perform_sync(t)
with open(os.path.join(dst, "a.txt"), encoding="utf-8") as f:
    check(f.read() == "BBBB", "单向微差修改已同步到目标")

# ---------- 8. M10: baseline 分离持久化与旧格式迁移 ----------
print("[8] M10: baseline 分离")
d = tempfile.mkdtemp()
cfg = os.path.join(d, "tasks.json")
store5 = TaskStore(cfg)
t5 = fresh_task(MODE_TWO_WAY, "/tmp/x1", "/tmp/x2")
t5.baseline = {"x.txt": {"size": 1, "mtime": 2.0, "hash": "ab"}}
store5.add(t5)
with open(cfg, "r", encoding="utf-8") as f:
    raw = json.load(f)
check("baseline" not in raw["tasks"][0], "tasks.json 不再内嵌 baseline")
store5.save_baseline(t5)
store6 = TaskStore(cfg)
t6 = store6.get(t5.id)
check(t6 is not None and t6.baseline.get("x.txt", {}).get("hash") == "ab",
      "独立 baseline 文件在 load 时回填")
# 旧格式（内嵌 baseline）自动迁移
d = tempfile.mkdtemp()
cfg = os.path.join(d, "tasks.json")
old = {"tasks": [{"id": "mig1", "name": "旧任务", "source": "/tmp/m1", "target": "/tmp/m2",
                  "mode": "two_way",
                  "baseline": {"y.txt": {"size": 3, "mtime": 4.0, "hash": "cd"}}}]}
with open(cfg, "w", encoding="utf-8") as f:
    json.dump(old, f, ensure_ascii=False)
store7 = TaskStore(cfg)
t7 = store7.get("mig1")
check(t7 is not None and t7.baseline.get("y.txt", {}).get("hash") == "cd",
      "旧内嵌格式迁移后 baseline 可用")
with open(cfg, "r", encoding="utf-8") as f:
    raw = json.load(f)
check("baseline" not in raw["tasks"][0], "迁移后 tasks.json 已剥离内嵌 baseline")
bl_files = os.listdir(os.path.join(d, "baseline"))
check("mig1.json" in bl_files, "迁移生成独立 baseline 文件")

# ---------- 9. L4: 部分失败计入状态 ----------
print("[9] L4: 部分失败")
d = tempfile.mkdtemp()
src = os.path.join(d, "src")
dst = os.path.join(d, "dst")
os.makedirs(src)
os.makedirs(dst)
write(os.path.join(src, "f.txt"), "data")
os.makedirs(os.path.join(dst, "f.txt"))   # 目标同名"目录"导致复制失败
t = fresh_task(MODE_ONE_WAY, src, dst)
res = perform_sync(t)
check(res.get("fail_count") == 1, "复制失败被计数(fail_count=1)")
check(t.last_status == "部分失败", "任务状态置为'部分失败'而非'成功'")
check("失败 1" in t.last_summary, "摘要包含失败数")
check(not os.path.exists(os.path.join(dst, "f.txt.tmp~")), "失败后无 .tmp~ 残留")

# ---------- 10. CLI 无头入口(run_cli) ----------
print("[10] CLI 无头入口")
import io
import contextlib


def _cli(argv, app_dir):
    # type: (list, str) -> Tuple[int, str]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run_cli(list(argv), app_dir=app_dir)
    return rc, buf.getvalue()


d = tempfile.mkdtemp()
cfg = os.path.join(d, "config", "tasks.json")
os.makedirs(os.path.dirname(cfg))
src = os.path.join(d, "src")
dst = os.path.join(d, "dst")
os.makedirs(src)
os.makedirs(dst)
write(os.path.join(src, "a.txt"), "hello")
store_cli = TaskStore(cfg)
tc = fresh_task(MODE_ONE_WAY, src, dst, one_way_delete=True)
tc.name = "cli任务"
tc.enabled = True
store_cli.add(tc)

# --list 非空
rc, out = _cli(["--list"], d)
check(rc == 0 and "cli任务" in out, "--list 列出已有任务(退出码 0)")

# --list 空配置
d_empty = tempfile.mkdtemp()
os.makedirs(os.path.join(d_empty, "config"))
rc, out = _cli(["--list"], d_empty)
check(rc == 0 and "暂无任务" in out, "--list 空配置提示(退出码 0)")

# --sync 未找到
rc, out = _cli(["--sync", "不存在"], d)
check(rc == 1 and "未找到" in out, "--sync 未找到任务(退出码 1)")

# --sync 已禁用
td = fresh_task(MODE_ONE_WAY, src, dst)
td.name = "禁用任务"
td.enabled = False
store_cli.add(td)
rc, out = _cli(["--sync", "禁用任务"], d)
check(rc == 1 and "已禁用" in out, "--sync 已禁用任务(退出码 1)")

# --sync 成功执行
rc, out = _cli(["--sync", "cli任务"], d)
check(rc == 0 and os.path.exists(os.path.join(dst, "a.txt")),
      "--sync 成功执行并同步文件(退出码 0)")

# --sync 异常路径(无头不抛未捕获 traceback，退出码 3)
_orig = _sync_engine.perform_sync


def _boom(task):
    # type: (Task) -> None
    raise RuntimeError("模拟同步异常")


_sync_engine.perform_sync = _boom
try:
    rc, out = _cli(["--sync", "cli任务"], d)
    check(rc == 3 and "同步失败" in out, "--sync 异常路径(退出码 3, 不抛 traceback)")
finally:
    _sync_engine.perform_sync = _orig

# ---------- 11. S1 空目录同步 / SE1 类型冲突 ----------
print("[11] S1 空目录同步 + SE1 类型冲突")

# S1a: 单向空目录创建
d = tempfile.mkdtemp()
src = os.path.join(d, "src")
dst = os.path.join(d, "dst")
os.makedirs(src)
os.makedirs(dst)
os.makedirs(os.path.join(src, "empty_dir"))
write(os.path.join(src, "a.txt"), "x")
t = fresh_task(MODE_ONE_WAY, src, dst)
perform_sync(t)
check(os.path.isdir(os.path.join(dst, "empty_dir")), "S1: 单向空目录创建到目标")

# S1b: 单向 one_way_delete 删除目标多余空目录
os.makedirs(os.path.join(dst, "gone_dir"))
t = fresh_task(MODE_ONE_WAY, src, dst, one_way_delete=True)
perform_sync(t)
check(not os.path.exists(os.path.join(dst, "gone_dir")),
      "S1: 单向目标多余空目录已删除(one_way_delete)")

# S1c: 双向空目录合并
d = tempfile.mkdtemp()
src = os.path.join(d, "src")
dst = os.path.join(d, "dst")
os.makedirs(src)
os.makedirs(dst)
os.makedirs(os.path.join(src, "dira"))
os.makedirs(os.path.join(dst, "dirb"))
t = fresh_task(MODE_TWO_WAY, src, dst)
perform_sync(t)
check(os.path.isdir(os.path.join(dst, "dira")) and os.path.isdir(os.path.join(src, "dirb")),
      "S1: 双向空目录相互合并")

# SE1: 文件 vs 目录同名 -> type_conflict 明确失败（不收敛 -> 一次失败）
d = tempfile.mkdtemp()
src = os.path.join(d, "src")
dst = os.path.join(d, "dst")
os.makedirs(src)
os.makedirs(dst)
write(os.path.join(src, "x.txt"), "data")
os.makedirs(os.path.join(dst, "x.txt"))
t = fresh_task(MODE_ONE_WAY, src, dst)
res = perform_sync(t, dry_run=True)
check(any(a.kind == "type_conflict" for a in res["diff"].actions),
      "SE1: 文件/目录同名被识别为类型冲突(而非 copy)")
res = perform_sync(t)
check(res.get("fail_count") == 1 and t.last_status == "部分失败",
      "SE1: 类型冲突明确失败(计数 1, 不静默)")

# ---------- 12. 修复回归: include 过滤下空目录不被传播 ----------
print("[12] 回归: include 空目录")
d = tempfile.mkdtemp()
src = os.path.join(d, "src")
dst = os.path.join(d, "dst")
os.makedirs(src)
os.makedirs(dst)
os.makedirs(os.path.join(src, "empty_dir"))   # 空目录，无匹配文件
write(os.path.join(src, "keep.txt"), "k")
t = fresh_task(MODE_ONE_WAY, src, dst, include=["*.txt"])
perform_sync(t)
check(os.path.exists(os.path.join(dst, "keep.txt")), "include 下匹配文件正常同步")
check(not os.path.exists(os.path.join(dst, "empty_dir")),
      "include 下空目录不被传播(此前会被误同步)")

# ---------- 13. 修复回归: interval 补跑后 next_run 锚定触发时刻 ----------
print("[13] 回归: interval 锚定触发时刻")
d = tempfile.mkdtemp()
src = os.path.join(d, "src")
dst = os.path.join(d, "dst")
os.makedirs(src)
os.makedirs(dst)
store8 = TaskStore(os.path.join(d, "tasks.json"))
ti8 = fresh_task(MODE_ONE_WAY, src, dst)
ti8.schedule.enabled = True
ti8.schedule.type = "interval"
ti8.schedule.interval_minutes = 5
ti8.last_run = time.time() - 3600  # 停机错过 -> 触发时即补跑
store8.add(ti8)
sched8 = Scheduler(store8, lambda t: None)
sched8._poll_once()
check(ti8.next_run is not None and abs(ti8.next_run - (time.time() + 300)) < 10,
      "补跑触发后 next_run 锚定当前时刻+间隔(而非过期时刻导致立即再触发)")
sched8.stop()

# ---------- 14. 修复回归: run_now 拒绝禁用任务 ----------
print("[14] 回归: run_now 禁用拒绝")
d = tempfile.mkdtemp()
src = os.path.join(d, "src")
dst = os.path.join(d, "dst")
os.makedirs(src)
os.makedirs(dst)
store9 = TaskStore(os.path.join(d, "tasks.json"))
td9 = fresh_task(MODE_ONE_WAY, src, dst)
td9.enabled = False
store9.add(td9)
sched9 = Scheduler(store9, lambda t: None)
check(not sched9.run_now(td9.id), "run_now 拒绝已禁用任务(返回 False)")
sched9.stop()

# ---------- 15. 调度器: daily 到点自动触发 + 未到点不触发 ----------
print("[15] 调度: daily 触发链路")
import datetime
d = tempfile.mkdtemp()
src = os.path.join(d, "src")
dst = os.path.join(d, "dst")
os.makedirs(src)
os.makedirs(dst)
store10 = TaskStore(os.path.join(d, "tasks.json"))
td10 = fresh_task(MODE_ONE_WAY, src, dst)
td10.schedule.enabled = True
td10.schedule.type = "daily"
# 未来 2 分钟的时刻：确保本次 poll 不触发，next_run 指向该时刻
future = (datetime.datetime.now() + datetime.timedelta(minutes=2)).strftime("%H:%M")
td10.schedule.times = [future]
store10.add(td10)
sched10 = Scheduler(store10, lambda t: None)
sched10._poll_once()
check(td10.next_run is not None and td10.next_run > time.time(),
      "daily 未到点: next_run 指向未来时刻")
sched10.stop()

# ---------- 16. logger: 回调投递 + 超限轮转 ----------
print("[16] logger: 回调与轮转")
from logger import AppLogger
logdir = tempfile.mkdtemp()
lg = AppLogger(logdir, quiet=True)
got = []
lg.add_callback(lambda level, line: got.append((level, line)))
lg.info("hello-logger")
check(len(got) == 1 and got[0][0] == "INFO" and "hello-logger" in got[0][1],
      "logger 回调收到 INFO 日志")
lg.close()
# 超限轮转：写入超过阈值的行数后应生成 .1 备份
lg2 = AppLogger(logdir, quiet=True)
for i in range(3000):
    lg2.info("x" * 2000)   # 每行约 2KB，累计 > 2MB 触发轮转
lg2.close()
import glob as _glob
backups = _glob.glob(os.path.join(logdir, "foldersync.log.1"))
check(len(backups) == 1, "日志超限后轮转生成 .1 备份")
check(os.path.getsize(os.path.join(logdir, "foldersync.log")) < 2 * 1024 * 1024,
      "轮转后主日志文件回到阈值以内")

# ---------- 17. 每周调度: 计算 + 触发链路 ----------
print("[17] 调度: weekly 计算与触发")
from utils.timeutil import next_weekly_times
import datetime as _dt

# 确定性计算（基准日 2026-08-19 为周三）
base_wed = _dt.datetime(2026, 8, 19)
# 周三 09:00 之后：本周三 10:00 未过 -> 返回当天 10:00
from_e = _dt.datetime(2026, 8, 19, 9, 0, 0).timestamp()
nxt = next_weekly_times([3], ["10:00"], from_e)
check(nxt == _dt.datetime(2026, 8, 19, 10, 0, 0).timestamp(),
      "weekly 本周未过: 返回当天 10:00")
# 周三 11:00 之后：本周已过 -> 返回下周三 10:00
from_e2 = _dt.datetime(2026, 8, 19, 11, 0, 0).timestamp()
nxt2 = next_weekly_times([3], ["10:00"], from_e2)
check(nxt2 == _dt.datetime(2026, 8, 26, 10, 0, 0).timestamp(),
      "weekly 本周已过: 返回下周三 10:00")
# 多周几 × 多时刻：周三五 09:00/18:00，周三 08:00 -> 当天 09:00 最近
from_e3 = _dt.datetime(2026, 8, 19, 8, 0, 0).timestamp()
nxt3 = next_weekly_times([3, 5], ["09:00", "18:00"], from_e3)
check(nxt3 == _dt.datetime(2026, 8, 19, 9, 0, 0).timestamp(),
      "weekly 多周几多时刻: 取最近组合")
# 非法输入容错
check(next_weekly_times([], ["10:00"], time.time()) is None, "weekly 空周几 -> None")
check(next_weekly_times([0, 8], ["10:00"], time.time()) is None, "weekly 非法周几 -> None")
check(next_weekly_times([3], ["8点"], time.time()) is None, "weekly 非法时刻 -> None")

# 触发链路：未来 2 分钟不触发 + 到点触发
d = tempfile.mkdtemp()
src = os.path.join(d, "src")
dst = os.path.join(d, "dst")
os.makedirs(src)
os.makedirs(dst)
store11 = TaskStore(os.path.join(d, "tasks.json"))
tw11 = fresh_task(MODE_ONE_WAY, src, dst)
tw11.schedule.enabled = True
tw11.schedule.type = "weekly"
future = (_dt.datetime.now() + _dt.timedelta(minutes=2)).strftime("%H:%M")
tw11.schedule.times = [future]
tw11.schedule.weekdays = [1]  # 周一（未来 2 分钟跨日无碍，next_run 仍指向未来）
store11.add(tw11)
ran11 = []
sched11 = Scheduler(store11, lambda t: ran11.append(t.id))
sched11._poll_once()
check(tw11.next_run is not None and tw11.next_run > time.time(),
      "weekly 未到点: next_run 指向未来时刻")
check(tw11.id not in ran11, "weekly 未到点: 不触发")
# 模拟到点：把 next_run 置为过去，poll 应启动 worker
tw11.next_run = time.time() - 1
sched11._poll_once()
deadline = time.time() + 5
while time.time() < deadline and tw11.id not in ran11:
    time.sleep(0.05)
check(tw11.id in ran11, "weekly 到点: 自动触发")
sched11.stop()

# ---------- 18. config: baseline 迁移 + 损坏配置恢复 ----------
print("[18] config: baseline 迁移与损坏恢复")
d18 = tempfile.mkdtemp()
cfg18 = os.path.join(d18, "tasks.json")
t18 = Task()
t18.name = "迁移任务"
# 旧内嵌格式：tasks.json 中直接内嵌 baseline 字段
legacy18 = t18.to_dict()
legacy18["baseline"] = {"a.txt": {"size": 3, "mtime": 123.0, "hash": "abc"}}
with open(cfg18, "w", encoding="utf-8") as f:
    json.dump({"tasks": [legacy18]}, f, ensure_ascii=False)
store18 = TaskStore(cfg18)
t18b = store18.get(t18.id)
check(t18b is not None and t18b.baseline.get("a.txt", {}).get("hash") == "abc",
      "baseline 迁移: 内嵌 baseline 被加载")
bp18 = os.path.join(d18, "baseline", t18.id + ".json")
check(os.path.exists(bp18), "baseline 迁移: 独立 baseline 文件已生成")
with open(cfg18, "r", encoding="utf-8") as f:
    data18 = json.load(f)
check("baseline" not in data18["tasks"][0], "baseline 迁移: tasks.json 已剥离内嵌 baseline")
store18b = TaskStore(cfg18)
t18c = store18b.get(t18.id)
check(t18c is not None and t18c.baseline.get("a.txt", {}).get("hash") == "abc",
      "baseline 迁移: 重载后从独立文件恢复")

# 损坏配置：备份现场副本 + 从空配置启动
d18c = tempfile.mkdtemp()
cfg18c = os.path.join(d18c, "tasks.json")
with open(cfg18c, "w", encoding="utf-8") as f:
    f.write("{ 这不是合法JSON ")
store18c = TaskStore(cfg18c)
check(store18c.tasks == [], "损坏配置: 从空配置启动")
corrupts18 = [x for x in os.listdir(d18c) if x.startswith("tasks.json.corrupt-")]
check(len(corrupts18) == 1, "损坏配置: 生成 .corrupt-* 备份副本")

# ---------- 19. GUI 校验纯函数（无头可测） ----------
print("[19] GUI 校验: validate_schedule_input")
from config import validate_schedule_input, SCHED_DAILY, SCHED_WEEKLY, SCHED_INTERVAL

check(validate_schedule_input(True, SCHED_DAILY, "60", "08:00,20:00", "1,3") is None,
      "校验: 合法 daily 输入通过")
check(validate_schedule_input(True, SCHED_WEEKLY, "30", "09:00", "1,3,5") is None,
      "校验: 合法 weekly 输入通过")
check(validate_schedule_input(True, SCHED_INTERVAL, "0", "", "") is not None,
      "校验: interval <1 被拦截")
check(validate_schedule_input(True, SCHED_INTERVAL, "abc", "", "") is not None,
      "校验: interval 非整数被拦截")
check(validate_schedule_input(True, SCHED_DAILY, "60", "25:99", "1") is not None,
      "校验: 非法时刻被拦截")
check(validate_schedule_input(True, SCHED_DAILY, "60", "", "1") is not None,
      "校验: daily 启用缺时刻被拦截")
check(validate_schedule_input(True, SCHED_WEEKLY, "60", "09:00", "") is not None,
      "校验: weekly 启用缺周几被拦截")
check(validate_schedule_input(True, SCHED_WEEKLY, "60", "09:00", "0,8") is not None,
      "校验: 非法周几被拦截")
check(validate_schedule_input(False, SCHED_DAILY, "60", "", "") is None,
      "校验: 未启用定时不强制时刻")

# ---------- 20. utils/paths 路径工具 ----------
print("[20] utils/paths: app_dir / longpath / ensure_dir / join_rel")
from utils import paths as _paths

check(os.path.isdir(_paths.app_dir()), "app_dir: 返回存在的应用根目录")
check(_paths.is_longpath_supported() == (sys.platform == "win32"),
      "is_longpath_supported: 仅 Windows 为 True")
check(_paths.longpath("/tmp/abc") == "/tmp/abc" or "/tmp/abc".startswith("\\\\?\\"),
      "longpath: 非 Windows 原样返回(或 Windows 加前缀)")
check(_paths.longpath(None) is None, "longpath(None) 原样返回 None")
p20 = os.path.join(tempfile.mkdtemp(), "a", "b", "c")
_paths.ensure_dir(p20)
check(os.path.isdir(p20), "ensure_dir: 递归创建多级目录")
check(_paths.join_rel("/root", "x/y/z.txt") == os.path.join("/root", "x", "y", "z.txt"),
      "join_rel: '/' 相对路径拼回系统路径")

# ---------- 21. utils/timeutil 时间工具 ----------
print("[21] utils/timeutil: format_epoch / is_newer / _hms")
from utils.timeutil import format_epoch, is_newer, _hms, MTIME_TOLERANCE

check(format_epoch(None) == "-", "format_epoch(None) 返回 '-'")
check(format_epoch(0.0) != "-", "format_epoch(0) 返回格式化串")
check(format_epoch(0.0, "%Y") == "1970", "format_epoch: 自定义格式生效")
check(not is_newer(None, 100.0), "is_newer: ma=None -> False")
check(is_newer(100.0, None), "is_newer: mb=None -> ma 更新")
check(is_newer(100.0 + MTIME_TOLERANCE + 0.5, 100.0), "is_newer: 超出容差判更新")
check(not is_newer(100.0 + MTIME_TOLERANCE, 100.0), "is_newer: 恰在容差内不判更新")
check(_hms("08:30") == 30600, "_hms: 合法时刻换算秒数")
check(_hms("25:99") == 95940, "_hms: 数值越界不校验(按公式换算)")
check(_hms("abc") is None, "_hms: 非时刻格式返回 None")

# ---------- 22. scanner: 过滤 / 排除 / 取消 / 哈希 ----------
print("[22] scanner: include/exclude/self_paths/取消/hash_file")
from scanner import scan, hash_file, ScanCancelled

d22 = tempfile.mkdtemp()
s22 = os.path.join(d22, "src")
os.makedirs(os.path.join(s22, "sub"))
write(os.path.join(s22, "a.txt"), "aaa")
write(os.path.join(s22, "b.log"), "bbb")
write(os.path.join(s22, "sub", "c.txt"), "ccc")
write(os.path.join(s22, "sub", "d.tmp"), "ddd")
res22 = scan(s22, include=["*.txt"])
check("a.txt" in res22 and "b.log" not in res22 and "sub/c.txt" in res22,
      "scan: include 过滤仅保留匹配文件")
res22b = scan(s22, exclude=["*.tmp"])
check("sub/d.tmp" not in res22b and "a.txt" in res22b, "scan: exclude 过滤排除匹配文件")
res22c = scan(s22, self_paths=[os.path.join(s22, "sub")])
check("sub/c.txt" not in res22c, "scan: self_paths 排除指定目录")
# 保留名不参与扫描
write(os.path.join(s22, "keep.conflict-abc"), "x")
write(os.path.join(s22, "res.tmp~"), "x")
res22d = scan(s22)
check("keep.conflict-abc" not in res22d and "res.tmp~" not in res22d,
      "scan: .conflict-* 与 .tmp~ 保留名跳过")
# 取消事件
import threading as _thr
ev = _thr.Event()
ev.set()
try:
    scan(s22, cancel_event=ev)
    check(False, "scan: cancel 置位应抛 ScanCancelled")
except ScanCancelled:
    check(True, "scan: cancel 置位抛 ScanCancelled")
check(scan(os.path.join(d22, "nonexistent")) == {}, "scan: 目录不存在返回空 dict")
h1 = hash_file(os.path.join(s22, "a.txt"))
check(isinstance(h1, str) and len(h1) > 0, "hash_file: 返回哈希串")
check(hash_file(os.path.join(s22, "a.txt")) == h1, "hash_file: 同内容哈希稳定")
check(hash_file(os.path.join(d22, "nope.txt")) is None, "hash_file: 文件不存在返回 None")

# ---------- 23. config: Task 序列化往返 + TaskStore 增删改查 ----------
print("[23] config: Task 往返 / TaskStore update/remove/get")
from config import TaskStore as _TS, Schedule as _Sched

d23 = os.path.join(tempfile.mkdtemp(), "tasks.json")
store23 = _TS(d23)
t23 = fresh_task(MODE_ONE_WAY, "/src", "/dst")
t23.name = "往返测试"
t23.exclude = ["*.tmp"]
t23.schedule.type = "daily"
t23.schedule.times = ["08:00"]
back23 = Task.from_dict(t23.to_dict())
check(back23.name == t23.name and back23.source == t23.source, "Task: to_dict/from_dict 往返")
check(back23.exclude == ["*.tmp"], "Task: 往返保留 exclude")
check(back23.schedule.type == "daily" and back23.schedule.times == ["08:00"],
      "Task: 往返保留嵌套 Schedule")
check(Task.from_dict({}).id != "", "Task.from_dict: 空 dict 用默认值不崩")
store23.add(t23)
check(store23.get(t23.id) is t23, "TaskStore.get: 命中返回对象")
check(store23.get("no-such") is None, "TaskStore.get: 未命中返回 None")
# update: 同 id 替换
t23b = fresh_task(MODE_TWO_WAY, "/src2", "/dst2")
t23b.id = t23.id
store23.update(t23b)
check(store23.get(t23.id) is t23b and len(store23.tasks) == 1, "TaskStore.update: 同 id 替换")
# update_runtime: 只更新运行期字段
t23c = fresh_task(MODE_ONE_WAY, "/src3", "/dst3")
t23c.id = t23.id
t23c.last_status = "OK"
t23c.last_summary = "3 个文件"
store23.update_runtime(t23c)
cur23 = store23.get(t23.id)
check(cur23 is t23b and cur23.last_status == "OK" and cur23.last_summary == "3 个文件",
      "update_runtime: 不整对象覆盖, 只拷运行期字段")
# remove: 任务 + baseline 文件一并删除
store23.save_baseline(t23b)
bp23 = store23._baseline_path(t23.id)
check(os.path.exists(bp23), "save_baseline: baseline 文件已落盘")
store23.remove(t23.id)
check(store23.get(t23.id) is None and not os.path.exists(bp23),
      "TaskStore.remove: 任务移除且 baseline 清理")

# ---------- 24. logger: 级别方法 + close ----------
print("[24] logger: debug/info/warn/error 与 close")
from logger import AppLogger as _AL, LEVEL_DEBUG, LEVEL_INFO, LEVEL_WARN, LEVEL_ERROR

d24 = tempfile.mkdtemp()
lg = _AL(d24, quiet=True)
lg.debug("调试")
lg.info("信息")
lg.warn("警告")
lg.error("错误")
with open(os.path.join(d24, "foldersync.log"), encoding="utf-8") as f:
    lines24 = f.read()
check(LEVEL_DEBUG + " 调试" in lines24, "logger.debug 写入文件")
check(LEVEL_INFO + " 信息" in lines24, "logger.info 写入文件")
check(LEVEL_WARN + " 警告" in lines24, "logger.warn 写入文件")
check(LEVEL_ERROR + " 错误" in lines24, "logger.error 写入文件")
lg.close()
check(lg._file is None, "logger.close: 句柄置空")

# ---------- 25. GUI 对话框交互（mock 无头，不依赖 tkinter） ----------
print("[25] GUI: TaskDialog 保存/校验/取消（mock 无头）")
import sys as _sys
import types as _types

# 构造 fake tkinter 树注入 sys.modules，使 gui_task_dialog 可在无 tkinter 环境导入
class _FakeEntry(object):
    def __init__(self, text=""):
        self._text = text
    def get(self):
        return self._text
    def insert(self, *a, **k):
        pass
    def delete(self, *a, **k):
        pass

class _FakeVar(object):
    def __init__(self, value=False):
        self._value = value
    def get(self):
        return self._value
    def set(self, v):
        self._value = v

class _FakeCombo(object):
    def __init__(self, value=""):
        self._value = value
    def get(self):
        return self._value
    def set(self, v):
        self._value = v

class _FakeWidget(object):
    def __init__(self, *a, **k):
        pass
    def configure(self, *a, **k):
        pass
    def grid(self, *a, **k):
        pass
    def pack(self, *a, **k):
        pass
    def bind(self, *a, **k):
        pass

class _FakeToplevel(_FakeWidget):
    def __init__(self, *a, **k):
        self.destroyed = False
    def title(self, *a, **k):
        pass
    def geometry(self, *a, **k):
        pass
    def minsize(self, *a, **k):
        pass
    def transient(self, *a, **k):
        pass
    def grab_set(self):
        pass
    def protocol(self, *a, **k):
        pass
    def destroy(self):
        self.destroyed = True

_fake_tk = _types.ModuleType("tkinter")
_fake_tk.Toplevel = _FakeToplevel
_fake_tk.W = "w"
_fake_tk.END = "end"
_fake_tk.DISABLED = "disabled"
_fake_tk.NORMAL = "normal"
_fake_tk.BOTH = "both"
_fake_ttk = _types.ModuleType("tkinter.ttk")
for _n in ("Entry", "Combobox", "Checkbutton", "Spinbox", "Label", "Frame", "Button"):
    setattr(_fake_ttk, _n, _FakeWidget)
_fake_tk.ttk = _fake_ttk
_fake_fd = _types.ModuleType("tkinter.filedialog")
_fake_fd.askdirectory = lambda: "/tmp"
_fake_tk.filedialog = _fake_fd
_err_box_calls = []
def _fake_showerror(*a, **k):
    _err_box_calls.append(a)
_fake_mb = _types.ModuleType("tkinter.messagebox")
_fake_mb.showerror = _fake_showerror
_fake_tk.messagebox = _fake_mb
_sys.modules["tkinter"] = _fake_tk
_sys.modules["tkinter.ttk"] = _fake_ttk
_sys.modules["tkinter.filedialog"] = _fake_fd
_sys.modules["tkinter.messagebox"] = _fake_mb

from gui_task_dialog import TaskDialog as _TD
from gui_task_dialog import _MODE_LABELS as _ML, _SCHED_LABELS as _SL, _POLICY_LABELS as _PL
from config import SCHED_INTERVAL as _SI, SCHED_DAILY as _SD, CONFLICT_NEWER as _CN

d25 = tempfile.mkdtemp()
src25 = os.path.join(d25, "src")
dst25 = os.path.join(d25, "dst")
os.makedirs(src25)
os.makedirs(dst25)

def _mk_dialog(one_way=True):
    d = _TD.__new__(_TD)  # 绕过 __init__（真实控件构造），手动装配
    d.is_new = True
    d.task = None
    d.store = None
    d.result = None
    d._name = _FakeEntry("任务A")
    d._src = _FakeEntry(src25)
    d._dst = _FakeEntry(dst25)
    d._mode = _FakeCombo(_ML[MODE_ONE_WAY if one_way else MODE_TWO_WAY])
    d._ow_del = _FakeVar(True)
    d._tw_del = _FakeVar(False)
    d._enabled = _FakeVar(True)
    d._sched_on = _FakeVar(False)
    d._sched_type = _FakeCombo(_SL[_SI])
    d._interval = _FakeEntry("60")
    d._times = _FakeEntry("08:00")
    d._weekdays = _FakeEntry("1,3,5")
    d._include = _FakeEntry("")
    d._exclude = _FakeEntry("*.tmp")
    d._conflict = _FakeCombo(_PL[_CN])
    return d

# 校验拦截分支
_err_box_calls[:] = []
d = _mk_dialog()
d._name = _FakeEntry("   ")
d._on_save()
check(len(_err_box_calls) == 1 and d.result is None, "保存: 空名称被拦截(弹窗且不保存)")

_err_box_calls[:] = []
d = _mk_dialog()
d._src = _FakeEntry("")
d._on_save()
check(len(_err_box_calls) == 1 and d.result is None, "保存: 缺源目录被拦截")

_err_box_calls[:] = []
d = _mk_dialog()
d._src = _FakeEntry(os.path.join(d25, "nonexistent"))
d._on_save()
check(len(_err_box_calls) == 1 and d.result is None, "保存: 源目录不存在被拦截")

_err_box_calls[:] = []
d = _mk_dialog()
d._src = _FakeEntry(src25)
d._dst = _FakeEntry(src25)
d._on_save()
check(len(_err_box_calls) == 1 and d.result is None, "保存: 源=目标被拦截")

_err_box_calls[:] = []
d = _mk_dialog()
d._dst = _FakeEntry(os.path.join(src25, "sub"))
d._on_save()
check(len(_err_box_calls) == 1 and d.result is None, "保存: 互为子目录被拦截")

_err_box_calls[:] = []
d = _mk_dialog()
d._sched_on = _FakeVar(True)
d._sched_type = _FakeCombo(_SL[_SD])
d._times = _FakeEntry("25:99")
d._on_save()
check(len(_err_box_calls) == 1 and d.result is None, "保存: 非法调度时刻被拦截")

# 合法保存：字段正确（含反向映射修复）
_err_box_calls[:] = []
d = _mk_dialog()
d._on_save()
check(d.result is not None and _err_box_calls == [], "保存: 合法输入生成 Task")
check(d.result.name == "任务A", "保存: name 正确")
check(d.result.source == os.path.abspath(src25) and d.result.target == os.path.abspath(dst25),
      "保存: 路径转为绝对路径")
check(d.result.mode == MODE_ONE_WAY, "保存: mode 存内部值(非中文标签)")
check(d.result.schedule.type == _SI and d.result.schedule.interval_minutes == 60,
      "保存: 调度类型/间隔正确")
check(d.result.conflict_policy == _CN, "保存: 冲突策略存内部值")
check(d.result.exclude == ["*.tmp"], "保存: 排除规则解析为列表")
check(d.result.one_way_delete is True and d.result.two_way_delete is False,
      "保存: 单向删除勾选映射")

# 双向模式：two_way_delete 生效、one_way_delete 强制关闭
d = _mk_dialog(one_way=False)
d._tw_del = _FakeVar(True)
d._on_save()
check(d.result.mode == MODE_TWO_WAY, "保存: 双向 mode 正确")
check(d.result.two_way_delete is True and d.result.one_way_delete is False,
      "保存: 双向传播删除勾选映射")

# 编辑模式：复用既有 task 对象
t25 = Task()
d = _mk_dialog()
d.is_new = False
d.task = t25
d._on_save()
check(d.result is t25, "保存: 编辑模式复用原 Task 对象")

# 取消
d = _mk_dialog()
d._on_cancel()
check(d.result is None and d.destroyed, "取消: result 置 None 且关闭对话框")

# ---------- 26. 审查修复: run_now 重置 next_run（停机错过场景不重复触发） ----------
print("[26] 调度: run_now 重置 next_run")
d26 = tempfile.mkdtemp()
src26 = os.path.join(d26, "src")
dst26 = os.path.join(d26, "dst")
os.makedirs(src26)
os.makedirs(dst26)
write(os.path.join(src26, "a.txt"), "x")
store26 = TaskStore(os.path.join(d26, "tasks.json"))
tk26 = fresh_task(MODE_ONE_WAY, src26, dst26)
tk26.schedule.enabled = True
tk26.schedule.type = "interval"
tk26.schedule.interval_minutes = 60
store26.add(tk26)
tk26.next_run = time.time() - 3600  # 模拟停机错过周期：next_run 已过期
ran26 = []


def _sched_run26(task):
    # type: (Task) -> None
    ran26.append(task.id)


sched26 = Scheduler(store26, _sched_run26)
ok26 = sched26.run_now(tk26.id)
check(ok26, "run_now: 手动触发成功")
# 修复点：run_now 成功后 next_run 必须重置，否则 _poll_once 会因过期值立即再触发
check(tk26.next_run is None, "run_now: next_run 已重置(防立即重复触发)")
# 运行期间 _poll_once 不应重复触发（任务在运行槽中）
sched26._poll_once()
check(len(ran26) == 1, "run_now: 运行期间 _poll_once 不重复触发")
# 等待手动线程结束，确认总触发次数仍为 1
deadline26 = time.time() + 10
while len(ran26) < 1 and time.time() < deadline26:
    time.sleep(0.1)
time.sleep(0.3)  # 给可能的错误重复触发留出窗口
check(len(ran26) == 1, "run_now: 任务完成后未再次自动触发(总计 1 次)")
sched26.stop()

# ---------- 清理 ----------
print("\n结果：%s" % ("全部通过" if not failures else "%d 项失败" % len(failures)))
if failures:
    for f in failures:
        print("  - %s" % f)
    sys.exit(1)
print("自测通过。")
