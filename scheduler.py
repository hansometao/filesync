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
from utils.timeutil import (
    now_epoch, next_daily_times, next_weekly_times,
    prev_daily_time, prev_weekly_time,
)
from logger import get_logger


def join_threads_bounded(threads, timeout):
    # type: (List[threading.Thread], float) -> None
    """有界等待一组线程结束：总时限 timeout 秒，逐个 join 剩余时间。

    供退出流程使用（调度 worker / 手动同步 worker），保证进程退出前
    写盘线程有机会完成，同时不无限阻塞 GUI。单个 join 抛异常按已结束
    处理，不中断后续等待。
    """
    deadline = time.time() + timeout
    for th in threads:
        remain = deadline - time.time()
        if remain <= 0:
            break
        try:
            th.join(timeout=remain)
        except Exception:
            pass


class Scheduler(object):
    def __init__(self, store, run_task, logger=None):
        # type: (Any, Callable[[Task], None], Any) -> None
        self.store = store
        self.run_task = run_task          # 执行单个任务的回调 task -> None
        self.logger = logger or get_logger()
        self._running = set()             # type: set
        self._lock = threading.Lock()
        self._next_lock = threading.Lock()  # 保护 next_run 读写（poll 线程 vs GUI 线程）
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
        # 短停：置位 + 清 next_run + 等待循环线程完全退出。
        # 不能用一次性 join(timeout=1)：poll 正被磁盘 I/O（store.save 等）阻塞时
        # 1 秒内退不出来，stop 返回后该次 _poll_once 仍可能启动新 worker，且可能
        # 逃逸 wait_workers 的快照（退出流程据此有界 join）——worker 被强杀留
        # 下半截文件。循环 join 直到线程真正退出（_loop 每 0.1s 检查 _stop，
        # 正常 0.2s 内退出；上限 3s 防止 GUI 线程被极端 I/O 卡死），
        # 配合 _poll_once 开头的 _stop 检查，确保 stop 返回后不再有 poll 在跑。
        self._stop = True
        self.running = False
        with self._next_lock:
            for t in self.store.tasks:
                t.next_run = None
        th = self._thread
        if th is not None and th.is_alive():
            deadline = time.time() + 3
            while th.is_alive() and time.time() < deadline:
                try:
                    th.join(timeout=0.2)
                except Exception:
                    break
        self.logger.info("调度器已停止")

    def wait_workers(self, timeout=5):
        # type: (float) -> None
        """有界等待运行中的工作线程结束（仅供退出流程在后台线程调用）。"""
        with self._lock:
            threads = list(self._active)
        join_threads_bounded(threads, timeout)

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
        # 审查修复：先登记 _running（_lock 内）再重置 next_run（_next_lock）。
        # 此前顺序相反：重置 next_run 与登记之间存在窗口，poll 读到 None 后
        # 按过期锚点算出"立即到期"并自行启动 worker（run_now 假返回 False，
        # 或与手动 worker 并存）。先登记后，poll 在 _lock 内发现任务已在
        # _running 便不再启动，窗口闭合；next_run 重置用于让 _poll_once 基于
        # 更新后的 last_run 重算（interval 锚定 last_run、daily/weekly 重算
        # 未来时刻），避免任务一完成即被调度器再触发一次。
        with self._lock:
            if task_id in self._running:
                return False
            self._running.add(task_id)
            t = threading.Thread(target=self._worker, args=(task_id,),
                                 name="run-%s" % task_id, daemon=True)
            self._active.append(t)   # 先登记再启动，避免线程早于登记结束的竞态
            try:
                t.start()
            except Exception as e:
                # 线程创建失败：回滚登记，否则任务永久显示"运行中"且无人收尾
                self._running.discard(task_id)
                try:
                    self._active.remove(t)
                except ValueError:
                    pass
                self.logger.error("任务[%s] 线程启动失败: %s" % (task.name, e))
                return False
        with self._next_lock:
            task.next_run = None
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

    def _missed_since_last_run(self, task, now):
        # type: (Task, float) -> bool
        """daily/weekly 计划在 last_run 之后是否有已错过未执行的时刻。

        interval 不需要此判定：_compute_next 已按 last_run 锚定，
        过期即视为立即到期（补跑）。+2 秒容差吸收完成时刻与计划时刻的
        浮点/秒级偏差，避免刚跑完又被判"错过"。
        """
        sched = task.schedule
        if task.last_run is None:
            # 从未运行过：与 interval 首跑行为一致，等下一个计划点，不补跑
            return False
        if sched.type == SCHED_DAILY:
            prev = prev_daily_time(sched.times, now)
        elif sched.type == SCHED_WEEKLY:
            prev = prev_weekly_time(sched.weekdays, sched.times, now)
        else:
            return False
        return prev is not None and prev > task.last_run + 2

    def _poll_once(self, now=None):
        # type: (Optional[float]) -> None
        if now is None:
            now = now_epoch()
        # 用快照迭代：直接遍历 store.tasks 时，GUI 线程并发 add/remove 会
        # 原地修改列表，抛 RuntimeError: list changed size during iteration
        # （当轮轮询中断，其后任务被跳过）
        for task in self.store.snapshot():
            try:
                self._poll_task(task, now)
            except Exception as e:
                # 单个任务的异常只跳过该任务：若不隔离，一个坏任务会让
                # _poll_once 每秒在循环中间抛出，排在它后面的所有任务
                # 永远得不到轮询（且界面仍显示调度器"运行中"）
                try:
                    self.logger.error("轮询任务[%s]异常: %s" % (task.name, e))
                except Exception:
                    pass
        if self._status_cb:
            try:
                self._status_cb()
            except Exception:
                pass

    def _poll_task(self, task, now):
        # type: (Task, float) -> None
        if not (task.schedule.enabled and task.enabled):
            with self._next_lock:
                task.next_run = None
            return
        # next_run 粘性化：只在未设置时计算，避免每秒重算导致触发点无限滑移。
        # next_run 由 poll 线程读写，stop()/run_now() 在 GUI 线程也会写它，
        # 用 _next_lock 保护（与 _lock 分两把锁且不嵌套，避免死锁）。
        with self._next_lock:
            if task.next_run is None:
                nxt = self._compute_next(task, now)
                # 停机补跑（仅冷启动分支）：daily/weekly 错过的计划时刻在下次
                # 启动时补跑一次（README 承诺）。不放 _compute_next —— 触发后的
                # 重算也会走它，配合运行中顺延会造成每次触发跑两遍。
                # 任务正在运行时不补（run_now 刚触发过，last_run 尚未更新）
                if (nxt is not None and task.id not in self._running
                        and self._missed_since_last_run(task, now)):
                    nxt = now
                task.next_run = nxt
            nxt = task.next_run
        if nxt is None or now < nxt:
            return
        started = False
        with self._lock:
            # enabled 复查在锁内进行：读 task.enabled 未持锁时，GUI 禁用与
            # poll 触发存在竞态窗口（禁用瞬间仍可能启动最后一次运行）
            if (task.id not in self._running
                    and task.enabled and task.schedule.enabled):
                self._running.add(task.id)
                t = threading.Thread(
                    target=self._worker, args=(task.id,),
                    name="sched-%s" % task.id, daemon=True)
                self._active.append(t)
                try:
                    t.start()
                    started = True
                except Exception as e:
                    # 线程创建失败：回滚登记，否则任务永久显示"运行中"且无人收尾
                    self._running.discard(task.id)
                    try:
                        self._active.remove(t)
                    except ValueError:
                        pass
                    self.logger.error("任务[%s] 线程启动失败: %s" % (task.name, e))
        with self._next_lock:
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
                # 任务运行中：计划顺延重试而非丢弃（daily 一次性时刻尤其重要）。
                # 顺延时长按计划类型区分：
                # - interval：周期任务无需在完成后再追跑一次，直接推到"此刻起
                #   一个完整周期后"，避免 run_now/到点时运行中造成的多余重跑
                #   （此前固定 +60s，interval 任务完成后 60 秒被自动再触发）；
                # - daily/weekly：重算**下一个未来计划点**而非滑动 now+60。
                #   此前 now+60 是滑动锚点：任务运行超过 60s 时每次轮询都把它
                #   推到更晚，任务完成后 next_run 已过期 -> "完成即重跑"；
                #   改为从此刻起找下一个计划点后，next_run 必在未来，
                #   长任务完成后不会立即再触发（错过的一次不补跑，与
                #   interval 的"运行中不追跑"哲学一致，也消除当日跑两遍）。
                if task.schedule.type == SCHED_INTERVAL:
                    task.next_run = now + max(1, int(task.schedule.interval_minutes)) * 60
                else:
                    task.next_run = self._compute_next(task, now + 1)
        if not started:
            self.logger.warn("任务[%s] 正在运行，计划顺延" % task.name)

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
