"""Platform detection helpers."""
from __future__ import annotations

import sys


def is_windows() -> bool:
    return sys.platform.startswith("win")


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def platform_name() -> str:
    if is_windows():
        return "windows"
    if is_macos():
        return "macos"
    if is_linux():
        return "linux"
    return sys.platform
