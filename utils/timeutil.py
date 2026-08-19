"""时间工具：epoch 互转、FAT32 mtime 容差比较（兼容 Python 3.8）。

Windows 7 上 FAT32 文件系统的修改时间精度仅 2 秒，直接比较 mtime 会产生
误判。这里统一引入 2 秒容差，并在 content 判定上回退到 size/hash 比较。
"""

import time
from typing import Optional

# FAT32 时间精度（秒）。NTFS 为 100ns，但为兼容可移动磁盘统一取 2。
MTIME_TOLERANCE = 2.0


def now_epoch():
    """当前 epoch 秒（浮点）。"""
    return time.time()


def format_epoch(epoch, fmt="%Y-%m-%d %H:%M:%S"):
    """把 epoch 秒格式化为可读字符串；None 返回 '-'。"""
    if epoch is None:
        return "-"
    try:
        return time.strftime(fmt, time.localtime(epoch))
    except (ValueError, OSError):
        return "-"


def is_newer(ma, mb):
    """ma 是否比 mb 更新（考虑容差）。mb 为 None 视为 ma 更新。"""
    if ma is None:
        return False
    if mb is None:
        return True
    return float(ma) > float(mb) + MTIME_TOLERANCE


def next_daily_times(times, from_epoch):
    """计算每日定时（times 为 ['HH:MM', ...]）的下一次触发 epoch。

    返回距离 from_epoch 最近且 >= from_epoch 的那个时刻的 epoch；
    若当天剩余时刻都已过，则取次日最早时刻；若所有时刻都非法则返回 None。
    """
    if not times:
        return None
    struct = time.localtime(from_epoch)
    today = time.mktime(
        (struct.tm_year, struct.tm_mon, struct.tm_mday, 0, 0, 0, 0, 0, -1)
    )
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
    nexts = [tomorrow + _hms(t) for t in times if _hms(t) is not None]
    if not nexts:
        return None
    return min(nexts)


def _hms(t):
    try:
        hh, mm = t.split(":")
        return int(hh) * 3600 + int(mm) * 60
    except ValueError:
        return None
