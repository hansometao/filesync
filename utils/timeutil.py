"""时间工具：epoch 互转、FAT32 mtime 容差比较（兼容 Python 3.8）。

Windows 7 上 FAT32 文件系统的修改时间精度仅 2 秒，直接比较 mtime 会产生
误判。这里统一引入 2 秒容差，并在 content 判定上回退到 size/hash 比较。
"""

import time
from typing import List, Optional

# FAT32 时间精度（秒）。NTFS 为 100ns，但为兼容可移动磁盘统一取 2。
MTIME_TOLERANCE = 2.0


def now_epoch():
    # type: () -> float
    """当前 epoch 秒（浮点）。"""
    return time.time()


def format_epoch(epoch, fmt="%Y-%m-%d %H:%M:%S"):
    # type: (Optional[float], str) -> str
    """把 epoch 秒格式化为可读字符串；None 返回 '-'。"""
    if epoch is None:
        return "-"
    try:
        return time.strftime(fmt, time.localtime(epoch))
    except (ValueError, OSError):
        return "-"


def is_newer(ma, mb):
    # type: (Optional[float], Optional[float]) -> bool
    """ma 是否比 mb 更新（考虑容差）。mb 为 None 视为 ma 更新。"""
    if ma is None:
        return False
    if mb is None:
        return True
    return float(ma) > float(mb) + MTIME_TOLERANCE


def _midnight(epoch):
    # type: (float) -> float
    """epoch 所在当天的 00:00（本地时间）对应的 epoch。"""
    struct = time.localtime(epoch)
    return time.mktime(
        (struct.tm_year, struct.tm_mon, struct.tm_mday, 0, 0, 0, 0, 0, -1)
    )


def next_daily_times(times, from_epoch):
    # type: (List[str], float) -> Optional[float]
    """计算每日定时（times 为 ['HH:MM', ...]）的下一次触发 epoch。

    返回距离 from_epoch 最近且 >= from_epoch 的那个时刻的 epoch；
    若当天剩余时刻都已过，则取次日最早时刻；若所有时刻都非法则返回 None。
    """
    if not times:
        return None
    today = _midnight(from_epoch)
    candidates = []
    for t in times:
        sec = _hms(t)
        if sec is not None:
            candidates.append(today + sec)
    candidates.sort()
    for cand in candidates:
        if cand >= from_epoch:  # 不返回过去时刻（避免触发已过去的 1s 候选）
            return cand
    # 全部已过，取次日最早；无合法候选返回 None（由调用方按未配置处理）
    tomorrow = today + 86400
    nexts = []
    for t in times:
        s = _hms(t)
        if s is not None:
            nexts.append(tomorrow + s)
    if not nexts:
        return None
    return min(nexts)


def prev_daily_time(times, before_epoch):
    # type: (List[str], float) -> Optional[float]
    """<= before_epoch 的最近一个每日触发点（今天已过的或昨天的）。

    用于调度器判断"停机期间是否错过了计划时刻"（补跑判定）；
    times 为空或全部非法返回 None。DST 切换日按 86400 平移可能有 1 小时偏差，
    仅影响补跑判定的边界，不影响正常触发。
    """
    if not times:
        return None
    best = None  # type: Optional[float]
    today = _midnight(before_epoch)
    for day_offset in (0, 1):  # 今天与昨天：覆盖"今天还没到第一个时刻"的情况
        day0 = today - day_offset * 86400
        for t in times:
            sec = _hms(t)
            if sec is None:
                continue
            cand = day0 + sec
            if cand <= before_epoch and (best is None or cand > best):
                best = cand
    return best


def next_weekly_times(weekdays, times, from_epoch):
    # type: (List[int], List[str], float) -> Optional[float]
    """计算每周定时（weekdays 为 [1..7]，1=周一…7=周日）的下一次触发 epoch。

    在选中的每个星期几 × 每个时刻组合中，取 >= from_epoch 的最近触发点；
    本周组合全部已过则取下周最早组合；weekdays/times 空或全部非法返回 None。
    """
    if not weekdays or not times:
        return None
    wds = sorted(int(w) for w in weekdays if 1 <= int(w) <= 7)
    if not wds:
        return None
    secs = []
    for t in times:
        s = _hms(t)
        if s is not None:
            secs.append(s)
    secs.sort()
    if not secs:
        return None
    struct = time.localtime(from_epoch)
    # 本周一 00:00（tm_wday: 0=周一…6=周日）
    monday = time.mktime(
        (struct.tm_year, struct.tm_mon, struct.tm_mday, 0, 0, 0, 0, 0, -1)
    ) - struct.tm_wday * 86400
    candidates = []
    for wd in wds:
        dow = wd - 1  # 配置 1..7 -> 内部 0..6
        # 绝对基准：本周该周几的 00:00（周一周一 00:00 + dow 天）
        day0 = monday + dow * 86400
        for s in secs:
            cand = day0 + s
            # 本周该组合已过（含 from_epoch 本身早于该时刻的情况）-> 顺延到下周
            while cand < from_epoch:
                cand += 7 * 86400
            candidates.append(cand)
    if not candidates:
        # 理论不可达（wds/secs 非空必有未来组合），防御性兜底
        return None
    return min(candidates)


def prev_weekly_time(weekdays, times, before_epoch):
    # type: (List[int], List[str], float) -> Optional[float]
    """<= before_epoch 的最近一个每周触发点（今天与过去 7 天内扫描）。

    与 prev_daily_time 同理，用于 weekly 计划的停机补跑判定；
    weekdays/times 为空或全部非法返回 None。
    """
    if not weekdays or not times:
        return None
    wds = set(int(w) for w in weekdays if 1 <= int(w) <= 7)
    secs = [s for s in (_hms(t) for t in times) if s is not None]
    if not wds or not secs:
        return None
    best = None  # type: Optional[float]
    m0 = _midnight(before_epoch)
    for day_offset in range(8):  # 今天 + 过去 7 天必覆盖最近一个选中周几
        day0 = m0 - day_offset * 86400
        st = time.localtime(day0)
        if (st.tm_wday + 1) not in wds:  # tm_wday: 0=周一…6=周日 -> 1..7
            continue
        for s in secs:
            cand = day0 + s
            if cand <= before_epoch and (best is None or cand > best):
                best = cand
    return best


def _hms(t):
    # type: (str) -> Optional[int]
    try:
        hh, mm = t.split(":")
        return int(hh) * 3600 + int(mm) * 60
    except ValueError:
        return None


def unique_stamp():
    # type: () -> str
    """唯一时间戳（秒 + 毫秒三位），用于冲突备份 / 损坏配置副本命名防覆盖。"""
    return time.strftime("%Y%m%d-%H%M%S") + (".%03d" % int((time.time() % 1) * 1000))
