"""Regression tests for the home-assistant fan command.

Covers the graceful-degradation contract: when an HA entity reports no state
(``{"state": null}`` -> ``_state`` returns ``None``), the temperature float
conversion must not crash the fan command. Only ``ValueError`` was caught
originally, so ``float(None)`` raised an uncaught ``TypeError`` (Tom QA,
PR #87 blocking finding).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_HA_PATH = Path(__file__).parents[1] / "skills" / "home-assistant" / "skill.py"


def _load_ha(monkeypatch):
    monkeypatch.setenv("HA_URL", "http://ha.test:8123")
    monkeypatch.setenv("HA_TOKEN", "test-token")
    sys.modules.setdefault("dotenv", MagicMock())
    spec = importlib.util.spec_from_file_location("ha_skill", _HA_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def ha(monkeypatch):
    return _load_ha(monkeypatch)


class TestFanStatusNoneState:
    def test_status_survives_none_temp(self, ha, monkeypatch):
        """A None indoor/target temp (unavailable entity) must not crash."""
        monkeypatch.setattr(ha, "_state", lambda _entity: None)
        out = ha.cmd_fan([])
        assert "Fan status" in out
        # None falls through the conversion and is rendered as-is, not raised.
        assert "None°C" in out

    def test_temp_subcommand_survives_none(self, ha, monkeypatch):
        monkeypatch.setattr(ha, "_state", lambda _entity: None)
        out = ha.cmd_fan(["temp"])
        assert "indoor temp" in out.lower()

    def test_status_still_converts_valid_fahrenheit(self, ha, monkeypatch):
        """Valid numeric states are still converted F -> C."""
        monkeypatch.setattr(ha, "_state", lambda _entity: "68")
        out = ha.cmd_fan([])
        # 68F -> 20.0C
        assert "20.0°C" in out
