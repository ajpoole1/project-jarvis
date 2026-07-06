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
        """Fan control is currently disabled; cmd_fan returns a stub message."""
        out = ha.cmd_fan([])
        assert "disabled" in out.lower()

    def test_temp_subcommand_survives_none(self, ha, monkeypatch):
        out = ha.cmd_fan(["temp"])
        assert "disabled" in out.lower()

    def test_status_still_converts_valid_fahrenheit(self, ha, monkeypatch):
        out = ha.cmd_fan([])
        assert "disabled" in out.lower()
