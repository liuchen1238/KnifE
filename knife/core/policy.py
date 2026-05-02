"""Allow/block list policies — applied by name to running processes.

A policy contains an allow list and a block list (each is a list of
glob-like name patterns or absolute exe paths). The PolicyEnforcer
periodically scans running processes and applies the configured action
to anything that violates the policy.

Two enforcement modes:
  * ALLOW_LIST  — only listed names may run; everything else is acted on
  * BLOCK_LIST  — listed names cannot run; everything else is allowed
"""
from __future__ import annotations

import enum
import fnmatch
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Set

import psutil

log = logging.getLogger("knife.policy")


class PolicyMode(str, enum.Enum):
    BLOCK_LIST = "block"
    ALLOW_LIST = "allow"
    OFF = "off"


class PolicyAction(str, enum.Enum):
    WARN = "warn"
    SUSPEND = "suspend"
    TERMINATE = "terminate"
    KILL = "kill"


@dataclass
class Policy:
    mode: PolicyMode = PolicyMode.OFF
    action: PolicyAction = PolicyAction.WARN
    allow: List[str] = field(default_factory=list)
    block: List[str] = field(default_factory=list)
    # Names that should never be touched even if they match (safety net).
    protected: List[str] = field(default_factory=lambda: [
        "knife", "python", "pythonw", "python3", "systemd", "init",
        "launchd", "kernel_task", "WindowServer", "explorer.exe",
        "System", "Registry", "csrss.exe", "winlogon.exe",
    ])

    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "action": self.action.value,
            "allow": list(self.allow),
            "block": list(self.block),
            "protected": list(self.protected),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Policy":
        return cls(
            mode=PolicyMode(d.get("mode", "off")),
            action=PolicyAction(d.get("action", "warn")),
            allow=list(d.get("allow", [])),
            block=list(d.get("block", [])),
            protected=list(d.get("protected", [])) or cls().protected,
        )

    # ------------------------------------------------------------------
    def matches(self, name: str, patterns: List[str]) -> bool:
        n = (name or "").lower()
        for p in patterns:
            p_l = p.lower()
            if fnmatch.fnmatch(n, p_l) or p_l in n:
                return True
        return False

    def is_violation(self, name: str) -> bool:
        if self.mode == PolicyMode.OFF:
            return False
        if self.matches(name, self.protected):
            return False
        if self.mode == PolicyMode.BLOCK_LIST:
            return self.matches(name, self.block)
        if self.mode == PolicyMode.ALLOW_LIST:
            return not self.matches(name, self.allow)
        return False


class PolicyEnforcer:
    """Periodic scanner that applies the policy to live processes."""

    def __init__(self, policy: Policy, poll_interval: float = 2.0,
                 on_event: Optional[Callable[[str, dict], None]] = None) -> None:
        self.policy = policy
        self.poll_interval = poll_interval
        self.on_event = on_event
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._already_acted: Set[int] = set()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="knife-policy", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("policy enforcer tick failed")
            self._stop.wait(self.poll_interval)

    def _tick(self) -> None:
        if self.policy.mode == PolicyMode.OFF:
            return
        live = set()
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                pid = proc.info["pid"]
                name = proc.info.get("name") or ""
                live.add(pid)
                if not self.policy.is_violation(name):
                    continue
                if pid in self._already_acted and self.policy.action == PolicyAction.WARN:
                    continue
                self._enforce(proc, name)
                self._already_acted.add(pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        # GC
        self._already_acted &= live

    def _enforce(self, proc: psutil.Process, name: str) -> None:
        info = {"pid": proc.pid, "name": name, "action": self.policy.action.value}
        try:
            if self.policy.action == PolicyAction.WARN:
                self._emit("policy_violation", info)
            elif self.policy.action == PolicyAction.SUSPEND:
                proc.suspend()
                self._emit("policy_suspended", info)
            elif self.policy.action == PolicyAction.TERMINATE:
                proc.terminate()
                self._emit("policy_terminated", info)
            elif self.policy.action == PolicyAction.KILL:
                proc.kill()
                self._emit("policy_killed", info)
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            self._emit("policy_failed", {**info, "error": str(e)})

    def _emit(self, event: str, info: dict) -> None:
        log.info("%s pid=%s name=%s", event, info.get("pid"), info.get("name"))
        if self.on_event:
            try:
                self.on_event(event, info)
            except Exception:
                log.exception("on_event hook failed")
