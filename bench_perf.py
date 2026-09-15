# type: ignore
# -*- coding: utf-8 -*-
"""性能基线/对比实测：大规模目录的 scan / diff / 哈希 / baseline 重建。

用法: python bench_perf.py [标签]
产物目录在系统临时目录下自动创建/复用，测试后保留供对比轮次复用。
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scanner import scan, hash_file
from sync_engine import diff, perform_sync, build_baseline_after
from config import Task

N_FILES = 10000
N_CHANGED = 200


def make_tree(root, n, seed_offset=0, change=0):
    os.makedirs(root, exist_ok=True)
    for i in range(n):
        d = os.path.join(root, "d%02d" % (i % 100))
        if not os.path.isdir(d):
            os.makedirs(d)
        # 4KB 文件
        content = b"x" * 4096 + ("%d" % (i + seed_offset)).encode()
        if change and i < change:
            content += b"CHANGED"
        with open(os.path.join(d, "f%05d.txt" % i), "wb") as f:
            f.write(content)


def bench(label):
    base = tempfile.mkdtemp(prefix="fsbench_")
    src = os.path.join(base, "src")
    dst = os.path.join(base, "dst")
    if not os.path.isdir(src):
        t0 = time.time()
        make_tree(src, N_FILES)
        print("[%s] 造数 %d 文件: %.2fs" % (label, N_FILES, time.time() - t0))
    else:
        print("[%s] 复用既有造数" % label)

    task = Task(name="bench", source=src, target=dst, mode="two_way")

    # 1. 首次同步（全量 copy + baseline 全量哈希）
    t0 = time.time()
    res = perform_sync(task)
    t_first = time.time() - t0

    # 2. 无变更轮（热路径：扫描+diff+baseline 复用）
    t0 = time.time()
    res = perform_sync(task)
    t_noop = time.time() - t0

    # 3. 少量变更轮（200 文件修改）
    t0 = time.time()
    # 直接改源文件内容与 mtime
    n = 0
    for i in range(N_FILES):
        if n >= N_CHANGED:
            break
        d = os.path.join(src, "d%02d" % (i % 100))
        p = os.path.join(d, "f%05d.txt" % i)
        with open(p, "ab") as f:
            f.write(b"MOD")
        n += 1
    t_edit = time.time() - t0
    t0 = time.time()
    res = perform_sync(task)
    t_sync_changed = time.time() - t0

    # 4. 单独扫描耗时（含递归+stat 1 万条目）
    t0 = time.time()
    snap = scan(src)
    t_scan = time.time() - t0

    print("[%s] 首次全量同步(含baseline哈希): %.2fs" % (label, t_first))
    print("[%s] 无变更轮(热路径): %.3fs  (changed=%s)" % (label, t_noop, res.get("changed")))
    print("[%s] 编辑 %d 文件: %.2fs" % (label, N_CHANGED, t_edit))
    print("[%s] 变更轮同步(200 modified): %.2fs" % (label, t_sync_changed))
    print("[%s] 纯扫描 1 万条目: %.3fs" % (label, t_scan))
    return base


if __name__ == "__main__":
    label = sys.argv[1] if len(sys.argv) > 1 else "base"
    bench(label)
