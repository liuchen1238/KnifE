"""Cross-platform CPU/IO priority management.

Levels map to platform-appropriate values:
  REALTIME   — Windows: REALTIME_PRIORITY_CLASS, Unix: nice -20 (root)
  HIGH       — Windows: HIGH_PRIORITY_CLASS,     Unix: nice -10
  NORMAL     — Windows: NORMAL_PRIORITY_CLASS,   Unix: nice  0
  LOW        — Windows: BELOW_NORMAL_PRIORITY_CLASS, Unix: nice 10
  BACKGROUND — Windows: IDLE_PRIORITY_CLASS,     Unix: nice 19
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional

import psutil

from ..utils.platform import is_windows


class PriorityLevel(str, enum.Enum):
    REALTIME = "realtime"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"
    BACKGROUND = "background"


_UNIX_NICE = {
    PriorityLevel.REALTIME: -20,
    PriorityLevel.HIGH: -10,
    PriorityLevel.NORMAL: 0,
    PriorityLevel.LOW: 10,
    PriorityLevel.BACKGROUND: 19,
}


@dataclass
class PriorityResult:
    pid: int
    requested: PriorityLevel
    applied: bool
    note: str = ""


class PriorityManager:
    """Apply priority levels to running processes."""

    def set(self, pid: int, level: PriorityLevel) -> PriorityResult:
        if isinstance(level, str):
            level = PriorityLevel(level)
        try:
            proc = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return PriorityResult(pid, level, False, "process not found")

        if is_windows():
            # psutil exposes platform priority constants only on Windows.
            class_map = {
                PriorityLevel.REALTIME: getattr(psutil, "REALTIME_PRIORITY_CLASS", None),
                PriorityLevel.HIGH: getattr(psutil, "HIGH_PRIORITY_CLASS", None),
                PriorityLevel.NORMAL: getattr(psutil, "NORMAL_PRIORITY_CLASS", None),
                PriorityLevel.LOW: getattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS", None),
                PriorityLevel.BACKGROUND: getattr(psutil, "IDLE_PRIORITY_CLASS", None),
            }
            cls = class_map.get(level)
            if cls is None:
                return PriorityResult(pid, level, False, "priority class not available")
            try:
                proc.nice(cls)
                return PriorityResult(pid, level, True, f"set Windows priority class")
            except (psutil.AccessDenied, OSError) as e:
                return PriorityResult(pid, level, False, f"access denied: {e}")
        else:
            nice = _UNIX_NICE[level]
            try:
                proc.nice(nice)
            except (psutil.AccessDenied, OSError) as e:
                return PriorityResult(pid, level, False,
                                       f"nice {nice} requires elevated privileges: {e}")
            note = f"nice={nice}"
            # Try ionice on Linux for IO priority
            try:
                ionice_class = self._linux_ionice_class(level)
                if ionice_class is not None:
                    proc.ionice(ionice_class)  # type: ignore[attr-defined]
                    note += f", ionice={ionice_class}"
            except (psutil.AccessDenied, OSError, AttributeError, NotImplementedError):
                pass
            return PriorityResult(pid, level, True, note)

    def get(self, pid: int) -> Optional[int]:
        try:
            return psutil.Process(pid).nice()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None

    def _linux_ionice_class(self, level: PriorityLevel):
        # Lazily import to avoid attribute errors on non-Linux psutil builds.
        try:
            cls_realtime = psutil.IOPRIO_CLASS_RT  # type: ignore[attr-defined]
            cls_be = psutil.IOPRIO_CLASS_BE        # type: ignore[attr-defined]
            cls_idle = psutil.IOPRIO_CLASS_IDLE    # type: ignore[attr-defined]
        except AttributeError:
            return None
        return {
            PriorityLevel.REALTIME: cls_realtime,
            PriorityLevel.HIGH: cls_be,
            PriorityLevel.NORMAL: cls_be,
            PriorityLevel.LOW: cls_be,
            PriorityLevel.BACKGROUND: cls_idle,
        }.get(level)
