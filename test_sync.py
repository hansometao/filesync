# mypy: ignore-errors
# 测试脚本不做脚本级类型检查：含大量 fake tkinter 类与动态装配，无类型价值
"""无界面自测：覆盖 diff / apply / 冲突 / 调度器核心逻辑。

运行：python test_sync.py
（不依赖 tkinter，可在任意 3.8+ 环境执行）

结构：每个编号节封装为独立 test_N() 函数——
- 失败定位到节：单节断言失败或异常不影响其余节的执行与报告；
- pytest 兼容：test_* 函数可被 pytest 收集（逐节隔离报告），
  函数内末尾断言保证 check() 记录的失败会标记对应用例；
- 作用域：节顶层的赋值/import 经 `global` 声明发布，保持与原
  扁平脚本一致的全局语义（跨节共享 TaskStore、fake-tkinter 注入等）。
节内顶层 def/class 提升到文件尾部的助手区（调用发生在运行期）。
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

def test_1():
    """自测节 1. 单向镜像：新增 / 修改 / 删除"""
    global d, dst, f, res, res4, src, t, t2, t3, t4
    _fail_base = len(failures)
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
    assert len(failures) == _fail_base, "test_1: 本节有断言失败"

# ---------- 2. 双向同步：合并 + 冲突 ----------

def test_2():
    """自测节 2. 双向同步：合并 + 冲突"""
    global backups, ca, cb, d, dst, f, src, t, t2, t3, x
    _fail_base = len(failures)
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
    assert len(failures) == _fail_base, "test_2: 本节有断言失败"

# ---------- 3. 调度器：run_now 触发 ----------

def test_3_run_now():
    """自测节 3. 调度器：run_now 触发"""
    global TaskStore, d, deadline, dst, ok, ran, sched, src, store, threading, tk
    _fail_base = len(failures)
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




    sched = Scheduler(store, _sched_run)
    ok = sched.run_now(tk.id)
    check(ok, "run_now 成功入队")
    deadline = time.time() + 10
    while tk.id not in ran and time.time() < deadline:
        time.sleep(0.2)
    check(tk.id in ran, "调度器在后台线程执行了任务")
    check(os.path.exists(os.path.join(dst, "x.txt")), "调度执行后文件已同步")
    sched.stop()
    assert len(failures) == _fail_base, "test_3_run_now: 本节有断言失败"

# ---------- 2b. 回归 D：双向 mtime 微差(<2s) 的小修改不应漏判 ----------

def test_2b_D_mtime():
    """自测节 2b. 回归 D：双向 mtime 微差(<2s) 的小修改不应漏判"""
    global a, base_mtime, d, dst, f, res, src, t
    _fail_base = len(failures)
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
    assert len(failures) == _fail_base, "test_2b_D_mtime: 本节有断言失败"

# ---------- 3b. H2: 非法每日时刻容错 ----------

def test_3b():
    """自测节 3b. H2: 非法每日时刻容错"""
    global next_daily_times
    _fail_base = len(failures)
    print("[3b] H2: 非法每日时刻")
    from utils.timeutil import next_daily_times
    check(next_daily_times(["8点", "晚8"], time.time()) is None, "时刻全非法 -> 返回 None(调度线程不崩)")
    check(next_daily_times(["08:00"], time.time()) is not None, "合法时刻正常返回")
    assert len(failures) == _fail_base, "test_3b: 本节有断言失败"

# ---------- 3c. 调度: interval 锚定 last_run 自动触发 + 运行中顺延(M9) ----------

def test_3c_interval_last_run():
    """自测节 3c. 调度: interval 锚定 last_run 自动触发 + 运行中顺延(M9)"""
    global d, deadline, dst, finish_evt, sched2, src, started_evt, store2, ti
    _fail_base = len(failures)
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
    assert len(failures) == _fail_base, "test_3c_interval_last_run: 本节有断言失败"

# ---------- 4. H3: 损坏配置保留现场副本 ----------

def test_4():
    """自测节 4. H3: 损坏配置保留现场副本"""
    global cfg, corrupts, d, f, store3, x
    _fail_base = len(failures)
    print("[4] H3: 损坏配置")
    d = tempfile.mkdtemp()
    cfg = os.path.join(d, "tasks.json")
    with open(cfg, "w", encoding="utf-8") as f:
        f.write("{broken json!!!")
    store3 = TaskStore(cfg)
    check(store3.tasks == [], "损坏配置不崩溃，任务列表为空")
    corrupts = [x for x in os.listdir(d) if x.startswith("tasks.json.corrupt-")]
    check(len(corrupts) == 1, "损坏文件已备份为 .corrupt- 副本(可手工找回)")
    assert len(failures) == _fail_base, "test_4: 本节有断言失败"

# ---------- 5. H4: 并发 save 不损坏 ----------

def test_5_save():
    """自测节 5. H4: 并发 save 不损坏"""
    global _, f, ok, store4, t, ths
    _fail_base = len(failures)
    print("[5] H4: 并发 save")
    store4 = TaskStore(os.path.join(tempfile.mkdtemp(), "tasks.json"))
    store4.add(fresh_task(MODE_ONE_WAY, "/tmp/a1", "/tmp/b1"))




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
    assert len(failures) == _fail_base, "test_5_save: 本节有断言失败"

# ---------- 6. M4: 冲突备份不被同步传播 ----------

def test_6_M4():
    """自测节 6. M4: 冲突备份不被同步传播"""
    global T, backups_in_src, d, dst, res, src, t, x
    _fail_base = len(failures)
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
    assert len(failures) == _fail_base, "test_6_M4: 本节有断言失败"

# ---------- 7. M8: 单向 <2s 同尺寸修改被检出 ----------

def test_7_2s():
    """自测节 7. M8: 单向 <2s 同尺寸修改被检出"""
    global T, a, d, dst, f, res, src, t
    _fail_base = len(failures)
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
    assert len(failures) == _fail_base, "test_7_2s: 本节有断言失败"

# ---------- 8. M10: baseline 分离持久化与旧格式迁移 ----------

def test_8_M10_baseline():
    """自测节 8. M10: baseline 分离持久化与旧格式迁移"""
    global bl_files, cfg, d, f, old, raw, store5, store6, store7, t5, t6, t7
    _fail_base = len(failures)
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
    assert len(failures) == _fail_base, "test_8_M10_baseline: 本节有断言失败"

# ---------- 9. L4: 部分失败计入状态 ----------

def test_9_L4():
    """自测节 9. L4: 部分失败计入状态"""
    global d, dst, res, src, t
    _fail_base = len(failures)
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
    assert len(failures) == _fail_base, "test_9_L4: 本节有断言失败"

# ---------- 10. CLI 无头入口(run_cli) ----------

def test_10_CLI_run_cli():
    """自测节 10. CLI 无头入口(run_cli)"""
    global _orig, cfg, contextlib, d, d_empty, dst, io, out, rc, src, store_cli, tc
    global td
    _fail_base = len(failures)
    print("[10] CLI 无头入口")
    import io
    import contextlib




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




    _sync_engine.perform_sync = _boom
    try:
        rc, out = _cli(["--sync", "cli任务"], d)
        check(rc == 3 and "同步失败" in out, "--sync 异常路径(退出码 3, 不抛 traceback)")
    finally:
        _sync_engine.perform_sync = _orig
    assert len(failures) == _fail_base, "test_10_CLI_run_cli: 本节有断言失败"

# ---------- 11. S1 空目录同步 / SE1 类型冲突 ----------

def test_11_S1_SE1():
    """自测节 11. S1 空目录同步 / SE1 类型冲突"""
    global a, d, dst, res, src, t
    _fail_base = len(failures)
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
    assert len(failures) == _fail_base, "test_11_S1_SE1: 本节有断言失败"

# ---------- 12. 修复回归: include 过滤下空目录不被传播 ----------

def test_12_include():
    """自测节 12. 修复回归: include 过滤下空目录不被传播"""
    global d, dst, src, t
    _fail_base = len(failures)
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
    assert len(failures) == _fail_base, "test_12_include: 本节有断言失败"

# ---------- 13. 修复回归: interval 补跑后 next_run 锚定触发时刻 ----------

def test_13_interval_next_run():
    """自测节 13. 修复回归: interval 补跑后 next_run 锚定触发时刻"""
    global d, dst, sched8, src, store8, ti8
    _fail_base = len(failures)
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
    assert len(failures) == _fail_base, "test_13_interval_next_run: 本节有断言失败"

# ---------- 14. 修复回归: run_now 拒绝禁用任务 ----------

def test_14_run_now():
    """自测节 14. 修复回归: run_now 拒绝禁用任务"""
    global d, dst, sched9, src, store9, td9
    _fail_base = len(failures)
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
    assert len(failures) == _fail_base, "test_14_run_now: 本节有断言失败"

# ---------- 15. 调度器: daily 到点自动触发 + 未到点不触发 ----------

def test_15_daily():
    """自测节 15. 调度器: daily 到点自动触发 + 未到点不触发"""
    global d, datetime, dst, future, sched10, src, store10, td10
    _fail_base = len(failures)
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
    assert len(failures) == _fail_base, "test_15_daily: 本节有断言失败"

# ---------- 16. logger: 回调投递 + 超限轮转 ----------

def test_16_logger():
    """自测节 16. logger: 回调投递 + 超限轮转"""
    global AppLogger, _glob, backups, got, i, lg, lg2, logdir
    _fail_base = len(failures)
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
    assert len(failures) == _fail_base, "test_16_logger: 本节有断言失败"

# ---------- 17. 每周调度: 计算 + 触发链路 ----------

def test_17():
    """自测节 17. 每周调度: 计算 + 触发链路"""
    global _dt, base_wed, d, deadline, dst, from_e, from_e2, from_e3, future, next_weekly_times, nxt, nxt2
    global nxt3, ran11, sched11, src, store11, tw11
    _fail_base = len(failures)
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
    assert len(failures) == _fail_base, "test_17: 本节有断言失败"

# ---------- 18. config: baseline 迁移 + 损坏配置恢复 ----------

def test_18_config_baseline():
    """自测节 18. config: baseline 迁移 + 损坏配置恢复"""
    global bp18, cfg18, cfg18c, corrupts18, d18, d18c, data18, f, legacy18, store18, store18b, store18c
    global t18, t18b, t18c, x
    _fail_base = len(failures)
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
    assert len(failures) == _fail_base, "test_18_config_baseline: 本节有断言失败"

# ---------- 19. GUI 校验纯函数（无头可测） ----------

def test_19_GUI():
    """自测节 19. GUI 校验纯函数（无头可测）"""
    global SCHED_DAILY, SCHED_INTERVAL, SCHED_WEEKLY, validate_schedule_input
    _fail_base = len(failures)
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
    assert len(failures) == _fail_base, "test_19_GUI: 本节有断言失败"

# ---------- 20. utils/paths 路径工具 ----------

def test_20_utils_paths():
    """自测节 20. utils/paths 路径工具"""
    global _lp20, _paths, p20
    _fail_base = len(failures)
    print("[20] utils/paths: app_dir / longpath / ensure_dir / join_rel")
    from utils import paths as _paths

    check(os.path.isdir(_paths.app_dir()), "app_dir: 返回存在的应用根目录")
    check(_paths.is_longpath_supported() == (sys.platform == "win32"),
          "is_longpath_supported: 仅 Windows 为 True")
    _lp20 = _paths.longpath("/tmp/abc")
    check(_lp20 == "/tmp/abc" or _lp20.startswith("\\\\?\\"),
          "longpath: 非 Windows 原样返回(或 Windows 加前缀)")
    check(_paths.longpath(None) is None, "longpath(None) 原样返回 None")
    p20 = os.path.join(tempfile.mkdtemp(), "a", "b", "c")
    _paths.ensure_dir(p20)
    check(os.path.isdir(p20), "ensure_dir: 递归创建多级目录")
    check(_paths.join_rel("/root", "x/y/z.txt") == os.path.join("/root", "x", "y", "z.txt"),
          "join_rel: '/' 相对路径拼回系统路径")
    assert len(failures) == _fail_base, "test_20_utils_paths: 本节有断言失败"

# ---------- 21. utils/timeutil 时间工具 ----------

def test_21_utils_timeutil():
    """自测节 21. utils/timeutil 时间工具"""
    global MTIME_TOLERANCE, _hms, format_epoch, is_newer
    _fail_base = len(failures)
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
    check(_hms("25:99") is None, "_hms: 越界时刻返回 None(越界按非法)")
    check(_hms("-1:00") is None, "_hms: 负数小时返回 None")
    check(_hms("08:60") is None, "_hms: 越界分钟返回 None")
    check(_hms("23:59") == 86340, "_hms: 边界合法时刻(23:59)")
    check(_hms("00:00") == 0, "_hms: 边界合法时刻(00:00)")
    check(_hms("abc") is None, "_hms: 非时刻格式返回 None")
    assert len(failures) == _fail_base, "test_21_utils_timeutil: 本节有断言失败"

# ---------- 22. scanner: 过滤 / 排除 / 取消 / 哈希 ----------

def test_22_scanner():
    """自测节 22. scanner: 过滤 / 排除 / 取消 / 哈希"""
    global ScanCancelled, _thr, d22, ev, h1, hash_file, res22, res22b, res22c, res22d, s22, scan
    _fail_base = len(failures)
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
    # 保留名不参与扫描（冲突备份用精确时间戳格式匹配；非时间戳的
    # "xxx.conflict-abc" 是真实用户文件，不再被宽泛的 *.conflict-* 误排）
    write(os.path.join(s22, "keep.conflict-20260821-120000.123"), "x")
    write(os.path.join(s22, "keep.conflict-20260821-120000.123-2"), "x")
    write(os.path.join(s22, "user.conflict-final.docx"), "x")
    write(os.path.join(s22, "res.tmp~"), "x")
    res22d = scan(s22)
    check("keep.conflict-20260821-120000.123" not in res22d
          and "keep.conflict-20260821-120000.123-2" not in res22d
          and "res.tmp~" not in res22d,
          "scan: .conflict-<时间戳> 与 .tmp~ 保留名跳过")
    check("user.conflict-final.docx" in res22d,
          "scan: 非时间戳 .conflict- 文件名不被误排(真实用户文件)")
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
    assert len(failures) == _fail_base, "test_22_scanner: 本节有断言失败"

# ---------- 23. config: Task 序列化往返 + TaskStore 增删改查 ----------

def test_23_config_Task():
    """自测节 23. config: Task 序列化往返 + TaskStore 增删改查"""
    global _Sched, _TS, back23, bp23, cur23, d23, store23, t23, t23b, t23c
    _fail_base = len(failures)
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
    assert len(failures) == _fail_base, "test_23_config_Task: 本节有断言失败"

# ---------- 24. logger: 级别方法 + close ----------

def test_24_logger_close():
    """自测节 24. logger: 级别方法 + close"""
    global LEVEL_DEBUG, LEVEL_ERROR, LEVEL_INFO, LEVEL_WARN, _AL, d24, f, lg, lines24
    _fail_base = len(failures)
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
    assert len(failures) == _fail_base, "test_24_logger_close: 本节有断言失败"

# ---------- 25. GUI 对话框交互（mock 无头，不依赖 tkinter） ----------

def test_25_GUI_mock():
    """自测节 25. GUI 对话框交互（mock 无头，不依赖 tkinter）"""
    global _CN, _ML, _PL, _SD, _SI, _SL, _TD, _ask_calls, _err_box_calls, _fake_fd, _fake_mb, _fake_tk
    global _fake_ttk, _n, _sys, _types, d, d25, dst25, src25, t25
    _fail_base = len(failures)
    print("[25] GUI: TaskDialog 保存/校验/取消（mock 无头）")
    import sys as _sys
    import types as _types

    # 构造 fake tkinter 树注入 sys.modules，使 gui_task_dialog 可在无 tkinter 环境导入





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
    _ask_calls = []
    _fake_mb = _types.ModuleType("tkinter.messagebox")
    _fake_mb.showerror = _fake_showerror
    _fake_mb.askyesno = _fake_askyesno
    _fake_tk.messagebox = _fake_mb
    _sys.modules["tkinter"] = _fake_tk
    _sys.modules["tkinter.ttk"] = _fake_ttk
    _sys.modules["tkinter.filedialog"] = _fake_fd
    _sys.modules["tkinter.messagebox"] = _fake_mb

    from gui_task_dialog import TaskDialog as _TD
    from gui_task_dialog import _MODE_LABELS as _ML, _SCHED_LABELS as _SL, POLICY_LABELS as _PL
    from config import SCHED_INTERVAL as _SI, SCHED_DAILY as _SD, CONFLICT_NEWER as _CN

    d25 = tempfile.mkdtemp()
    src25 = os.path.join(d25, "src")
    dst25 = os.path.join(d25, "dst")
    os.makedirs(src25)
    os.makedirs(dst25)


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
    assert len(failures) == _fail_base, "test_25_GUI_mock: 本节有断言失败"

# ---------- 26. 审查修复: run_now 重置 next_run（停机错过场景不重复触发） ----------

def test_26_run_now_next_run():
    """自测节 26. 审查修复: run_now 重置 next_run（停机错过场景不重复触发）"""
    global d26, deadline26, dst26, ok26, ran26, sched26, src26, store26, tk26
    _fail_base = len(failures)
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
    assert len(failures) == _fail_base, "test_26_run_now_next_run: 本节有断言失败"

# ---------- 27. F1: 冲突处理失败计入 fail_count + 备份失败不覆盖 ----------

def test_27_fail_count():
    """自测节 27. F1: 冲突处理失败计入 fail_count + 备份失败不覆盖"""
    global _orig_copy2, _orig_docopy, backups27, d, dst, f, res, res2, src, t, t2, x
    _fail_base = len(failures)
    print("[27] F1: 冲突失败计数与备份安全")
    d = tempfile.mkdtemp()
    src = os.path.join(d, "src")
    dst = os.path.join(d, "dst")
    os.makedirs(src)
    os.makedirs(dst)
    write(os.path.join(src, "c.txt"), "src-version", mtime=1000.0)
    write(os.path.join(dst, "c.txt"), "dst-version", mtime=2000.0)

    # 备份失败 -> 不覆盖落败方 + 计为失败（不静默丢数据）
    t = fresh_task(MODE_TWO_WAY, src, dst, conflict_policy=CONFLICT_SOURCE)
    _orig_copy2 = _sync_engine.shutil.copy2




    _sync_engine.shutil.copy2 = _boom_copy2
    try:
        res = perform_sync(t)
    finally:
        _sync_engine.shutil.copy2 = _orig_copy2
    check(res.get("fail_count") == 1, "F1: 冲突备份失败计为失败(fail_count=1)")
    check(t.last_status == "部分失败", "F1: 冲突失败不误报'成功'")
    with open(os.path.join(src, "c.txt"), encoding="utf-8") as f:
        check(f.read() == "src-version", "F1: 备份失败时胜者未覆盖(保持原状)")
    with open(os.path.join(dst, "c.txt"), encoding="utf-8") as f:
        check(f.read() == "dst-version", "F1: 备份失败时落败方数据未丢")

    # 覆盖失败 -> 同样计为失败，但备份已先行完成
    d = tempfile.mkdtemp()
    src = os.path.join(d, "src")
    dst = os.path.join(d, "dst")
    os.makedirs(src)
    os.makedirs(dst)
    write(os.path.join(src, "c2.txt"), "src-version", mtime=1000.0)
    write(os.path.join(dst, "c2.txt"), "dst-version", mtime=2000.0)
    t2 = fresh_task(MODE_TWO_WAY, src, dst, conflict_policy=CONFLICT_SOURCE)
    _orig_docopy = _sync_engine._do_copy




    _sync_engine._do_copy = _boom_docopy
    try:
        res2 = perform_sync(t2)
    finally:
        _sync_engine._do_copy = _orig_docopy
    check(res2.get("fail_count") == 1 and t2.last_status == "部分失败",
          "F1: 冲突覆盖失败计为失败(此前状态误报'成功')")
    backups27 = [x for x in os.listdir(dst) if x.startswith("c2.txt.conflict-")]
    check(len(backups27) == 1, "F1: 覆盖失败前落败方已先行备份")
    assert len(failures) == _fail_base, "test_27_fail_count: 本节有断言失败"

# ---------- 28. F2: mkdir/type_conflict 计数与摘要不漏报 ----------

def test_28_mkdir_type_conflict():
    """自测节 28. F2: mkdir/type_conflict 计数与摘要不漏报"""
    global d, dst, res, res2, src, t, t2
    _fail_base = len(failures)
    print("[28] F2: 动作计数与摘要")
    d = tempfile.mkdtemp()
    src = os.path.join(d, "src")
    dst = os.path.join(d, "dst")
    os.makedirs(src)
    os.makedirs(dst)
    os.makedirs(os.path.join(src, "empty1"))
    os.makedirs(os.path.join(src, "empty2"))
    t = fresh_task(MODE_ONE_WAY, src, dst)
    res = perform_sync(t, dry_run=True)
    check(res["diff"].mkdir_count == 2, "F2: mkdir 动作计入计数")
    check("创建目录 2" in res["diff"].summary(),
          "F2: 目录创建在摘要可见(此前显示全 0 漏报)")

    d = tempfile.mkdtemp()
    src = os.path.join(d, "src")
    dst = os.path.join(d, "dst")
    os.makedirs(src)
    os.makedirs(dst)
    write(os.path.join(src, "x.txt"), "data")
    os.makedirs(os.path.join(dst, "x.txt"))
    t2 = fresh_task(MODE_ONE_WAY, src, dst)
    res2 = perform_sync(t2, dry_run=True)
    check(res2["diff"].type_conflict_count == 1, "F2: type_conflict 计入计数")
    check("类型冲突 1" in res2["diff"].summary(), "F2: 类型冲突在摘要可见")
    assert len(failures) == _fail_base, "test_28_mkdir_type_conflict: 本节有断言失败"

# ---------- 29. F4: interval 校验仅限 interval 类型 + 解析兜底 ----------

def test_29_F4_interval():
    """自测节 29. F4: interval 校验仅限 interval 类型 + 解析兜底"""
    global d
    _fail_base = len(failures)
    print("[29] F4: 校验放宽与解析兜底")
    check(validate_schedule_input(True, SCHED_DAILY, "abc", "08:00", "1") is None,
          "校验: daily 下非法间隔不再拦截(间隔与该类型无关)")
    check(validate_schedule_input(True, SCHED_WEEKLY, "", "09:00", "1,3") is None,
          "校验: weekly 下空间隔不再拦截")
    check(validate_schedule_input(True, SCHED_INTERVAL, "abc", "", "") is not None,
          "校验: interval 下非法间隔仍被拦截")

    _err_box_calls[:] = []
    d = _mk_dialog()
    d._sched_on = _FakeVar(True)
    d._sched_type = _FakeCombo(_SL[_SD])
    d._interval = _FakeEntry("abc")
    d._on_save()
    check(d.result is not None and _err_box_calls == [],
          "保存: daily + 非法间隔不弹错且成功保存")
    check(d.result.schedule.interval_minutes == 60,
          "保存: 非法间隔回退默认 60(解析不崩溃)")
    assert len(failures) == _fail_base, "test_29_F4_interval: 本节有断言失败"

# ---------- 30. F5: daily/weekly 停机错过补跑 ----------

def test_30_F5_daily():
    """自测节 30. F5: daily/weekly 停机错过补跑"""
    global d, deadline30, deadline30c, dst, from_e30, from_e30b, past_t, prev_daily_time, prev_weekly_time, ran30, ran30b, ran30c
    global sched30, sched30b, sched30c, src, store30, store30b, store30c, td30, td30b, td30c
    _fail_base = len(failures)
    print("[30] F5: daily/weekly 停机补跑")
    from utils.timeutil import prev_daily_time, prev_weekly_time

    # 纯函数：确定性断言（基准日 2026-08-19 为周三，本地时区自洽）
    from_e30 = _dt.datetime(2026, 8, 19, 9, 30).timestamp()
    check(prev_daily_time(["08:00"], from_e30) == _dt.datetime(2026, 8, 19, 8, 0).timestamp(),
          "prev_daily: 今天已过时刻")
    check(prev_daily_time(["10:00"], from_e30) == _dt.datetime(2026, 8, 18, 10, 0).timestamp(),
          "prev_daily: 今天未到取昨天")
    check(prev_daily_time(["8点"], from_e30) is None, "prev_daily: 全非法 -> None")
    check(prev_weekly_time([3], ["10:00"], from_e30) == _dt.datetime(2026, 8, 12, 10, 0).timestamp(),
          "prev_weekly: 本周已过取上周三")
    from_e30b = _dt.datetime(2026, 8, 19, 10, 30).timestamp()
    check(prev_weekly_time([3], ["10:00"], from_e30b) == _dt.datetime(2026, 8, 19, 10, 0).timestamp(),
          "prev_weekly: 今天已过取今天")

    # 调度链路：错过 daily 时刻(last_run 更早) -> 启动补跑一次


    d = tempfile.mkdtemp()
    src = os.path.join(d, "src")
    dst = os.path.join(d, "dst")
    os.makedirs(src)
    os.makedirs(dst)
    past_t = time.strftime("%H:%M", time.localtime(time.time() - 300))  # 5 分钟前已过的时刻
    store30 = TaskStore(os.path.join(d, "tasks.json"))
    td30 = _mk_sched_task(SCHED_DAILY, [past_t], last_run=time.time() - 7200)
    store30.add(td30)
    ran30 = []
    sched30 = Scheduler(store30, lambda t: ran30.append(t.id))
    sched30._poll_once()
    deadline30 = time.time() + 5
    while time.time() < deadline30 and td30.id not in ran30:
        time.sleep(0.05)
    check(td30.id in ran30, "F5: daily 停机错过的时刻启动后补跑一次")
    check(td30.next_run is None or td30.next_run > time.time(),
          "F5: 补跑后 next_run 指向未来(不连续重复触发)")
    sched30.stop()

    # 今天已跑过(last_run 晚于已过时刻) -> 不补跑
    store30b = TaskStore(os.path.join(d, "tasks.json2"))
    td30b = _mk_sched_task(SCHED_DAILY, [past_t], last_run=time.time() - 30)
    store30b.add(td30b)
    ran30b = []
    sched30b = Scheduler(store30b, lambda t: ran30b.append(t.id))
    sched30b._poll_once()
    check(td30b.id not in ran30b and td30b.next_run is not None and td30b.next_run > time.time(),
          "F5: 今天已运行过不重复补跑")
    sched30b.stop()

    # weekly 错过补跑：选中今天与昨天（防跨日周几翻转）、时刻已过、last_run 更早
    store30c = TaskStore(os.path.join(d, "tasks.json3"))
    td30c = _mk_sched_task(SCHED_WEEKLY, [past_t],
                           weekdays=[time.localtime().tm_wday + 1,
                                     time.localtime(time.time() - 86400).tm_wday + 1],
                           last_run=time.time() - 7200)
    store30c.add(td30c)
    ran30c = []
    sched30c = Scheduler(store30c, lambda t: ran30c.append(t.id))
    sched30c._poll_once()
    deadline30c = time.time() + 5
    while time.time() < deadline30c and td30c.id not in ran30c:
        time.sleep(0.05)
    check(td30c.id in ran30c, "F5: weekly 停机错过的时刻启动后补跑一次")
    sched30c.stop()
    assert len(failures) == _fail_base, "test_30_F5_daily: 本节有断言失败"

# ---------- 31. F6: baseline 取执行后真实状态（dirty 路径直接 stat） ----------

def test_31_F6_baseline():
    """自测节 31. F6: baseline 取执行后真实状态（dirty 路径直接 stat）"""
    global T31, bl31, d, dst, res31, src, st31, t31
    _fail_base = len(failures)
    print("[31] F6: baseline 状态新鲜度")
    d = tempfile.mkdtemp()
    src = os.path.join(d, "src")
    dst = os.path.join(d, "dst")
    os.makedirs(src)
    os.makedirs(dst)
    T31 = 1700000200.0
    write(os.path.join(src, "y.txt"), "v1", mtime=T31)
    write(os.path.join(src, "seed.txt"), "seed", mtime=T31)
    t31 = fresh_task(MODE_TWO_WAY, src, dst)
    perform_sync(t31)   # 建立 baseline（y.txt 复制到 dst）
    st31 = os.stat(os.path.join(dst, "y.txt"))
    bl31 = t31.baseline.get("y.txt", {})
    check(bl31.get("mtime") == st31.st_mtime and bl31.get("size") == st31.st_size,
          "F6: baseline mtime/size 与执行后目标侧真实状态一致")
    # A 侧删除 + two_way_delete -> B 侧删除后 baseline 条目消失，再次同步无差异
    os.remove(os.path.join(src, "seed.txt"))
    t31.two_way_delete = True
    perform_sync(t31)
    check("seed.txt" not in t31.baseline, "F6: 删除传播后 baseline 条目移除")
    res31 = perform_sync(t31, dry_run=True)
    check(res31["diff"].is_empty(), "F6: 删除传播后再次同步无差异(收敛)")
    assert len(failures) == _fail_base, "test_31_F6_baseline: 本节有断言失败"

# ---------- 32. 回归: C1/C2 数据安全 + 冲突策略矩阵 + 持久层清洗 + CLI 退出码 ----------

def test_32_C1_C2():
    """自测节 32. 回归: C1/C2 数据安全 + 冲突策略矩阵 + 持久层清洗 + CLI 退出码"""
    global CONFLICT_ASK, CONFLICT_SKIP, CONFLICT_TARGET, T32, _existing32, _orig_ps32, _orig_scan32, _store32, _v5a, _v5b, _v7a, _v7b
    global _v9a, _v9b, cfg, d, dst, f, out32, out33, rc32, rc33, res, res1c
    global res3, res4, res4b, res6, res7, s32, src, store32, store33, t, t1c, t2
    global t3, t32, t33, t4, t5, t6, t7, t8, t9, td32, x
    _fail_base = len(failures)
    print("[32] 回归: C1 源不可达 / C2 冲突不固化 / 策略矩阵 / 持久层清洗")
    from config import CONFLICT_SKIP, CONFLICT_TARGET, CONFLICT_ASK

    # --- C1: 源目录不可达(U 盘拔出/盘符漂移) -> 中止，绝不删除目标 ---
    d = tempfile.mkdtemp()
    src = os.path.join(d, "src")
    dst = os.path.join(d, "dst")
    os.makedirs(src)
    os.makedirs(dst)
    write(os.path.join(src, "a.txt"), "hello")
    t = fresh_task(MODE_ONE_WAY, src, dst, one_way_delete=True)
    perform_sync(t)
    check(os.path.exists(os.path.join(dst, "a.txt")), "C1 前置: 首次同步完成")
    shutil.rmtree(src)   # 模拟源目录消失
    t2 = fresh_task(MODE_ONE_WAY, src, dst, one_way_delete=True)
    res = perform_sync(t2)
    check(res.get("aborted") is True, "C1: 源不可达时同步中止(aborted)")
    check(t2.last_status == "失败", "C1: 任务状态置'失败'")
    check(os.path.exists(os.path.join(dst, "a.txt")), "C1: 目标文件未被误删(此前的全量删除)")

    # --- C1b: 扫描不完整(子目录权限等) -> 同样中止 ---
    d = tempfile.mkdtemp()
    src = os.path.join(d, "src")
    dst = os.path.join(d, "dst")
    os.makedirs(src)
    os.makedirs(dst)
    write(os.path.join(src, "a.txt"), "hello")
    write(os.path.join(dst, "keep.txt"), "keep")
    t3 = fresh_task(MODE_ONE_WAY, src, dst, one_way_delete=True)




    _orig_scan32 = _sync_engine.scan
    _sync_engine.scan = _err_scan32
    try:
        res3 = perform_sync(t3)
    finally:
        _sync_engine.scan = _orig_scan32
    check(res3.get("aborted") is True and t3.last_status == "失败",
          "C1b: 扫描不完整时中止(宁失败不误删)")
    check(os.path.exists(os.path.join(dst, "keep.txt")), "C1b: 快照不完整时目标未被误删")

    # --- C1c: 目标目录不可达 + 已有非空 baseline -> 中止，绝不误删源侧 ---
    # 对称于 C1：目标缺失仅在首次同步（baseline 为空）时合法；已有 baseline 时
    # 目标被拔/路径漂移会让快照全空，two_way_delete 会把源侧误判为"删除(A 侧)"
    d = tempfile.mkdtemp()
    src = os.path.join(d, "src")
    dst = os.path.join(d, "dst")
    os.makedirs(src)
    os.makedirs(dst)
    write(os.path.join(src, "a.txt"), "hello")
    write(os.path.join(dst, "b.txt"), "world")
    t1c = fresh_task(MODE_TWO_WAY, src, dst, two_way_delete=True)
    perform_sync(t1c)   # 首次同步，回填 baseline
    check(bool(t1c.baseline), "C1c 前置: 双向首次同步建立 baseline")
    check(os.path.exists(os.path.join(src, "a.txt")), "C1c 前置: 源文件存在")
    shutil.rmtree(dst)  # 模拟目标盘被拔/路径漂移
    res1c = perform_sync(t1c)  # 复用同一任务对象（带 baseline 续跑）
    check(res1c.get("aborted") is True and t1c.last_status == "失败",
          "C1c: 目标不可达时中止(aborted)")
    check(os.path.exists(os.path.join(src, "a.txt")),
          "C1c: 源文件未被误删(此前会生成全量'删除(A 侧)')")
    check(os.path.exists(os.path.join(src, "b.txt")),
          "C1c: 目标侧独有文件同样未被误删")

    # --- C2: skip 的冲突不写 baseline -> 下次重新检出冲突，不被固化为单侧覆盖 ---
    d = tempfile.mkdtemp()
    src = os.path.join(d, "src")
    dst = os.path.join(d, "dst")
    os.makedirs(src)
    os.makedirs(dst)
    T32 = 1700000400.0
    write(os.path.join(src, "c.txt"), "src-v", mtime=T32)
    write(os.path.join(dst, "c.txt"), "dst-v", mtime=T32 + 10)
    t4 = fresh_task(MODE_TWO_WAY, src, dst, conflict_policy=CONFLICT_SKIP)
    res4 = perform_sync(t4)
    check(res4["diff"].conflict_count == 1, "C2 前置: 检出 1 个冲突")
    check("c.txt" not in t4.baseline, "C2: skip 冲突不写入 baseline")
    res4b = perform_sync(t4, dry_run=True)
    check(res4b["diff"].conflict_count == 1,
          "C2: 下次同步重新检出冲突(未退化成无备份的单侧覆盖)")

    # --- 冲突策略矩阵: target_wins ---
    d = tempfile.mkdtemp()
    src = os.path.join(d, "src")
    dst = os.path.join(d, "dst")
    os.makedirs(src)
    os.makedirs(dst)
    write(os.path.join(src, "tw.txt"), "src-newer", mtime=3000.0)
    write(os.path.join(dst, "tw.txt"), "dst-older", mtime=2000.0)  # 源更新,验证 target_wins 非按 mtime
    t5 = fresh_task(MODE_TWO_WAY, src, dst, conflict_policy=CONFLICT_TARGET)
    perform_sync(t5)
    with open(os.path.join(src, "tw.txt"), encoding="utf-8") as f:
        _v5a = f.read()
    with open(os.path.join(dst, "tw.txt"), encoding="utf-8") as f:
        _v5b = f.read()
    check(_v5a == _v5b == "dst-older", "策略: target_wins 目标侧胜出(覆盖更新的源)")
    check(len([x for x in os.listdir(src) if x.startswith("tw.txt.conflict-")]) == 1,
          "策略: target_wins 落败方(源)已备份")

    # --- ask 回退: on_ask 抛异常 -> 保守跳过 ---
    d = tempfile.mkdtemp()
    src = os.path.join(d, "src")
    dst = os.path.join(d, "dst")
    os.makedirs(src)
    os.makedirs(dst)
    write(os.path.join(src, "ask1.txt"), "src-v", mtime=1000.0)
    write(os.path.join(dst, "ask1.txt"), "dst-v", mtime=2000.0)




    t6 = fresh_task(MODE_TWO_WAY, src, dst, conflict_policy=CONFLICT_ASK)
    res6 = perform_sync(t6, on_ask=_ask_boom32)
    check(res6["diff"].conflict_count == 1, "ask 回退: 冲突仍被检出")
    with open(os.path.join(src, "ask1.txt"), encoding="utf-8") as f:
        check(f.read() == "src-v", "ask 回退: 异常时源侧保持原状")
    with open(os.path.join(dst, "ask1.txt"), encoding="utf-8") as f:
        check(f.read() == "dst-v", "ask 回退: 异常时目标侧保持原状")
    check("ask1.txt" not in t6.baseline, "ask 回退: 异常回退 skip 的冲突不写 baseline")

    # --- ask 回退: 返回无效值 -> 跳过 ---
    d = tempfile.mkdtemp()
    src = os.path.join(d, "src")
    dst = os.path.join(d, "dst")
    os.makedirs(src)
    os.makedirs(dst)
    write(os.path.join(src, "ask2.txt"), "src-v2", mtime=1000.0)
    write(os.path.join(dst, "ask2.txt"), "dst-v2", mtime=2000.0)




    t7 = fresh_task(MODE_TWO_WAY, src, dst, conflict_policy=CONFLICT_ASK)
    res7 = perform_sync(t7, on_ask=_ask_bad32)
    check(res7["diff"].conflict_count == 1, "ask 无效返回: 冲突仍被检出")
    with open(os.path.join(src, "ask2.txt"), encoding="utf-8") as f:
        _v7a = f.read()
    with open(os.path.join(dst, "ask2.txt"), encoding="utf-8") as f:
        _v7b = f.read()
    check(_v7a == "src-v2" and _v7b == "dst-v2", "ask 无效返回: 按跳过处理(两侧原状)")
    check("ask2.txt" not in t7.baseline, "ask 无效返回: 冲突不写 baseline")

    # --- ask 正常: 返回 source_wins -> 生效 ---
    d = tempfile.mkdtemp()
    src = os.path.join(d, "src")
    dst = os.path.join(d, "dst")
    os.makedirs(src)
    os.makedirs(dst)
    write(os.path.join(src, "ask3.txt"), "src-v3", mtime=1000.0)
    write(os.path.join(dst, "ask3.txt"), "dst-v3", mtime=2000.0)




    t8 = fresh_task(MODE_TWO_WAY, src, dst, conflict_policy=CONFLICT_ASK)
    perform_sync(t8, on_ask=_ask_src32)
    with open(os.path.join(dst, "ask3.txt"), encoding="utf-8") as f:
        check(f.read() == "src-v3", "ask 正常返回: source_wins 生效(覆盖更新的目标)")
    check(len([x for x in os.listdir(dst) if x.startswith("ask3.txt.conflict-")]) == 1,
          "ask 正常返回: 落败方(目标)已备份")

    # --- ask + 无人值守(on_ask=None) -> 回退 newer_wins ---
    d = tempfile.mkdtemp()
    src = os.path.join(d, "src")
    dst = os.path.join(d, "dst")
    os.makedirs(src)
    os.makedirs(dst)
    write(os.path.join(src, "ask4.txt"), "src-newer", mtime=3000.0)
    write(os.path.join(dst, "ask4.txt"), "dst-older", mtime=2000.0)
    t9 = fresh_task(MODE_TWO_WAY, src, dst, conflict_policy=CONFLICT_ASK)
    perform_sync(t9)   # 不传 on_ask(调度器/CLI 场景)
    with open(os.path.join(src, "ask4.txt"), encoding="utf-8") as f:
        _v9a = f.read()
    with open(os.path.join(dst, "ask4.txt"), encoding="utf-8") as f:
        _v9b = f.read()
    check(_v9a == _v9b == "src-newer", "ask 无人值守: 回退 newer_wins(不卡死)")

    # --- 持久层清洗: Schedule.from_dict / Task.from_dict ---
    s32 = _Sched.from_dict({"enabled": "yes", "type": "bogus", "interval_minutes": "abc",
                            "times": ["08:00", " 09:30 ", "25:99", 123, None],
                            "weekdays": [0, 3, 9, "2", "abc", 3]})
    check(s32.type == SCHED_INTERVAL and s32.interval_minutes == 60,
          "清洗: 非法类型/间隔回退默认")
    check(s32.times == ["08:00", "09:30"], "清洗: times 仅保留合法时刻")
    check(s32.weekdays == [3, 2], "清洗: weekdays 剔除越界/非数值并去重")
    check(_Sched.from_dict(None).interval_minutes == 60, "清洗: None dict 全默认")

    td32 = Task.from_dict({"last_run": "abc", "mode": "xxx", "conflict_policy": "xxx",
                           "name": 123, "source": None, "enabled": 0,
                           "schedule": {"type": "daily", "times": ["8:00"],
                                        "interval_minutes": -5}})
    check(td32.last_run is None, "清洗: last_run 非数值回退 None")
    check(td32.mode == MODE_ONE_WAY and td32.conflict_policy == CONFLICT_NEWER,
          "清洗: 白名单外回退默认")
    check(td32.name == "" and td32.source == "" and td32.enabled is False,
          "清洗: 字段类型收敛")
    check(td32.schedule.type == "daily" and td32.schedule.times == ["8:00"]
          and td32.schedule.interval_minutes == 60, "清洗: 嵌套 Schedule 同样净化")
    check(Task.from_dict({"last_run": True}).last_run is None,
          "清洗: bool last_run 视为非法(不当作 epoch=1)")

    # --- CLI 退出码 2: 部分失败 ---
    d = tempfile.mkdtemp()
    cfg = os.path.join(d, "config", "tasks.json")
    os.makedirs(os.path.dirname(cfg))
    src = os.path.join(d, "src")
    dst = os.path.join(d, "dst")
    os.makedirs(src)
    os.makedirs(dst)
    write(os.path.join(src, "ok.txt"), "ok")
    write(os.path.join(src, "bad.txt"), "bad")
    os.makedirs(os.path.join(dst, "bad.txt"))   # 同名目录 -> 类型冲突 -> 失败 1
    store32 = TaskStore(cfg)
    t32 = fresh_task(MODE_ONE_WAY, src, dst, one_way_delete=False)
    t32.name = "部分失败"
    store32.add(t32)
    rc32, out32 = _cli(["--sync", "部分失败"], d)
    check(rc32 == 2, "CLI: 部分失败退出码 2")
    check("失败 1" in out32, "CLI: 部分失败摘要含失败数")
    check(os.path.exists(os.path.join(dst, "ok.txt")), "CLI: 部分失败时成功项仍完成")

    # --- CLI 异常路径: 失败状态落盘(不残留上次'成功') ---


    _orig_ps32 = _sync_engine.perform_sync
    _sync_engine.perform_sync = _boom32
    try:
        rc33, out33 = _cli(["--sync", "部分失败"], d)
        check(rc33 == 3 and "同步失败" in out33, "CLI: 异常路径退出码 3")
    finally:
        _sync_engine.perform_sync = _orig_ps32
    store33 = TaskStore(cfg)
    t33 = store33.get(t32.id)
    check(t33 is not None and t33.last_status == "失败" and "执行异常" in t33.last_summary,
          "CLI: 异常路径失败状态已落盘")

    # --- 任务名查重(TaskDialog) ---


    _existing32 = fresh_task(MODE_ONE_WAY, src25, dst25)
    _existing32.name = "任务A"
    _store32 = _FakeStore32([_existing32])

    _err_box_calls[:] = []
    d = _mk_dialog()
    d.store = _store32
    d._on_save()
    check(len(_err_box_calls) == 1 and d.result is None and "同名" in _err_box_calls[0][1],
          "查重: 新建同名任务被拦截")

    _err_box_calls[:] = []
    d = _mk_dialog()
    d.store = _store32
    d.is_new = False
    d.task = _existing32
    d._on_save()
    check(d.result is not None and _err_box_calls == [], "查重: 编辑自身不误拦")

    _err_box_calls[:] = []
    d = _mk_dialog()
    d.store = None
    d._on_save()
    check(d.result is not None and _err_box_calls == [], "查重: store 缺失时跳过查重不崩溃")
    assert len(failures) == _fail_base, "test_32_C1_C2: 本节有断言失败"

# ---------- 33. 托盘 / 开机自启（tray / autostart） ----------

def test_33_tray_autostart():
    """自测节 33. 托盘 / 开机自启（tray / autostart）"""
    global _autostart, _tray, args, cmd, content, e, f, home, home2, p
    _fail_base = len(failures)
    print("[33] 托盘与开机自启")
    import tray as _tray
    import autostart as _autostart

    check(_tray.is_supported() == (sys.platform == "win32"),
          "tray.is_supported 与平台一致")

    # 托盘图标本体仅 Windows 可创建；非 Windows 必须优雅抛 OSError（降级路径）
    if sys.platform != "win32":
        try:
            _tray.TrayIcon("t", [], lambda x: None)
            check(False, "非 Windows 创建托盘应抛 OSError")
        except OSError:
            check(True, "非 Windows 创建托盘优雅抛 OSError")
        except Exception as e:
            check(False, "非 Windows 托盘异常类型: %s" % e)

    check(_autostart.is_supported() == (sys.platform in ("win32", "linux", "darwin")),
          "autostart.is_supported 与平台一致")

    cmd = _autostart.build_command()
    check(cmd.endswith("--autostart"), "自启命令行含 --autostart")
    check(sys.executable in cmd, "自启命令行含解释器/exe 路径")
    args = _autostart._program_args()
    check(args[-1] == "--autostart", "ProgramArguments 末位为 --autostart")

    # Linux .desktop 与 macOS plist 为纯文件逻辑：注入 home 跨平台可测
    home = tempfile.mkdtemp()
    check(_autostart._linux_enable(home, "/opt/app --autostart"),
          "Linux .desktop 写入成功")
    p = _autostart._linux_path(home)
    check(os.path.isfile(p) and _autostart._linux_is_enabled(home),
          "Linux 自启文件生成且状态为已启用")
    with open(p, encoding="utf-8") as f:
        content = f.read()
    check("[Desktop Entry]" in content and "--autostart" in content,
          "Linux .desktop 内容正确")
    check(_autostart._linux_disable(home) and not _autostart._linux_is_enabled(home),
          "Linux 反注册删除文件")

    home2 = tempfile.mkdtemp()
    check(_autostart._mac_enable(home2, ["/opt/app", "--autostart"]),
          "macOS plist 写入成功")
    check(_autostart._mac_is_enabled(home2), "macOS 自启文件生成且状态为已启用")
    check(_autostart._mac_disable(home2) and not _autostart._mac_is_enabled(home2),
          "macOS 反注册删除文件")

    # Windows 注册表分支在非 Windows 上必须优雅失败（不崩溃、不误报）
    if sys.platform != "win32":
        check(not _autostart._win_enable("x"), "非 Windows 注册自启返回 False")
        check(not _autostart._win_disable(), "非 Windows 反注册返回 False")
        check(not _autostart._win_is_enabled(), "非 Windows 查询自启返回 False")
    assert len(failures) == _fail_base, "test_33_tray_autostart: 本节有断言失败"

# ---------- 34. fast 比较模式（FAT32 mtime 容差内免哈希） ----------

def test_34_fast_FAT32():
    """自测节 34. fast 比较模式（FAT32 mtime 容差内免哈希）"""
    global T34, _hash_calls, _orig_hash34, a, d, dst, f34, rels3, res, res2, res3, res4
    global src, st34, t, t2, t3, t4
    _fail_base = len(failures)
    print("[34] fast 比较模式: FAT32 容差内免哈希判同")

    d = tempfile.mkdtemp()
    src = os.path.join(d, "src")
    dst = os.path.join(d, "dst")
    os.makedirs(src)
    os.makedirs(dst)
    T34 = 1700000000.0
    write(os.path.join(src, "a.txt"), "hello", mtime=T34)
    write(os.path.join(dst, "a.txt"), "hello", mtime=T34 + 1)  # FAT32 2s 粒度截断

    _hash_calls = [0]
    _orig_hash34 = _sync_engine.hash_file




    _sync_engine.hash_file = _counting_hash34
    try:
        t = fresh_task(MODE_ONE_WAY, src, dst)
        t.compare = "fast"
        res = perform_sync(t, dry_run=True)
        check(res["diff"].copy_count == 0 and _hash_calls[0] == 0,
              "fast: 容差内 mtime 微差免哈希判同(零哈希读盘)")

        _hash_calls[0] = 0
        t2 = fresh_task(MODE_ONE_WAY, src, dst)
        t2.compare = "auto"
        res2 = perform_sync(t2, dry_run=True)
        check(res2["diff"].copy_count == 0 and _hash_calls[0] > 0,
              "auto: 同场景哈希确认内容相同(不复制但读盘)")
    finally:
        _sync_engine.hash_file = _orig_hash34

    # fast 的安全网：超出容差的 mtime 差 / size 变化仍必须检出
    write(os.path.join(src, "b.txt"), "new", mtime=T34)
    write(os.path.join(dst, "b.txt"), "old", mtime=T34 - 10)      # Δmtime 超容差
    write(os.path.join(src, "c.txt"), "xyz", mtime=T34)
    write(os.path.join(dst, "c.txt"), "abcdef", mtime=T34 + 1)    # size 不同
    t3 = fresh_task(MODE_ONE_WAY, src, dst)
    t3.compare = "fast"
    res3 = perform_sync(t3, dry_run=True)
    rels3 = {a.rel for a in res3["diff"].actions}
    check("b.txt" in rels3 and "c.txt" in rels3,
          "fast: 超容差/尺寸变化仍被检出(安全网不受影响)")

    # fast 双向：baseline mtime 容差内微差（FAT32 截断）判 same，免哈希
    d = tempfile.mkdtemp()
    src = os.path.join(d, "src")
    dst = os.path.join(d, "dst")
    os.makedirs(src)
    os.makedirs(dst)
    write(os.path.join(src, "a.txt"), "hello")
    t4 = fresh_task(MODE_TWO_WAY, src, dst)
    t4.compare = "fast"
    perform_sync(t4)  # 首次同步建立 baseline
    f34 = os.path.join(dst, "a.txt")
    st34 = os.stat(f34)
    os.utime(f34, (st34.st_atime, st34.st_mtime + 1))  # 模拟 FAT32 截断(<2s)




    _sync_engine.hash_file = _boom_hash34
    try:
        res4 = perform_sync(t4, dry_run=True)
        check(res4["diff"].is_empty(),
              "fast: baseline 容差内微差免哈希判 same(双向)")
    finally:
        _sync_engine.hash_file = _orig_hash34
    assert len(failures) == _fail_base, "test_34_fast_FAT32: 本节有断言失败"

# ---------- 35. 第三轮修复回归: 删除vs修改冲突 / 空根防线 / 失败记账 / skip计失败 ----------

def test_35_vs_skip():
    """自测节 35. 第三轮修复回归: 删除vs修改冲突 / 空根防线 / 失败记账 / skip计失败"""
    global CONFLICT_SKIP, CONFLICT_TARGET, _Sched35, _Sched35i, _Store35, _Task35, _Task35b, _calls35l, _cfg35l, _dup35l, _got35l, _logger_mod35l
    global _now35, _orig_copy35, _orig_fsync35l, _orig_gl35l, _out35m, _prev_daily35, _re35, _scan35, _sic35m, _st35l, _time35, a
    global bp35, bstale35k, d, d35k, d35l, d35m, dst, f, fp, fresh35k, kinds, legacy35k
    global now35, nxt35, old35k, p35, p35k, p35l, rc35m, res, res2, root, s35, sched35
    global src, st35, st35i, stale35k, t, t35, t35i, t35k, tk35, x
    _fail_base = len(failures)
    print("[35] 第三轮修复回归")
    from config import CONFLICT_SKIP, CONFLICT_TARGET, Task as _Task35
    from scanner import scan as _scan35, _CONFLICT_BACKUP_RE as _re35
    from utils.timeutil import prev_daily_time as _prev_daily35

    # --- 35a. 一侧删除、另一侧修改 -> conflict_del(不再是静默恢复/删除) ---
    d = tempfile.mkdtemp()
    src = os.path.join(d, "src"); dst = os.path.join(d, "dst")
    os.makedirs(src); os.makedirs(dst)
    t = fresh_task(MODE_TWO_WAY, src, dst)
    t.two_way_delete = True
    write(os.path.join(src, "f.txt"), "v1")
    perform_sync(t)
    os.remove(os.path.join(src, "f.txt"))
    write(os.path.join(dst, "f.txt"), "v2-modified")
    os.utime(os.path.join(dst, "f.txt"), (2000000000, 2000000000))  # 确保 mtime 不同
    res = perform_sync(t, dry_run=True)
    kinds = [a.kind for a in res["diff"].actions]
    check(kinds == ["conflict_del"], "35a: A删B改 检出 conflict_del(而非静默动作)")
    check(res["diff"].conflict_count == 1, "35a: conflict_del 计入冲突计数")
    perform_sync(t)  # 默认 newer_wins -> 修改方胜, 恢复文件
    check(os.path.exists(os.path.join(src, "f.txt")),
          "35a: newer_wins 下修改方胜, 文件恢复到源侧")
    check(open(os.path.join(src, "f.txt")).read() == "v2-modified",
          "35a: 恢复内容为修改版")
    check(perform_sync(t, dry_run=True)["diff"].is_empty(), "35a: 再次同步收敛")

    # --- 35b. source_wins + 源侧删除 -> 删除方胜: 备份修改侧后删除 ---
    d = tempfile.mkdtemp()
    src = os.path.join(d, "src"); dst = os.path.join(d, "dst")
    os.makedirs(src); os.makedirs(dst)
    t = fresh_task(MODE_TWO_WAY, src, dst, conflict_policy=CONFLICT_SOURCE)
    t.two_way_delete = True
    write(os.path.join(src, "g.txt"), "v1")
    perform_sync(t)
    os.remove(os.path.join(src, "g.txt"))
    write(os.path.join(dst, "g.txt"), "v2")
    os.utime(os.path.join(dst, "g.txt"), (2000000000, 2000000000))
    perform_sync(t)
    check(not os.path.exists(os.path.join(src, "g.txt")), "35b: 删除方胜, 源侧不复活")
    check(not os.path.exists(os.path.join(dst, "g.txt")), "35b: 删除方胜, 目标侧已删")
    check(any("g.txt.conflict-" in x for x in os.listdir(dst)),
          "35b: 删除前修改侧已备份(.conflict- 副本)")

    # --- 35c. 目标侧防线: 双向+传播删除+目标空根+非空baseline -> 中止, 源侧不误删 ---
    d = tempfile.mkdtemp()
    src = os.path.join(d, "src"); dst = os.path.join(d, "dst")
    os.makedirs(src); os.makedirs(dst)
    t = fresh_task(MODE_TWO_WAY, src, dst)
    t.two_way_delete = True
    write(os.path.join(src, "f.txt"), "data")
    perform_sync(t)
    check(bool(t.baseline), "35c 前置: 首次同步建立 baseline")
    for x in os.listdir(dst):
        os.remove(os.path.join(dst, x))  # 模拟换盘/清空目标根
    res = perform_sync(t)
    check(res.get("aborted") is True, "35c: 目标空根+非空baseline 中止(防误删源侧)")
    check(t.last_status == "失败", "35c: 任务状态置'失败'")
    check(os.path.exists(os.path.join(src, "f.txt")), "35c: 源侧文件未被误删")

    # --- 35d. 单向+目标空盘 -> 合法重新全量同步(不误伤) ---
    d = tempfile.mkdtemp()
    src = os.path.join(d, "src"); dst = os.path.join(d, "dst")
    os.makedirs(src); os.makedirs(dst)
    t = fresh_task(MODE_ONE_WAY, src, dst)
    t.one_way_delete = True
    write(os.path.join(src, "f.txt"), "data")
    perform_sync(t)
    for x in os.listdir(dst):
        os.remove(os.path.join(dst, x))
    res = perform_sync(t)
    check(not res.get("aborted"), "35d: 单向目标空盘不中止")
    check(os.path.exists(os.path.join(dst, "f.txt")), "35d: 重新全量同步成功")

    # --- 35e. 失败记账: 复制失败后保留旧baseline, 下次重试方向不反转 ---
    d = tempfile.mkdtemp()
    src = os.path.join(d, "src"); dst = os.path.join(d, "dst")
    os.makedirs(src); os.makedirs(dst)
    t = fresh_task(MODE_TWO_WAY, src, dst, conflict_policy=CONFLICT_TARGET)
    t.two_way_delete = True
    write(os.path.join(src, "f.txt"), "v1")
    perform_sync(t)
    write(os.path.join(src, "f.txt"), "v2")
    os.utime(os.path.join(src, "f.txt"), (2000000000, 2000000000))
    _orig_copy35 = _sync_engine._do_copy
    _sync_engine._do_copy = _fail_copy35
    try:
        res = perform_sync(t)
    finally:
        _sync_engine._do_copy = _orig_copy35
    check("f.txt" in t.baseline, "35e: 复制失败条目保留旧 baseline(不整条剔除)")
    res2 = perform_sync(t)
    check(open(os.path.join(dst, "f.txt")).read() == "v2",
          "35e: 重试方向仍为 A->B(未在 target_wins 下反转 B->A)")
    check(perform_sync(t, dry_run=True)["diff"].is_empty(), "35e: 收敛")

    # --- 35f. CONFLICT_SKIP 计入 fail(冲突未处理不误报'成功') ---
    d = tempfile.mkdtemp()
    src = os.path.join(d, "src"); dst = os.path.join(d, "dst")
    os.makedirs(src); os.makedirs(dst)
    t = fresh_task(MODE_TWO_WAY, src, dst, conflict_policy=CONFLICT_SKIP)
    t.two_way_delete = True
    write(os.path.join(src, "c.txt"), "src-content-longer")
    write(os.path.join(dst, "c.txt"), "dst-content")
    res = perform_sync(t)
    check(t.last_status == "部分失败", "35f: skip 冲突置'部分失败'(不误报成功)")
    check(res["fail_count"] == 1, "35f: skip 冲突计入 fail_count")
    check("c.txt" not in t.baseline, "35f: skip 冲突不写 baseline")
    # 正常解决冲突不误伤
    d = tempfile.mkdtemp()
    src = os.path.join(d, "src"); dst = os.path.join(d, "dst")
    os.makedirs(src); os.makedirs(dst)
    t = fresh_task(MODE_TWO_WAY, src, dst)
    t.two_way_delete = True
    write(os.path.join(src, "c.txt"), "src-content-longer")
    os.utime(os.path.join(src, "c.txt"), (1700000400, 1700000400))
    write(os.path.join(dst, "c.txt"), "dst-content")
    os.utime(os.path.join(dst, "c.txt"), (1700000400, 1700000400))
    res = perform_sync(t)
    check(t.last_status == "成功" and res["fail_count"] == 0,
          "35f: 正常解决冲突不计 fail(回归)")

    # --- 35g. scanner: 嵌套尾斜杠目录模式 / .conflict- 精确匹配 / stat 降级 ---
    d = tempfile.mkdtemp()
    root = os.path.join(d, "root")
    os.makedirs(os.path.join(root, "foo", "bar"))
    write(os.path.join(root, "a.txt"), "x")
    write(os.path.join(root, "foo", "bar", "b.txt"), "y")
    write(os.path.join(root, "report.conflict-final.docx"), "真实文件")
    res = _scan35(root, exclude=["foo/bar/"])
    check("foo/bar/b.txt" not in res, "35g: 嵌套尾斜杠目录模式生效")
    check("a.txt" in res, "35g: 普通文件不受影响")
    res2 = _scan35(root)
    check("report.conflict-final.docx" in res2, "35g: .conflict- 不误伤真实用户文件")
    check(_re35.search("x.txt.conflict-20260821-120000.123"), "35g: 精确备份格式仍被识别")
    check(not _re35.search("report.conflict-final.docx"), "35g: 非时间戳格式不误匹配")

    # --- 35h. config: from_dict 非 dict 防护 / compare 白名单 / baseline 结构校验 ---
    from config import Schedule as _Sched35, TaskStore as _Store35, Task as _Task35b
    s35 = _Sched35.from_dict("abc")
    check(s35.interval_minutes == 60, "35h: Schedule.from_dict(非dict) 回退默认")
    t35 = _Task35b.from_dict({"name": "x", "compare": "bogus"})
    check(t35.compare == "auto", "35h: compare 白名单外回退 auto")
    d = tempfile.mkdtemp()
    st35 = _Store35(os.path.join(d, "tasks.json"))
    tk35 = _Task35b(name="t", source="/s", target="/t", mode=MODE_ONE_WAY)
    st35.add(tk35)
    bp35 = st35._baseline_path(tk35.id)
    os.makedirs(os.path.dirname(bp35), exist_ok=True)
    with open(bp35, "w", encoding="utf-8") as f:
        json.dump({"ok.txt": {"size": 1, "mtime": 1.0, "hash": "h"},
                   "bad.txt": "not-a-dict"}, f)
    st35._load_baseline(tk35)
    check("ok.txt" in tk35.baseline and "bad.txt" not in tk35.baseline,
          "35h: baseline 结构校验剔除非法条目")

    # --- 35i. scheduler: daily 运行中顺延重算未来计划点(不滑动 now+60) ---
    from utils.timeutil import now_epoch as _now35
    d = tempfile.mkdtemp()
    st35i = _Store35(os.path.join(d, "tasks.json"))
    t35i = _Task35b(name="d", source="/s", target="/t", mode=MODE_ONE_WAY)
    t35i.schedule.enabled = True
    t35i.schedule.type = "daily"
    t35i.schedule.times = ["08:00"]
    st35i.add(t35i)
    from scheduler import Scheduler as _Sched35i
    sched35 = _Sched35i(st35i, lambda tk: {"changed": False, "fail_count": 0})
    sched35.start()
    sched35.acquire(t35i.id)  # 模拟任务运行中
    with sched35._next_lock:
        t35i.next_run = _now35()
    sched35._poll_task(t35i, _now35() + 1)
    with sched35._next_lock:
        nxt35 = t35i.next_run
    sched35.release(t35i.id)
    sched35.stop()
    check(nxt35 is not None and nxt35 > _now35(),
          "35i: daily 运行中顺延到未来计划点(完成即重跑已消除)")

    # --- 35j. prev_daily_time 回看窗口扩大到 8 天(停机补跑判定) ---
    import time as _time35
    now35 = _time35.mktime(_time35.strptime("2026-08-21 12:00:00", "%Y-%m-%d %H:%M:%S"))
    p35 = _prev_daily35(["08:00"], now35 - 6 * 86400)
    check(p35 is not None, "35j: 停机 6 天仍能判定错过触发(窗口 8 天)")
    check(_prev_daily35([], now35) is None, "35j: 空 times 返回 None")

    # --- 35k. 第四轮修复回归: 内嵌 baseline 清洗 / 崩溃 tmp 残留清理 / 目标不存在可保存 ---
    print("[35k] 回归: 内嵌 baseline 清洗 / tmp 残留清理 / 目标目录自动创建")

    # F2: Task.from_dict 内嵌旧格式 baseline 的结构清洗（与文件加载校验对齐）
    t35k = _Task35b.from_dict({
        "name": "x",
        "baseline": {"ok.txt": {"size": 1, "mtime": 1.0}, "bad": "not-a-dict"},
    })
    check("ok.txt" in t35k.baseline and "bad" not in t35k.baseline,
          "35k: 内嵌 baseline 非 dict 条目被剔除")
    check(_Task35b.from_dict({"baseline": "junk"}).baseline == {},
          "35k: 内嵌 baseline 非 dict 根回退空")

    # F3: 崩溃残留 .pid.tid.tmp~ 在 load() 时被清理（config 与 baseline 目录 + 旧命名）
    d35k = tempfile.mkdtemp()
    p35k = os.path.join(d35k, "tasks.json")
    with open(p35k, "w", encoding="utf-8") as f:
        json.dump({"tasks": []}, f)
    stale35k = p35k + ".111.222.tmp~"
    fresh35k = p35k + ".333.444.tmp~"
    legacy35k = p35k + ".tmp"
    old35k = time.time() - 7200
    for fp in (stale35k, fresh35k, legacy35k):
        open(fp, "w").close()
        os.utime(fp, (old35k, old35k))
    os.utime(fresh35k, (time.time(), time.time()))  # 新文件视为并发实例在写
    os.makedirs(os.path.join(d35k, "baseline"))
    bstale35k = os.path.join(d35k, "baseline", "abc.json.555.666.tmp~")
    open(bstale35k, "w").close()
    os.utime(bstale35k, (old35k, old35k))
    _Store35(p35k)  # 构造即触发 load() -> _cleanup_stale_tmp()
    check(not os.path.exists(stale35k), "35k: 超过 1 小时的 tasks.json tmp 残留被清理")
    check(not os.path.exists(legacy35k), "35k: 旧版固定命名 .tmp 残留被清理")
    check(not os.path.exists(bstale35k), "35k: baseline 目录 tmp 残留被清理")
    check(os.path.exists(fresh35k), "35k: 1 小时内的 tmp 不误删（可能是并发实例在写）")

    # F4: 目标目录不存在 -> askyesno 确认后允许保存（引擎首跑自动创建）
    _fake_mb.askyesno = lambda *a, **k: _ask_calls.append(a) or True
    _ask_calls[:] = []
    d = _mk_dialog()
    d._dst = _FakeEntry(os.path.join(d25, "new-target"))
    d._on_save()
    check(len(_ask_calls) == 1 and d.result is not None
          and d.result.target == os.path.abspath(os.path.join(d25, "new-target")),
          "35k: 目标目录不存在经确认后保存（首跑自动创建语义）")

    _fake_mb.askyesno = lambda *a, **k: _ask_calls.append(a) or False
    _ask_calls[:] = []
    d = _mk_dialog()
    d._dst = _FakeEntry(os.path.join(d25, "nope"))
    d._on_save()
    check(len(_ask_calls) == 1 and d.result is None,
          "35k: 目标目录不存在且拒绝确认时不保存")

    _err_box_calls[:] = []
    d = _mk_dialog()
    d._dst = _FakeEntry(dst25)
    d._on_save()
    check(_err_box_calls == [] and d.result is not None,
          "35k: 目标目录存在时不弹确认框（原行为不变）")

    # --- 35l. 第五轮修复回归: 持久层告警入日志 / id 清洗与去重 / 原子写 fsync ---
    print("[35l] 回归: 持久层告警入日志 / id 清洗与去重 / 原子写 fsync")
    import config as _cfg35l
    import logger as _logger_mod35l

    # G1: _safe_print 双通道——stdout 之外必须进入日志通道（GUI quiet/windowed
    # exe 下 stdout 不可见甚至不存在，baseline 写失败等告警否则彻底丢失）
    _orig_gl35l = _logger_mod35l.get_logger
    _got35l = []




    _logger_mod35l.get_logger = lambda: _FakeLog35l()
    try:
        _cfg35l._safe_print("G1 观测性告警测试")
    finally:
        _logger_mod35l.get_logger = _orig_gl35l
    check(len(_got35l) == 1 and "G1 观测性告警测试" in _got35l[0],
          "35l: _safe_print 告警同步写入日志通道（G1）")

    # G2: from_dict 的 id 类型清洗（非 str/空 id 会让 _baseline_path 的
    # 字符串拼接与 --list 的 t.id[:8] 切片崩溃）
    check(isinstance(_Task35b.from_dict({"id": 123}).id, str)
          and len(_Task35b.from_dict({"id": 123}).id) == 32,
          "35l: 非 str id 替换为 uuid hex（G2）")
    check(_Task35b.from_dict({"id": ""}).id != "",
          "35l: 空 id 重新生成（G2）")
    check(_Task35b.from_dict({"id": "keep-me"}).id == "keep-me",
          "35l: 合法 str id 原样保留（G2）")

    # G3: 手编配置重复 id——加载时为副本重新生成，任务数不减、GUI iid 不再冲突
    d35l = tempfile.mkdtemp()
    p35l = os.path.join(d35l, "tasks.json")
    _dup35l = {"id": "dup-id-35l", "name": "n", "source": "s", "target": "t"}
    with open(p35l, "w", encoding="utf-8") as f:
        json.dump({"tasks": [dict(_dup35l), dict(_dup35l)]}, f)
    _st35l = _Store35(p35l)
    check(len(_st35l.tasks) == 2
          and _st35l.tasks[0].id == "dup-id-35l"
          and _st35l.tasks[1].id != "dup-id-35l",
          "35l: 重复 id 加载时副本重新生成且任务数不减（G3）")

    # G4: 原子写 JSON 在 os.replace 前 fsync（断电防半截新文件）
    _calls35l = []
    _orig_fsync35l = os.fsync
    os.fsync = lambda fd: _calls35l.append(fd)
    try:
        _st35l.save()
    finally:
        os.fsync = _orig_fsync35l
    check(len(_calls35l) >= 1,
          "35l: _atomic_write_json 在 replace 前 fsync（G4）")

    # --- 35m. 第六轮修复回归: 编辑身份变更判定(H1) / CLI 日志初始化顺序(H2) ---
    print("[35m] 回归: 编辑身份变更判定(H1) / CLI 日志初始化顺序(H2)")
    from config import sync_identity_changed as _sic35m

    check(not _sic35m("/x/s", "/x/d", "one_way", "/x/s", "/x/d", "one_way"),
          "35m: 身份未变返回 False（H1）")
    check(_sic35m("/x/s", "/x/d", "one_way", "/x/s2", "/x/d", "one_way"),
          "35m: 仅源路径变化返回 True（H1）")
    check(_sic35m("/x/s", "/x/d", "one_way", "/x/s", "/x/d2", "one_way"),
          "35m: 仅目标路径变化返回 True（H1）")
    check(_sic35m("/x/s", "/x/d", "one_way", "/x/s", "/x/d", "two_way"),
          "35m: 仅同步方向变化返回 True（H1）")
    check(not _sic35m("s35m", "/x/d", "two_way",
                      os.path.abspath("s35m"), "/x/d", "two_way"),
          "35m: 相对/绝对表达同一路径视为未变（H1）")

    # H2: --list 时日志目录已在注入的 app_dir 下创建（init_logger 先于 TaskStore，
    # 持久层告警落入正式 logs/ 而非 cwd 兜底）
    d35m = tempfile.mkdtemp()
    rc35m, _out35m = _cli(["--list"], d35m)
    check(rc35m == 0 and os.path.isdir(os.path.join(d35m, "logs")),
          "35m: run_cli 日志初始化先于配置加载（H2）")
    assert len(failures) == _fail_base, "test_35_vs_skip: 本节有断言失败"


# ---------- 36. 重构回归: 提取的纯函数/助手行为不变 ----------
def test_36_refactor_regress():
    # type: () -> None
    """自测节 36. 重构回归: scanner 拆分 / 对话框纯函数 / 备份命名 / 有界join"""
    global b1, b2, d36, ev36, loser36, snap_all, snap_exc, snap_inc
    global src36, sub36, th_slow36, th36, t036
    _fail_base = len(failures)
    print("[36] 重构回归: scanner 拆分 / 对话框纯函数 / 备份命名 / 有界join")

    from scanner import scan, ScanCancelled, _CONFLICT_BACKUP_RE
    import threading
    from gui_task_dialog import (
        validate_task_name, find_dup_task_name, validate_dir_pair,
        parse_interval_minutes, parse_weekdays_text,
    )
    from scheduler import join_threads_bounded

    # scanner.scan 拆分后：目录条目记录、include 过滤、排除、取消语义不变
    d36 = tempfile.mkdtemp()
    src36 = os.path.join(d36, "src")
    sub36 = os.path.join(src36, "sub")
    os.makedirs(sub36)
    write(os.path.join(src36, "a.txt"), "A")
    write(os.path.join(sub36, "b.txt"), "B")
    os.makedirs(os.path.join(src36, "emptydir"))
    snap_all = scan(src36)
    check(sorted(snap_all) == ["a.txt", "emptydir", "sub", "sub/b.txt"],
          "36: scan 无 include 时记录目录与嵌套文件(拆分后行为不变)")
    snap_inc = scan(src36, include=["*.txt"])
    check(sorted(snap_inc) == ["a.txt", "sub/b.txt"],
          "36: scan include 过滤下不记录目录条目(拆分后行为不变)")
    snap_exc = scan(src36, exclude=["sub/"])
    check(sorted(snap_exc) == ["a.txt", "emptydir"],
          "36: scan 目录级排除生效(拆分后行为不变)")
    ev36 = threading.Event()
    ev36.set()
    try:
        scan(src36, cancel_event=ev36)
        check(False, "36: 取消事件置位应抛 ScanCancelled")
    except ScanCancelled:
        check(True, "36: 取消事件置位抛 ScanCancelled(拆分后行为不变)")

    # gui_task_dialog 纯校验函数（fake tkinter 已由第 25 节注入 sys.modules）
    check(validate_task_name("") == "请填写任务名称",
          "36: validate_task_name 空名拦截")
    check(validate_task_name("x") is None,
          "36: validate_task_name 合法名通过")

    class _T36(object):
        def __init__(self, name, tid):
            self.name = name
            self.id = tid

    check(find_dup_task_name([_T36("a", "1"), _T36("b", "2")], "b", None) == "b",
          "36: find_dup_task_name 命中重名")
    check(find_dup_task_name([_T36("a", "1")], "a", "1") is None,
          "36: find_dup_task_name 排除自身 id")
    check(find_dup_task_name(None, "a", None) is None,
          "36: find_dup_task_name tasks 为 None 不崩溃")
    check(validate_dir_pair("/x/s", "/x/s") == "源目录与目标目录不能相同",
          "36: validate_dir_pair 相同路径拦截")
    check(validate_dir_pair("/x/s", "/x/s/sub") is not None,
          "36: validate_dir_pair 父子目录拦截")
    check(validate_dir_pair("/x/s", "/x/d") is None,
          "36: validate_dir_pair 平级目录放行")
    check(parse_interval_minutes("abc", 60) == 60
          and parse_interval_minutes("15", 60) == 15,
          "36: parse_interval_minutes 非法回退/合法解析")
    check(parse_weekdays_text("1, abc ,3,x") == [1, 3],
          "36: parse_weekdays_text 忽略非法项保留合法项")

    # sync_engine._unique_backup：同秒冲突生成序号后缀，格式被 scanner 精确识别
    loser36 = os.path.join(d36, "f.bin")
    write(loser36, "x")
    b1 = _sync_engine._unique_backup(loser36)
    open(b1, "w").close()
    b2 = _sync_engine._unique_backup(loser36)
    # 两次调用可能同毫秒（第二份带 -序号 后缀）或跨毫秒（各自独立时间戳，
    # 无后缀）——两者均为正确行为，断言不得依赖毫秒落点（此前写死单分支
    # 在跨毫秒时误报）
    same_ms = b2.startswith(b1 + "-")
    diff_ms = (_CONFLICT_BACKUP_RE.search(os.path.basename(b1)) is not None
               and _CONFLICT_BACKUP_RE.search(os.path.basename(b2)) is not None)
    check(b1 != b2 and (same_ms or diff_ms),
          "36: _unique_backup 同毫秒加序号/跨毫秒独立时间戳均不重名")
    check(_CONFLICT_BACKUP_RE.search(os.path.basename(b1)) is not None,
          "36: 备份命名仍匹配 scanner 保留名正则(不被同步传播)")
    check(_CONFLICT_BACKUP_RE.search("report.conflict-final.docx") is None,
          "36: 正则不误伤真实用户文件")

    # join_threads_bounded：正常等待完成 + 超时不无限阻塞
    th36 = threading.Thread(target=time.sleep, args=(0.3,), daemon=True)
    th36.start()
    t036 = time.time()
    join_threads_bounded([th36], 5)
    check(not th36.is_alive() and time.time() - t036 < 5,
          "36: join_threads_bounded 等待线程在时限内结束")
    th_slow36 = threading.Thread(target=time.sleep, args=(30,), daemon=True)
    th_slow36.start()
    t036 = time.time()
    join_threads_bounded([th_slow36], 0.4)
    check(th_slow36.is_alive() and 0.3 <= time.time() - t036 < 3,
          "36: join_threads_bounded 超时按界返回(不阻塞退出)")
    assert len(failures) == _fail_base, "test_36_refactor_regress: 本节有断言失败"


# ---------- 37. UI 审查修复回归: 托盘守卫/删除警示/气泡/版本常量 ----------
def test_37_ui_review_fixes():
    # type: () -> None
    """自测节 37. UI 审查修复: P2 弹窗守卫 / P3 删除警示+气泡+版本"""
    global _appmod37, _gd37, _tray37
    _fail_base = len(failures)
    print("[37] UI 审查修复回归")
    import inspect

    # fake tkinter 已由第 25 节注入 sys.modules，gui_app 可无头导入；
    # 25 节的 fake 树只覆盖 TaskDialog 所需子模块，此处补注入 gui_app
    # 顶部 from-import 还需要的 scrolledtext（仅导入期绑定，无需行为）
    _tk37 = sys.modules.get("tkinter")
    if _tk37 is not None and not hasattr(_tk37, "scrolledtext"):
        import types as _types37
        _st37 = _types37.ModuleType("tkinter.scrolledtext")
        _st37.ScrolledText = type(
            "ScrolledText", (object,),
            {"__init__": lambda self, *a, **k: None})
        _tk37.scrolledtext = _st37
        sys.modules["tkinter.scrolledtext"] = _st37
    import gui_app as _appmod37  # noqa: E402
    import gui_diff as _gd37
    import tray as _tray37
    import main as _main37

    # P2：_on_diff_ready 全路径经 _popup_if_alive（托盘隐藏态不弹不可见模态框）
    src_onready = inspect.getsource(_appmod37.App._on_diff_ready)
    check("messagebox." not in src_onready,
          "37: P2 _on_diff_ready 无直调 messagebox(统一走守卫)")
    check("_popup_if_alive" in src_onready,
          "37: P2 _on_diff_ready 使用 _popup_if_alive")

    # P3-1：删除警示纯函数（0 -> None；delete+rmdir 求和入文案）
    check(_gd37.deletion_warning_text(0, 0) is None,
          "37: 无删除动作不生成警示文案")
    txt = _gd37.deletion_warning_text(3)
    check(txt is not None and "3" in txt,
          "37: 纯删除计数生成含数量的警示文案")
    txt = _gd37.deletion_warning_text(2, 5)
    check(txt is not None and "7" in txt,
          "37: 文件与目录删除计数合并展示")

    # P3-2：托盘气泡——常量定义 + 未创建图标时 notify 静默返回
    check(_tray37.NIF_INFO == 0x10 and _tray37.NIIF_INFO == 0x1,
          "37: NIF_INFO/NIIF_INFO 常量为 SDK 定义值")
    dummy = _tray37.TrayIcon.__new__(_tray37.TrayIcon)
    dummy._nid = None  # type: ignore[attr-defined]
    try:
        dummy.notify("标题", "内容")  # type: ignore[attr-defined]
        check(True, "37: notify 在图标未就绪时静默返回不崩溃")
    except Exception as e:
        check(False, "37: notify 图标未就绪应静默: %s" % e)

    # P3-4：版本号单一来源（main 定义、gui_app 引用同一常量）
    check(getattr(_main37, "APP_VERSION", "") != "",
          "37: APP_VERSION 已在 main 定义且非空")
    check(_main37.APP_VERSION == _appmod37.APP_VERSION,
          "37: gui_app 与 main 引用同一版本常量(消除硬编码)")
    assert len(failures) == _fail_base, "test_37_ui_review_fixes: 本节有断言失败"


# ---------- 38. 结构断言: App 混入拆分 ----------
def test_38_app_split_structure():
    # type: () -> None
    """自测节 38. App 拆分为 SyncFlow/TrayMenu/CloseSeq 三混入"""
    global _gw38, _gt38, _gc38
    _fail_base = len(failures)
    print("[38] 结构: App 混入拆分")
    import gui_workers as _gw38
    import gui_tray as _gt38
    import gui_close as _gc38

    check(issubclass(_appmod37.App, (_gw38.SyncFlowMixin,
                                     _gt38.TrayMenuMixin,
                                     _gc38.CloseSeqMixin)),
          "38: App 继承三个职责混入")
    for m in ("_on_sync_now", "_on_diff_ready", "_apply_worker",
              "_show_wait", "_hide_wait", "_popup_if_alive"):
        check(hasattr(_gw38.SyncFlowMixin, m),
              "38: SyncFlowMixin.%s 在位" % m)
    for m in ("_build_menu", "_init_tray", "_hide_to_background",
              "_restore_from_tray", "_request_quit", "_on_unmap"):
        check(hasattr(_gt38.TrayMenuMixin, m),
              "38: TrayMenuMixin.%s 在位" % m)
    for m in ("on_close", "_finish_close", "_join_workers_bounded"):
        check(hasattr(_gc38.CloseSeqMixin, m),
              "38: CloseSeqMixin.%s 在位" % m)
    # 核心层保留项：UI 队列与常驻界面不随混人迁移
    for m in ("__init__", "_build_ui", "_refresh_tasks", "_ui_put",
              "_drain_ui_queue", "_tick", "_run_task"):
        check(m in _appmod37.App.__dict__,
              "38: App 核心层保留 %s" % m)
    # MRO 身份断言：跨混入方法调用必须解析到真实现，
    # 而非类型占位（类级赋值占位会按 MRO 遮蔽后位混入的方法）
    check(_appmod37.App.on_close is _gc38.CloseSeqMixin.on_close,
          "38: on_close 解析到 CloseSeqMixin 真实现")
    check(_appmod37.App._hide_to_background is _gt38.TrayMenuMixin._hide_to_background,
          "38: _hide_to_background 解析到 TrayMenuMixin 真实现")
    check(_appmod37.App._hide_wait is _gw38.SyncFlowMixin._hide_wait,
          "38: _hide_wait 解析到 SyncFlowMixin 真实现")
    assert len(failures) == _fail_base, "test_38_app_split_structure: 本节有断言失败"


# ---------- 节内助手（提升到模块级；调用发生在测试运行期） ----------
def _sched_run(task):
    # type: (Task) -> None
    try:
        perform_sync(task)
    finally:
        ran.append(task.id)

def blocking_run(task):
    # type: (Task) -> None
    started_evt.set()
    finish_evt.wait(timeout=10)   # 兜底超时，断言失败也不会卡死测试

def _saver():
    # type: () -> None
    for _ in range(50):
        store4.save()

def _cli(argv, app_dir):
    # type: (list, str) -> Tuple[int, str]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run_cli(list(argv), app_dir=app_dir)
    return rc, buf.getvalue()

def _boom(task):
    # type: (Task) -> None
    raise RuntimeError("模拟同步异常")

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

def _fake_showerror(*a, **k):
    _err_box_calls.append(a)

def _fake_askyesno(*a, **k):
    # 默认拒绝（保守）：目标目录不存在的确认框需测试显式覆写应答
    _ask_calls.append(a)
    return False

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

def _sched_run26(task):
    # type: (Task) -> None
    ran26.append(task.id)

def _boom_copy2(*a, **k):
    # type: (*object, **object) -> None
    raise OSError("模拟备份失败")

def _boom_docopy(fp, tp):
    # type: (str, str) -> None
    raise OSError("模拟覆盖失败")

def _mk_sched_task(sched_type, times, weekdays=None, last_run=None):
    # type: (str, list, Optional[list], Optional[float]) -> Task
    tsk = fresh_task(MODE_ONE_WAY, src, dst)
    tsk.schedule.enabled = True
    tsk.schedule.type = sched_type
    tsk.schedule.times = times
    if weekdays is not None:
        tsk.schedule.weekdays = weekdays
    tsk.last_run = last_run
    return tsk

def _err_scan32(directory, **kw):
    # type: (str, **Any) -> Dict[str, FileMeta]
    sink = kw.get("error_sink")
    if sink is not None:
        sink.append("模拟扫描错误: %s" % directory)
    return {}

def _ask_boom32(action):
    # type: (Any) -> str
    raise RuntimeError("询问通道故障")

def _ask_bad32(action):
    # type: (Any) -> str
    return "garbage-value"

def _ask_src32(action):
    # type: (Any) -> str
    return CONFLICT_SOURCE

def _boom32(task, **kw):
    # type: (Task, **Any) -> None
    raise RuntimeError("模拟同步异常")

class _FakeStore32(object):
    def __init__(self, tasks):
        # type: (list) -> None
        self.tasks = tasks

def _counting_hash34(path, chunk=1 << 20, cancel_event=None):
    # type: (str, int, Optional[Any]) -> Optional[str]
    _hash_calls[0] += 1
    return _orig_hash34(path, chunk=chunk, cancel_event=cancel_event)

def _boom_hash34(path, chunk=1 << 20, cancel_event=None):
    # type: (str, int, Optional[Any]) -> Optional[str]
    raise AssertionError("fast 模式不应读哈希: %s" % path)

def _fail_copy35(frm, to):
    if os.path.basename(to) == "f.txt":
        raise OSError("模拟复制失败")
    return _orig_copy35(frm, to)

class _FakeLog35l(object):
    def warn(self, m):
        # type: (str) -> None
        _got35l.append(m)


TESTS = [test_1, test_2, test_3_run_now, test_2b_D_mtime, test_3b, test_3c_interval_last_run, test_4, test_5_save, test_6_M4, test_7_2s, test_8_M10_baseline, test_9_L4, test_10_CLI_run_cli, test_11_S1_SE1, test_12_include, test_13_interval_next_run, test_14_run_now, test_15_daily, test_16_logger, test_17, test_18_config_baseline, test_19_GUI, test_20_utils_paths, test_21_utils_timeutil, test_22_scanner, test_23_config_Task, test_24_logger_close, test_25_GUI_mock, test_26_run_now_next_run, test_27_fail_count, test_28_mkdir_type_conflict, test_29_F4_interval, test_30_F5_daily, test_31_F6_baseline, test_32_C1_C2, test_33_tray_autostart, test_34_fast_FAT32, test_35_vs_skip, test_36_refactor_regress, test_37_ui_review_fixes, test_38_app_split_structure]

if __name__ == "__main__":
    import traceback
    for _fn in TESTS:
        try:
            _fn()
        except Exception:
            failures.append("%s 执行异常" % _fn.__name__)
            print("  FAIL- %s 执行异常:" % _fn.__name__)
            traceback.print_exc()
    print("\n结果：%s" % ("全部通过" if not failures else "%d 项失败" % len(failures)))
    if failures:
        for f in failures:
            print("  - %s" % f)
        sys.exit(1)
    print("自测通过。")

