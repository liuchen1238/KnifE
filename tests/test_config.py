import json
from pathlib import Path

from knife.core.config import Config


def test_config_round_trip(tmp_path: Path):
    p = tmp_path / "config.json"
    c = Config(p)
    c.update("memory_limits", {"123": {"bytes": 1024, "action": "warn"}})
    c.save()
    assert p.exists()
    loaded = json.loads(p.read_text())
    assert loaded["memory_limits"]["123"]["bytes"] == 1024


def test_config_defaults(tmp_path: Path):
    p = tmp_path / "missing.json"
    c = Config(p)
    assert c.get("memory_limits") == {}
    assert c.get("policy")["mode"] == "off"
