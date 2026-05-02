import os

import pytest

from knife.core.monitor import Monitor


def test_monitor_returns_self():
    mon = Monitor()
    snap = mon.process(os.getpid())
    assert snap is not None
    assert snap.pid == os.getpid()
    assert snap.memory_bytes > 0


def test_monitor_sample_includes_self():
    mon = Monitor()
    snaps = mon.sample()
    pids = {s.pid for s in snaps}
    assert os.getpid() in pids


def test_monitor_find_by_name_returns_list():
    mon = Monitor()
    # search for python — we're running pytest via python so something matches
    matches = mon.find_by_name("python")
    assert isinstance(matches, list)
