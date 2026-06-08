"""Tests for 2026-0004-loop-hardening: run_git stderr guard and Tom retry logic."""

from __future__ import annotations

import importlib.util
import time
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Import skills/devqueue/skill.py without running main()
# ---------------------------------------------------------------------------

_SKILL_PATH = Path(__file__).parents[1] / "skills" / "devqueue" / "skill.py"
_spec = importlib.util.spec_from_file_location("devqueue_skill", _SKILL_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

PROJECT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Fix 4b: run_git capture=False failure → RuntimeError, not AttributeError
# ---------------------------------------------------------------------------


def test_run_git_capture_false_failure_raises_runtime_error():
    """run_git(capture=False) on a failing command raises RuntimeError, never AttributeError."""
    with pytest.raises(RuntimeError) as exc_info:
        _mod.run_git(
            "rev-parse",
            "--verify",
            "nonexistent-branch-xyz-devqueue-test",
            cwd=PROJECT,
            capture=False,
        )
    err = str(exc_info.value)
    assert "failed" in err
    assert "AttributeError" not in err


def test_run_git_capture_true_failure_includes_stderr():
    """run_git(capture=True) includes stderr in RuntimeError message."""
    with pytest.raises(RuntimeError) as exc_info:
        _mod.run_git(
            "rev-parse",
            "--verify",
            "nonexistent-branch-xyz-devqueue-test",
            cwd=PROJECT,
        )
    assert "failed" in str(exc_info.value)


def test_run_git_capture_false_error_message_not_captured():
    """run_git(capture=False) failure message says stderr was not captured."""
    with pytest.raises(RuntimeError) as exc_info:
        _mod.run_git(
            "rev-parse",
            "--verify",
            "nonexistent-branch-xyz-devqueue-test",
            cwd=PROJECT,
            capture=False,
        )
    assert "not captured" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Fix 2: Tom retry helper
#
# The retry logic lives inline in qa.yml. Mirror it here so it can be
# unit-tested without running the full workflow.
# ---------------------------------------------------------------------------


def _tom_post_with_retry(post_fn, payload, headers, url, max_attempts=3, base_delay=2):
    """
    Retry wrapper mirroring the logic in qa.yml's 'Run Tom' step.
    Retries on 503/429 with exponential backoff; fails closed on persistent errors.
    """
    RETRY_STATUSES = {429, 503}
    resp = None
    for attempt in range(1, max_attempts + 1):
        resp = post_fn(url, headers=headers, json=payload, timeout=120)
        if resp.status_code not in RETRY_STATUSES:
            break
        delay = base_delay * (2 ** (attempt - 1))
        if attempt < max_attempts:
            time.sleep(delay)
    return resp


def _make_mock_resp(status_code, text="ok"):
    r = MagicMock()
    r.status_code = status_code
    r.text = text
    return r


def test_tom_retry_success_on_first_attempt():
    post_fn = MagicMock(return_value=_make_mock_resp(200))
    resp = _tom_post_with_retry(post_fn, {}, {}, "http://fake")
    assert resp.status_code == 200
    assert post_fn.call_count == 1


@patch("time.sleep")
def test_tom_retry_503_then_success(mock_sleep):
    post_fn = MagicMock(side_effect=[_make_mock_resp(503), _make_mock_resp(200)])
    resp = _tom_post_with_retry(post_fn, {}, {}, "http://fake")
    assert resp.status_code == 200
    assert post_fn.call_count == 2
    mock_sleep.assert_called_once_with(2)


@patch("time.sleep")
def test_tom_retry_429_then_success(mock_sleep):
    post_fn = MagicMock(side_effect=[_make_mock_resp(429), _make_mock_resp(200)])
    resp = _tom_post_with_retry(post_fn, {}, {}, "http://fake")
    assert resp.status_code == 200
    assert post_fn.call_count == 2
    mock_sleep.assert_called_once_with(2)


@patch("time.sleep")
def test_tom_retry_persistent_503_fails_closed(mock_sleep):
    """Persistent 503 across all attempts returns 503 (caller then exits 1)."""
    post_fn = MagicMock(return_value=_make_mock_resp(503))
    resp = _tom_post_with_retry(post_fn, {}, {}, "http://fake", max_attempts=3, base_delay=2)
    assert resp.status_code == 503
    assert post_fn.call_count == 3
    # Two sleeps between 3 attempts: 2s then 4s
    assert mock_sleep.call_count == 2
    assert mock_sleep.call_args_list == [call(2), call(4)]


@patch("time.sleep")
def test_tom_retry_non_transient_error_not_retried(mock_sleep):
    """A 500 (not in retry set) is returned immediately without retry."""
    post_fn = MagicMock(return_value=_make_mock_resp(500))
    resp = _tom_post_with_retry(post_fn, {}, {}, "http://fake")
    assert resp.status_code == 500
    assert post_fn.call_count == 1
    mock_sleep.assert_not_called()


@patch("time.sleep")
def test_tom_retry_200_not_retried(mock_sleep):
    post_fn = MagicMock(return_value=_make_mock_resp(200))
    resp = _tom_post_with_retry(post_fn, {}, {}, "http://fake")
    assert resp.status_code == 200
    assert post_fn.call_count == 1
    mock_sleep.assert_not_called()


@patch("time.sleep")
def test_tom_retry_exponential_delays(mock_sleep):
    """Delays are 2^(attempt-1) * base: 2s, 4s for base=2, max_attempts=3."""
    post_fn = MagicMock(
        side_effect=[
            _make_mock_resp(503),
            _make_mock_resp(503),
            _make_mock_resp(200),
        ]
    )
    resp = _tom_post_with_retry(post_fn, {}, {}, "http://fake", max_attempts=3, base_delay=2)
    assert resp.status_code == 200
    assert mock_sleep.call_args_list == [call(2), call(4)]
