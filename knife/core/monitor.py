"""Process monitoring using psutil.

Provides a Monitor that produces ProcessSnapshot objects with CPU, memory,
and (best-effort) per-process network I/O. Per-process bandwidth tracking
relies on psutil's net_io_counters where available, otherwise it estimates
from connection deltas and falls back to system-wide stats.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

import psutil


@dataclass
class ProcessSnapshot:
    pid: int
    name: str
    username: Optional[str] = None
    cpu_percent: float = 0.0
    memory_bytes: int = 0
    memory_percent: float = 0.0
    num_threads: int = 0
    status: str = ""
    nice: Optional[int] = None
    create_time: float = 0.0
    exe: str = ""
    cmdline: List[str] = field(default_factory=list)
    # Network — bytes sent / received since last sample (delta).
    net_sent_per_sec: float = 0.0
    net_recv_per_sec: float = 0.0
    num_connections: int = 0
    # Cumulative byte counters since the process was first observed.
    net_sent_total: int = 0
    net_recv_total: int = 0


class Monitor:
    """Polls running processes and reports ProcessSnapshot objects.

    Usage:
        m = Monitor()
        snaps = m.sample()           # list of ProcessSnapshot
        snap = m.process(1234)       # single PID
    """

    def __init__(self) -> None:
        self._last_sample_at: Optional[float] = None
        self._last_net_io: Dict[int, tuple] = {}  # pid -> (sent, recv)
        # Prime cpu_percent by issuing a zero call against all procs.
        for p in psutil.process_iter():
            try:
                p.cpu_percent(interval=None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    # ------------------------------------------------------------------
    # Sampling
    # ------------------------------------------------------------------
    def sample(self) -> List[ProcessSnapshot]:
        now = time.monotonic()
        dt = (now - self._last_sample_at) if self._last_sample_at else 1.0
        self._last_sample_at = now

        snaps: List[ProcessSnapshot] = []
        for proc in psutil.process_iter([
            "pid", "name", "username", "memory_info", "memory_percent",
            "num_threads", "status", "nice", "create_time", "exe", "cmdline",
        ]):
            try:
                info = proc.info
                snap = ProcessSnapshot(
                    pid=info["pid"],
                    name=info.get("name") or "",
                    username=info.get("username"),
                    cpu_percent=proc.cpu_percent(interval=None),
                    memory_bytes=int(getattr(info.get("memory_info"), "rss", 0) or 0),
                    memory_percent=float(info.get("memory_percent") or 0.0),
                    num_threads=int(info.get("num_threads") or 0),
                    status=str(info.get("status") or ""),
                    nice=info.get("nice"),
                    create_time=float(info.get("create_time") or 0.0),
                    exe=info.get("exe") or "",
                    cmdline=list(info.get("cmdline") or []),
                )
                # Connections (cheap on most platforms).
                try:
                    snap.num_connections = len(proc.net_connections(kind="inet"))
                except (psutil.AccessDenied, NotImplementedError, AttributeError):
                    snap.num_connections = 0
                # Per-process net I/O — only available on Linux; psutil raises
                # NotImplementedError elsewhere. We surface zeros gracefully.
                self._fill_net_io(proc, snap, dt)
                snaps.append(snap)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return snaps

    def process(self, pid: int) -> Optional[ProcessSnapshot]:
        try:
            proc = psutil.Process(pid)
            proc.cpu_percent(interval=None)
            time.sleep(0.05)
            mem = proc.memory_info()
            snap = ProcessSnapshot(
                pid=proc.pid,
                name=proc.name(),
                username=_safe(proc.username),
                cpu_percent=proc.cpu_percent(interval=None),
                memory_bytes=int(getattr(mem, "rss", 0) or 0),
                memory_percent=float(proc.memory_percent() or 0.0),
                num_threads=proc.num_threads(),
                status=str(proc.status()),
                nice=_safe(proc.nice),
                create_time=proc.create_time(),
                exe=_safe(proc.exe) or "",
                cmdline=list(_safe(proc.cmdline) or []),
            )
            try:
                snap.num_connections = len(proc.net_connections(kind="inet"))
            except (psutil.AccessDenied, NotImplementedError, AttributeError):
                pass
            self._fill_net_io(proc, snap, 1.0)
            return snap
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def find_by_name(self, name: str) -> List[ProcessSnapshot]:
        name_l = name.lower()
        return [s for s in self.sample()
                if name_l in (s.name or "").lower()
                or any(name_l in c.lower() for c in s.cmdline)]

    def total_memory(self) -> int:
        return psutil.virtual_memory().total

    def system_net_io(self) -> Dict[str, int]:
        io = psutil.net_io_counters()
        return {
            "bytes_sent": io.bytes_sent,
            "bytes_recv": io.bytes_recv,
            "packets_sent": io.packets_sent,
            "packets_recv": io.packets_recv,
        }

    # ------------------------------------------------------------------
    def _fill_net_io(self, proc: psutil.Process, snap: ProcessSnapshot, dt: float) -> None:
        try:
            # Available on Linux; raises elsewhere.
            io = proc.net_io_counters()  # type: ignore[attr-defined]
        except (AttributeError, NotImplementedError, psutil.AccessDenied):
            return
        except Exception:
            return
        sent = int(getattr(io, "bytes_sent", 0) or 0)
        recv = int(getattr(io, "bytes_recv", 0) or 0)
        snap.net_sent_total = sent
        snap.net_recv_total = recv
        prev = self._last_net_io.get(proc.pid)
        if prev and dt > 0:
            snap.net_sent_per_sec = max(0.0, (sent - prev[0]) / dt)
            snap.net_recv_per_sec = max(0.0, (recv - prev[1]) / dt)
        self._last_net_io[proc.pid] = (sent, recv)


def _safe(fn):
    try:
        return fn()
    except Exception:
        return None
