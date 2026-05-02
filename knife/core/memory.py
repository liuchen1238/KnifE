"""Memory limits — soft enforcement via watchdog.

True per-process hard memory limits depend on the OS:
  * Linux: setrlimit(RLIMIT_AS) / cgroups
  * macOS: setrlimit (limited)
  * Windows: Job Objects (SetInformationJobObject)

For a portable userspace tool we implement *soft* limits: a watchdog
samples each registered process and applies a configurable action when
it exceeds its quota — warn, suspend, terminate, or kill.
"""
from __future__ import annotations

import enum
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

import psutil

log = logging.getLogger("knife.memory")


class MemoryAction(str, enum.Enum):
    WARN = "warn"
    SUSPEND = "suspend"
    TERMINATE = "terminate"
    KILL = "kill"


@dataclass
class MemoryLimit:
    pid: int
    bytes_limit: int
    action: MemoryAction = MemoryAction.WARN
    grace_seconds: float = 5.0
    over_since: Optional[float] = None
    last_action_at: Optional[float] = None


class MemoryGuard:
    """Background watchdog enforcing soft memory limits.

    Example:
        guard = MemoryGuard()
        guard.set_limit(1234, "512MB", action="suspend")
        guard.start()
        ...
        guard.stop()
    """

    def __init__(self, poll_interval: float = 1.0,
                 on_event: Optional[Callable[[str, MemoryLimit, dict], None]] = None) -> None:
        self.poll_interval = poll_interval
        self.on_event = on_event
        self._limits: Dict[int, MemoryLimit] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Limit management
    # ------------------------------------------------------------------
    def set_limit(self, pid: int, bytes_limit: int,
                  action: MemoryAction = MemoryAction.WARN,
                  grace_seconds: float = 5.0) -> MemoryLimit:
        if isinstance(action, str):
            action = MemoryAction(action)
        lim = MemoryLimit(pid=pid, bytes_limit=int(bytes_limit),
                          action=action, grace_seconds=grace_seconds)
        with self._lock:
            self._limits[pid] = lim
        return lim

    def remove_limit(self, pid: int) -> bool:
        with self._lock:
            return self._limits.pop(pid, None) is not None

    def list_limits(self):
        with self._lock:
            return list(self._limits.values())

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="knife-memory-guard", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None

    # ------------------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:  # never let the watchdog die silently
                log.exception("memory guard tick failed: %s", e)
            self._stop.wait(self.poll_interval)

    def _tick(self) -> None:
        now = time.monotonic()
        with self._lock:
            limits = list(self._limits.values())

        for lim in limits:
            try:
                proc = psutil.Process(lim.pid)
                rss = proc.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                # Drop limits for dead processes
                self.remove_limit(lim.pid)
                continue

            usage_info = {"rss": rss, "limit": lim.bytes_limit, "pct": rss / max(1, lim.bytes_limit)}
            if rss > lim.bytes_limit:
                if lim.over_since is None:
                    lim.over_since = now
                    self._emit("over_limit", lim, usage_info)
                if (now - lim.over_since) >= lim.grace_seconds:
                    self._enforce(lim, proc, usage_info)
                    lim.last_action_at = now
                    lim.over_since = now  # reset grace window
            else:
                if lim.over_since is not None:
                    self._emit("recovered", lim, usage_info)
                lim.over_since = None

    # ------------------------------------------------------------------
    def _enforce(self, lim: MemoryLimit, proc: psutil.Process, info: dict) -> None:
        action = lim.action
        try:
            if action == MemoryAction.WARN:
                self._emit("warn", lim, info)
            elif action == MemoryAction.SUSPEND:
                proc.suspend()
                self._emit("suspended", lim, info)
            elif action == MemoryAction.TERMINATE:
                proc.terminate()
                self._emit("terminated", lim, info)
            elif action == MemoryAction.KILL:
                proc.kill()
                self._emit("killed", lim, info)
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            self._emit("enforce_failed", lim, {**info, "error": str(e)})

    def _emit(self, event: str, lim: MemoryLimit, info: dict) -> None:
        log.info("memory %s pid=%s rss=%s limit=%s", event, lim.pid, info.get("rss"), lim.bytes_limit)
        if self.on_event:
            try:
                self.on_event(event, lim, info)
            except Exception:
                log.exception("on_event hook failed")
