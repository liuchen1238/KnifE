"""Persisted configuration — limits and policies survive across runs.

Config file location:
  $KNIFE_CONFIG (env var) — explicit override
  ~/.config/knife/config.json on Linux/macOS
  %APPDATA%/knife/config.json on Windows
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict


def default_config_path() -> Path:
    override = os.environ.get("KNIFE_CONFIG")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return Path(base) / "knife" / "config.json"
    return Path.home() / ".config" / "knife" / "config.json"


class Config:
    """Thread-safe JSON-backed config store."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else default_config_path()
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = self._load()

    # ------------------------------------------------------------------
    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {
                "memory_limits": {},   # pid_or_name -> {"bytes": int, "action": str}
                "network_limits": {},  # pid_or_name -> {"bps": int, "action": str, "direction": str}
                "priorities": {},      # pid_or_name -> level
                "policy": {"mode": "off", "action": "warn",
                            "allow": [], "block": [], "protected": []},
            }
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2, ensure_ascii=False, sort_keys=True)
            tmp.replace(self.path)

    # ------------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value

    def update(self, key: str, partial: Dict[str, Any]) -> None:
        with self._lock:
            existing = self._data.get(key, {})
            if not isinstance(existing, dict):
                existing = {}
            existing.update(partial)
            self._data[key] = existing

    def all(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data)
