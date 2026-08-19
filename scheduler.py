"""内置常驻调度器。

- 后台守护线程每秒轮询，按各任务的下次触发时间（next_run）触发同步。
- 支持两种计划：interval（每隔 N 分钟）与 daily（每日固定时刻，可多个）。
- 防重叠：同一任务正在运行时不会被再次触发。
- run_now(task_id)：手动立即触发一次（无视定时，常用于 GUI "立即同步"）。
- 通过 status_callback 把 next_run 变化推给 GUI 刷新。
"""


import time
import threading
from typing import Any, Callable, List, Optional

from config import Task, SCHED_INTERVAL, SCHED_DAILY, SCHED_WEEKLY
from utils.timeutil import now_epoch, next_daily_times, next_weekly_times
from logger import get_logger


class Scheduler(object):
    def __init__(self, store, run_task, logger=None):
        # type: (Any, Callable[[Task], None], Any) -> None
        self.store = store
        self.run_task = run_task          # 执行单个任务的回调 task -> None
        self.logger = logger or get_logger()
        self._running = set()             # type: set
        self._lock = threading.Lock()
        self._stop = False
        self._thread = None               # type: Optional[threading.Thread]
        self._active = []                 # type: List[threading.Thread]
        self._gen = 0                     # 调度循环代际号，防止快速停止→启动出现双循环
        self.running = False
        self._status_cb = None            # type: Optional[Callable[[], None]]

    def set_status_callback(self, cb):
        # type: (Callable[[], None]) -> None
        self._status_cb = cb

    def start(self):
        # type: () -> None
        if self.running:
            return
        self._stop = False
        self._gen += 1
        self.running = True
        self._thread = threading.Thread(
            target=self._loop, args=(self._gen,), name="scheduler", daemon=True)
        self._thread.start()
        self.logger.info("调度器已启动")

    def stop(self):
        # type: () -> None
        # 短停：只置位 + 清 next_run + 短暂等待循环线程退出，不在 GUI 线程做长等待
        self._stop = True
        self.running = False
        for t in self.store.tasks:
            t.next_run = None
        th = self._thread
        if th is not None and th.is_alive():
            try:
                th.join(timeout=1)
            except Exception:
                pass
        self.logger.info("调度器已停止")

    def wait_workers(self, timeout=5):
        # type: (float) -> None
        """有界等待运行中的工作线程结束（仅供退出流程在后台线程调用）。"""
        deadline = time.time() + timeout
        with self._lock:
            threads = list(self._active)
        for th in threads:
            remain = deadline - time.time()
            if remain <= 0:
                break
            try:
                th.join(timeout=remain)
            except Exception:
                pass

    def is_task_running(self, task_id):
        # type: (str) -> bool
        with self._lock:
            return task_id in self._running

    def acquire(self, task_id):
        # type: (str) -> bool
        """占用任务运行槽，成功返回 True（与 _run_task 共用防重叠锁）。

        手动「立即同步」的 apply 阶段调用，避免与调度器并发写同一目录。
        """
        with self._lock:
            if task_id in self._running:
                return False
            self._running.add(task_id)
            return True

    def release(self, task_id):
        # type: (str) -> None
        with self._lock:
            self._running.discard(task_id)
        if self._status_cb:
            try:
                self._status_cb()
            except Exception:
                pass

    def _compute_next(self, task, from_epoch):
        # type: (Task, float) -> Optional[float]
        sched = task.schedule
        if not sched.enabled or not task.enabled:
            return None
        if sched.type == SCHED_INTERVAL:
            minutes = max(1, int(sched.interval_minutes))
            # 以"上次完成时间"为锚点。锚点已过期（停机错过/首次）则视为立即到期：
            # 补跑一次后 last_run 更新，自然回到正常周期。
            # 注意：不能直接返回 from_epoch + N，否则触发条件 now >= nxt 永不成立。
            base = task.last_run or from_epoch
            nxt = base + minutes * 60
            if nxt <= from_epoch:
                return from_epoch
            return nxt
        if sched.type == SCHED_DAILY:
            return next_daily_times(sched.times, from_epoch)
        if sched.type == SCHED_WEEKLY:
            return next_weekly_times(sched.weekdays, sched.times, from_epoch)
        return None

    def run_now(self, task_id):
        # type: (str) -> bool
        """手动触发一次。任务不存在 / 已禁用 / 正在运行则返回 False。"""
        task = self.store.get(task_id)
        if task is None:
            return False
        # README 承诺：禁用任务不参与定时，也不手动同步
        if not task.enabled:
            self.logger.info("任务[%s] 已禁用，run_now 被拒绝" % task.name)
            return False
        with self._lock:
            if task_id in self._running:
                return False
            self._running.add(task_id)
            t = threading.Thread(target=self._worker, args=(task_id,),
                                 name="run-%s" % task_id, daemon=True)
            self._active.append(t)   # 先登记再启动，避免线程早于登记结束的竞态
            t.start()
        return True

    def _worker(self, task_id):
        # type: (str) -> None
        cur = threading.current_thread()
        task = self.store.get(task_id)
        if task is None:
            with self._lock:
                self._running.discard(task_id)
                try:
                    self._active.remove(cur)
                except ValueError:
                    pass
            return
        try:
            self.run_task(task)
        except Exception as e:  # 保护调度线程
            self.logger.error("任务执行异常 [%s]: %s" % (task.name, e))
        finally:
            with self._lock:
                self._running.discard(task_id)
                try:
                    self._active.remove(cur)   # 自清理，防止 _active 无限增长
                except ValueError:
                    pass
            if self._status_cb:
                try:
                    self._status_cb()
                except Exception:
                    pass

    def _poll_once(self, now=None):
        # type: (Optional[float]) -> None
        if now is None:
            now = now_epoch()
        for task in self.store.tasks:
            if not (task.schedule.enabled and task.enabled):
                task.next_run = None
                continue
            # next_run 粘性化：只在未设置时计算，避免每秒重算导致触发点无限滑移
            if task.next_run is None:
                task.next_run = self._compute_next(task, now)
            nxt = task.next_run
            if nxt is not None and now >= nxt:
                started = False
                with self._lock:
                    if task.id not in self._running:
                        self._running.add(task.id)
                        t = threading.Thread(
                            target=self._worker, args=(task.id,),
                            name="sched-%s" % task.id, daemon=True)
                        self._active.append(t)
                        t.start()
                        started = True
                if started:
                    # 立即推进下次触发时间，避免本周期重复。
                    # interval 模式以"本次触发时刻"为锚点而非 last_run：
                    # 补跑场景（停机错过/首次）下 last_run 远早于 now，若仍
                    # 用 last_run 计算会得到过期时刻（now+1），导致任务完成后
                    # 立即再触发一次。锚定 now 后周期从触发时刻重新起算。
                    if task.schedule.type == SCHED_INTERVAL:
                        task.next_run = now + max(1, int(task.schedule.interval_minutes)) * 60
                    else:
                        task.next_run = self._compute_next(task, now + 1)
                else:
                    # 任务运行中：计划顺延重试而非丢弃（daily 一次性时刻尤其重要）
                    task.next_run = now + 60
                    self.logger.warn("任务[%s] 正在运行，计划顺延 60 秒" % task.name)
        if self._status_cb:
            try:
                self._status_cb()
            except Exception:
                pass

    def _loop(self, gen):
        # type: (int) -> None
        while not self._stop and gen == self._gen:
            try:
                self._poll_once()
            except Exception as e:
                # 任何异常都不能杀死调度线程（否则定时全部失效且界面仍显示"运行中"）
                try:
                    self.logger.error("调度循环异常: %s" % e)
                except Exception:
                    pass
            # 分段睡眠以便快速响应 stop / 重启
            for _ in range(10):
                if self._stop or gen != self._gen:
                    break
                time.sleep(0.1)
