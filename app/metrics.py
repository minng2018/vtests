#!/usr/bin/env python3
"""Host memory and CPU sampling."""

from __future__ import annotations

import os
import threading


def meminfo() -> dict[str, int]:
    data: dict[str, int] = {}
    with open("/proc/meminfo", encoding="utf-8") as fh:
        for line in fh:
            parts = line.split()
            if len(parts) >= 2:
                data[parts[0].rstrip(":")] = int(parts[1])
    total = data.get("MemTotal", 0) // 1024
    avail = data.get("MemAvailable", data.get("MemFree", 0)) // 1024
    return {"total_mb": total, "avail_mb": avail, "used_mb": max(0, total - avail)}


def cpu_count() -> int:
    return os.cpu_count() or 1


class CpuSampler:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._prev: tuple[int, int] | None = None

    def _snap(self) -> tuple[int, int]:
        with open("/proc/stat", encoding="utf-8") as fh:
            parts = fh.readline().split()
        nums = [int(x) for x in parts[1:]]
        total = sum(nums)
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        return total, idle

    def percent(self) -> float:
        with self._lock:
            cur = self._snap()
            if self._prev is None:
                self._prev = cur
                return 0.0
            dt = cur[0] - self._prev[0]
            di = cur[1] - self._prev[1]
            self._prev = cur
            if dt <= 0:
                return 0.0
            return max(0.0, min(100.0, (1.0 - di / dt) * 100.0))
