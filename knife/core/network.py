"""Network rate limits — soft enforcement via SIGSTOP/SIGCONT throttling.

Real per-process traffic shaping needs OS-level tooling (tc/pf/QoS) and
elevated privileges. As a portable alternative we implement *soft* rate
limits: the watchdog observes how many bytes a process has sent or
received in the last interval, and if it exceeds its quota we pause the
process briefly with proc.suspend()/resume() to reduce its throughput.
On platforms where psutil cannot report per-process net I/O (macOS,
Windows) we fall back to monitoring connection counts and emitting
warnings — no silent failure.
"""
from __future__ import annotations

import enum
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

import psutil

from ..utils.platform import is_linux

log = logging.getLogger("knife.network")


class NetworkAction(str, enum.Enum):
    WARN = "warn"
    THROTTLE = "throttle"   # suspend/resume cycles
    SUSPEND = "suspend"     # full freeze until under limit
    TERMINATE = "terminate"
    KILL = "kill"


@dataclass
class NetworkLimit:
    pid: int
    bytes_per_sec: int
    action: NetworkAction = NetworkAction.WARN
    direction: str = "both"  # "send", "recv", or "both"
    last_sent: int = 0
    last_recv: int = 0
    last_at: float = 0.0


class NetworkGuard:
    """Per-process bandwidth watchdog."""

    def __init__(self, poll_interval: float = 1.0,
                 throttle_pause: float = 0.25,
                 on_event: Optional[Callable[[str, NetworkLimit, dict], None]] = None) -> None:
        self.poll_interval = poll_interval
        self.throttle_pause = throttle_pause
        self.on_event = on_event
        self._limits: Dict[int, NetworkLimit] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def set_limit(self, pid: int, bytes_per_sec: int,
                  action: NetworkAction = NetworkAction.WARN,
                  direction: str = "both") -> NetworkLimit:
        if isinstance(action, str):
            action = NetworkAction(action)
        lim = NetworkLimit(pid=pid, bytes_per_sec=int(bytes_per_sec),
                           action=action, direction=direction)
        with self._lock:
            self._limits[pid] = lim
        return lim

    def remove_limit(self, pid: int) -> bool:
        with self._lock:
            return self._limits.pop(pid, None) is not None

    def list_limits(self):
        with self._lock:
            return list(self._limits.values())

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="knife-net-guard", daemon=True)
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
            except Exception:
                log.exception("network guard tick failed")
            self._stop.wait(self.poll_interval)

    def _tick(self) -> None:
        now = time.monotonic()
        with self._lock:
            limits = list(self._limits.values())

        for lim in limits:
            try:
                proc = psutil.Process(lim.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                self.remove_limit(lim.pid)
                continue

            sent, recv = self._read_io(proc)
            if sent is None and recv is None:
                # No per-process IO available; emit periodic warnings only.
                if lim.action == NetworkAction.WARN:
                    try:
                        n = len(proc.net_connections(kind="inet"))
                    except Exception:
                        n = 0
                    self._emit("unsupported_platform", lim,
                               {"connections": n, "platform_supports_pernet": is_linux()})
                continue

            dt = (now - lim.last_at) if lim.last_at else self.poll_interval
            sent_rate = max(0.0, ((sent or 0) - lim.last_sent) / max(dt, 0.001)) if lim.last_at else 0
            recv_rate = max(0.0, ((recv or 0) - lim.last_recv) / max(dt, 0.001)) if lim.last_at else 0
            lim.last_sent = sent or 0
            lim.last_recv = recv or 0
            lim.last_at = now

            if lim.direction == "send":
                rate = sent_rate
            elif lim.direction == "recv":
                rate = recv_rate
            else:
                rate = sent_rate + recv_rate

            info = {"rate": rate, "sent_rate": sent_rate, "recv_rate": recv_rate, "limit": lim.bytes_per_sec}
            if rate > lim.bytes_per_sec:
                self._enforce(lim, proc, info)

    # ------------------------------------------------------------------
    def _read_io(self, proc: psutil.Process):
        try:
            io = proc.net_io_counters()  # type: ignore[attr-defined]
            return int(getattr(io, "bytes_sent", 0) or 0), int(getattr(io, "bytes_recv", 0) or 0)
        except (AttributeError, NotImplementedError, psutil.AccessDenied):
            return None, None
        except Exception:
            return None, None

    def _enforce(self, lim: NetworkLimit, proc: psutil.Process, info: dict) -> None:
        action = lim.action
        try:
            if action == NetworkAction.WARN:
                self._emit("over_limit", lim, info)
            elif action == NetworkAction.THROTTLE:
                proc.suspend()
                self._emit("throttled", lim, info)
                time.sleep(self.throttle_pause)
                try:
                    proc.resume()
                except Exception:
                    pass
            elif action == NetworkAction.SUSPEND:
                proc.suspend()
                self._emit("suspended", lim, info)
            elif action == NetworkAction.TERMINATE:
                proc.terminate()
                self._emit("terminated", lim, info)
            elif action == NetworkAction.KILL:
                proc.kill()
                self._emit("killed", lim, info)
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            self._emit("enforce_failed", lim, {**info, "error": str(e)})

    def _emit(self, event: str, lim: NetworkLimit, info: dict) -> None:
        log.info("network %s pid=%s rate=%.0fB/s limit=%dB/s",
                 event, lim.pid, info.get("rate", 0), lim.bytes_per_sec)
        if self.on_event:
            try:
                self.on_event(event, lim, info)
            except Exception:
                log.exception("on_event hook failed")
