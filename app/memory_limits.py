"""Inference RAM budgets. A mapped model is charged by resident pages, too."""
from dataclasses import dataclass
import math

import psutil

GIB = 1024 ** 3


def memory_settings(settings):
    values = settings if isinstance(settings, dict) else settings.values
    key, default, low, high = "cpu_memory_gb", 12, 1, 512
    value = values.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{key} 必须是 {low}–{high} GiB 之间的有效数值")
    return int(value * GIB)


@dataclass(frozen=True)
class MemoryBudget:
    limit_bytes: int
    resident_bytes: int

    def launch_kwargs(self):
        return {"memory_limit": self.limit_bytes, "working_set_limit": self.resident_bytes}


def budget_for(settings, *, max_bytes=None):
    limit = memory_settings(settings)
    if max_bytes is not None:
        limit = min(limit, max_bytes)
    available = psutil.virtual_memory().available
    resident = min(limit, available)
    if resident < 256 * 1024 ** 2:
        raise ValueError(f"内存保护：当前可用内存 {available / GIB:.2f} GiB，不足以启动模型。请先释放内存。")
    return MemoryBudget(limit, resident)
