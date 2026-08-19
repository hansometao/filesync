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
    if cond:
        print("  OK  - %s" % msg)
    else:
        print("  FAIL- %s" % msg)
        failures.append(msg)


def write(path, content, mtime=None):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def fresh_task(mode, src, dst, **kw):
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
    # type: (list, str) -> object
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

# ---------- 清理 ----------
print("\n结果：%s" % ("全部通过" if not failures else "%d 项失败" % len(failures)))
if failures:
    for f in failures:
        print("  - %s" % f)
    sys.exit(1)
print("自测通过。")
