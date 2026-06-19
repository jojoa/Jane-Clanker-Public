from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone


def formatUptime(delta: timedelta) -> str:
    totalSec = max(0, int(delta.total_seconds()))
    days, rem = divmod(totalSec, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours:02d}h {minutes:02d}m {seconds:02d}s"
    return f"{hours:02d}h {minutes:02d}m {seconds:02d}s"


def discordTimestamp(value: datetime, style: str = "s") -> str:
    dt = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"<t:{int(dt.timestamp())}:{style}>"


def formatBytes(value: int | None) -> str:
    if value is None or value < 0:
        return "unavailable"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    unitIndex = 0
    while size >= 1024.0 and unitIndex < len(units) - 1:
        size /= 1024.0
        unitIndex += 1
    return f"{size:.2f} {units[unitIndex]}"


def getProcessRssBytes() -> int | None:
    # Windows: use GetProcessMemoryInfo from psapi.
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class _ProcessMemoryCounters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = _ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
            process = ctypes.windll.kernel32.GetCurrentProcess()
            ok = ctypes.windll.psapi.GetProcessMemoryInfo(
                process,
                ctypes.byref(counters),
                counters.cb,
            )
            if ok:
                return int(counters.WorkingSetSize)
        except Exception:
            return None
        return None

    try:
        import resource

        rss = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        if rss <= 0:
            return None
        if sysPlatformIsDarwin():
            return rss
        return rss * 1024
    except Exception:
        return None


def sysPlatformIsDarwin() -> bool:
    import sys

    return sys.platform == "darwin"


def getProcessResourceSnapshot(*, botStartedAt: datetime, nowUtc: datetime) -> dict[str, str]:
    uptimeSec = max((nowUtc - botStartedAt).total_seconds(), 1.0)
    cpuSec = max(time.process_time(), 0.0)
    cpuCount = max(int(os.cpu_count() or 1), 1)
    avgCpuPercent = (cpuSec / (uptimeSec * cpuCount)) * 100.0
    avgCpuPercent = max(0.0, min(avgCpuPercent, 999.9))

    return {
        "pid": str(os.getpid()),
        "threads": str(threading.active_count()),
        "rss": formatBytes(getProcessRssBytes()),
        "cpuPercent": f"{avgCpuPercent:.2f}%",
    }
