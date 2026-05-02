"""Helpers for parsing/formatting size and rate units."""
from __future__ import annotations

import re

_SIZE_UNITS = {
    "b": 1,
    "k": 1024, "kb": 1024, "kib": 1024,
    "m": 1024 ** 2, "mb": 1024 ** 2, "mib": 1024 ** 2,
    "g": 1024 ** 3, "gb": 1024 ** 3, "gib": 1024 ** 3,
    "t": 1024 ** 4, "tb": 1024 ** 4, "tib": 1024 ** 4,
}


def parse_size(text: str) -> int:
    """Parse a human size like '512MB', '1.5G', '2048' into bytes."""
    if text is None:
        raise ValueError("size cannot be None")
    s = str(text).strip().lower().replace(" ", "")
    m = re.match(r"^([0-9]*\.?[0-9]+)([a-z]*)$", s)
    if not m:
        raise ValueError(f"cannot parse size: {text!r}")
    number = float(m.group(1))
    unit = m.group(2) or "b"
    if unit not in _SIZE_UNITS:
        raise ValueError(f"unknown size unit: {unit!r}")
    return int(number * _SIZE_UNITS[unit])


def parse_rate(text: str) -> int:
    """Parse a bandwidth like '10MB/s', '1.5Mbps', '500K' into bytes/second.

    Accepts:
      - Foo/s, Foo/sec  -> bytes/sec
      - Foobps, Foobits -> bits/sec (converted to bytes/sec)
      - Foo (no suffix) -> bytes/sec
    """
    if text is None:
        raise ValueError("rate cannot be None")
    s = str(text).strip().lower().replace(" ", "")
    # Strip /s suffix
    if s.endswith("/s"):
        s = s[:-2]
    elif s.endswith("/sec"):
        s = s[:-4]
    bits_mode = False
    if s.endswith("bps") or s.endswith("bit") or s.endswith("bits"):
        bits_mode = True
        s = s.rstrip("s").rstrip("t").rstrip("i").rstrip("b").rstrip("p")
        # collapse trailing letters until we hit alpha unit
    # Re-parse number + unit
    m = re.match(r"^([0-9]*\.?[0-9]+)([a-z]*)$", s)
    if not m:
        raise ValueError(f"cannot parse rate: {text!r}")
    number = float(m.group(1))
    unit = m.group(2) or "b"
    if unit not in _SIZE_UNITS:
        raise ValueError(f"unknown rate unit: {unit!r}")
    value = number * _SIZE_UNITS[unit]
    if bits_mode:
        value = value / 8.0
    return int(value)


def format_size(num_bytes: float) -> str:
    """Format bytes as human readable."""
    if num_bytes is None:
        return "n/a"
    n = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024.0:
            return f"{n:0.1f} {unit}"
        n /= 1024.0
    return f"{n:0.1f} EB"


def format_rate(num_bytes_per_sec: float) -> str:
    return f"{format_size(num_bytes_per_sec)}/s"
