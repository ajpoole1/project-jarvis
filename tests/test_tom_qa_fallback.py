"""Unit tests for the Tom QA Gemini/Claude fallback logic (scripts/dev-loop/tom_qa_helpers.py).

Covers:
- call_gemini_qa: success, retryable 503 exhaustion, network-error exhaustion,
  JSON parse retry + partial recovery, non-retryable error
- call_claude_fallback: success, single-attempt-only (no retry), failure shapes
- extract_json_object / extract_array / recover_partial_findings

A fake `requests` module (no real network) simulates a scripted sequence of
responses so retry/backoff paths can be exercised deterministically and fast.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_HELPERS_PATH = Path(__file__).parents[1] / "scripts" / "dev-loop" / "tom_qa_helpers.py"
_spec = importlib.util.spec_from_file_location("tom_qa_helpers", _HELPERS_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

call_gemini_qa = _mod.call_gemini_qa
call_claude_fallback = _mod.call_claude_fallback
extract_json_object = _mod.extract_json_object
extract_array = _mod.extract_array
recover_partial_findings = _mod.recover_partial_findings


_VALID_FINDINGS = {
    "spec_conformance": [],
    "defects": [],
    "architecture_notes": ["checked everything, looks fine"],
}


class _FakeResponse:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text

    def json(self):
        return self._json_body


class _FakeRequestException(Exception):
    pass


class _FakeExceptions:
    RequestException = _FakeRequestException


class _FakeRequests:
    """Scripted fake for the `requests` module's .post() + .exceptions surface.

    `script` is a list of either _FakeResponse instances or exception instances;
    each call to .post() pops the next entry. Raising entries are raised instead
    of returned. Sleeps are patched to no-ops via monkeypatching time.sleep in
    the helpers module (done per-test via monkeypatch fixture).
    """

    exceptions = _FakeExceptions

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _gemini_body(content_str):
    return {"choices": [{"message": {"content": content_str}}]}


def _claude_body(text_str):
    return {"content": [{"text": text_str}]}


# ---------------------------------------------------------------------------
# call_gemini_qa
# ---------------------------------------------------------------------------


def test_call_gemini_qa_success_first_try(monkeypatch):
    monkeypatch.setattr(_mod.time, "sleep", lambda *_: None)
    fake = _FakeRequests([_FakeResponse(200, _gemini_body(json.dumps(_VALID_FINDINGS)))])
    result = call_gemini_qa("key", "sys", "user", fake)
    assert result == _VALID_FINDINGS
    assert fake.calls == 1


def test_call_gemini_qa_retries_503_then_succeeds(monkeypatch):
    monkeypatch.setattr(_mod.time, "sleep", lambda *_: None)
    fake = _FakeRequests(
        [
            _FakeResponse(503, None, "unavailable"),
            _FakeResponse(503, None, "unavailable"),
            _FakeResponse(200, _gemini_body(json.dumps(_VALID_FINDINGS))),
        ]
    )
    result = call_gemini_qa("key", "sys", "user", fake)
    assert result == _VALID_FINDINGS
    assert fake.calls == 3


def test_call_gemini_qa_exhausts_503_retry_budget_returns_none(monkeypatch):
    monkeypatch.setattr(_mod.time, "sleep", lambda *_: None)
    fake = _FakeRequests([_FakeResponse(503, None, "unavailable") for _ in range(5)])
    result = call_gemini_qa("key", "sys", "user", fake)
    assert result is None
    assert fake.calls == 5


def test_call_gemini_qa_exhausts_network_error_budget_returns_none(monkeypatch):
    monkeypatch.setattr(_mod.time, "sleep", lambda *_: None)
    fake = _FakeRequests([_FakeRequestException("timed out") for _ in range(5)])
    result = call_gemini_qa("key", "sys", "user", fake)
    assert result is None
    assert fake.calls == 5


def test_call_gemini_qa_non_retryable_error_fails_fast(monkeypatch):
    monkeypatch.setattr(_mod.time, "sleep", lambda *_: None)
    fake = _FakeRequests([_FakeResponse(400, None, "bad request")])
    result = call_gemini_qa("key", "sys", "user", fake)
    assert result is None
    assert fake.calls == 1  # 400 is not in RETRY_STATUSES — no retry


def test_call_gemini_qa_bad_json_retries_full_call_then_succeeds(monkeypatch):
    monkeypatch.setattr(_mod.time, "sleep", lambda *_: None)
    fake = _FakeRequests(
        [
            _FakeResponse(200, _gemini_body("not json at all")),
            _FakeResponse(200, _gemini_body(json.dumps(_VALID_FINDINGS))),
        ]
    )
    result = call_gemini_qa("key", "sys", "user", fake)
    assert result == _VALID_FINDINGS
    assert fake.calls == 2


def test_call_gemini_qa_partial_recovery_on_truncated_json(monkeypatch):
    monkeypatch.setattr(_mod.time, "sleep", lambda *_: None)
    truncated = '{"spec_conformance": [], "defects": [{"type": "bug", "location": "x", "description": "y", "severity": "blocking"}], "architecture'
    fake = _FakeRequests([_FakeResponse(200, _gemini_body(truncated)) for _ in range(3)])
    result = call_gemini_qa("key", "sys", "user", fake)
    assert result is not None
    assert result["defects"][0]["severity"] == "blocking"
    assert "truncated" in result["architecture_notes"][0].lower()


def test_call_gemini_qa_unrecoverable_json_returns_none(monkeypatch):
    monkeypatch.setattr(_mod.time, "sleep", lambda *_: None)
    fake = _FakeRequests([_FakeResponse(200, _gemini_body("total garbage")) for _ in range(3)])
    result = call_gemini_qa("key", "sys", "user", fake)
    assert result is None


def test_call_gemini_qa_unexpected_response_shape_retries_then_fails(monkeypatch):
    monkeypatch.setattr(_mod.time, "sleep", lambda *_: None)
    fake = _FakeRequests([_FakeResponse(200, {"unexpected": "shape"}) for _ in range(3)])
    result = call_gemini_qa("key", "sys", "user", fake)
    assert result is None
    assert fake.calls == 3


# ---------------------------------------------------------------------------
# call_claude_fallback — single attempt, no retry loop
# ---------------------------------------------------------------------------


def test_call_claude_fallback_success():
    fake = _FakeRequests([_FakeResponse(200, _claude_body(json.dumps(_VALID_FINDINGS)))])
    result = call_claude_fallback("key", "sys", "user", fake)
    assert result == _VALID_FINDINGS
    assert fake.calls == 1


def test_call_claude_fallback_does_not_retry_on_503():
    """Unlike Gemini, Claude fallback is a single attempt — no retry loop.

    A 503 here should return None immediately, not retry, since the fallback
    is explicitly a one-shot resilience valve (DEV_LOOP_REFERENCE §3 inv #4).
    """
    fake = _FakeRequests([_FakeResponse(503, None, "unavailable")])
    result = call_claude_fallback("key", "sys", "user", fake)
    assert result is None
    assert fake.calls == 1


def test_call_claude_fallback_network_error_returns_none():
    fake = _FakeRequests([_FakeRequestException("connection reset")])
    result = call_claude_fallback("key", "sys", "user", fake)
    assert result is None


def test_call_claude_fallback_unexpected_shape_returns_none():
    fake = _FakeRequests([_FakeResponse(200, {"unexpected": "shape"})])
    result = call_claude_fallback("key", "sys", "user", fake)
    assert result is None


def test_call_claude_fallback_bad_json_attempts_partial_recovery():
    truncated = '{"spec_conformance": [], "defects": [], "architecture'
    fake = _FakeRequests([_FakeResponse(200, _claude_body(truncated))])
    result = call_claude_fallback("key", "sys", "user", fake)
    assert result is not None
    assert result["defects"] == []


# ---------------------------------------------------------------------------
# extract_json_object / extract_array / recover_partial_findings
# ---------------------------------------------------------------------------


def test_extract_json_object_clean():
    assert extract_json_object(json.dumps(_VALID_FINDINGS)) == _VALID_FINDINGS


def test_extract_json_object_with_surrounding_prose():
    raw = f"Here is the JSON:\n{json.dumps(_VALID_FINDINGS)}\nDone."
    assert extract_json_object(raw) == _VALID_FINDINGS


def test_extract_json_object_invalid_returns_none():
    assert extract_json_object("not json") is None


def test_extract_array_found():
    raw = '{"defects": [{"type": "bug"}], "other": 1}'
    assert extract_array(raw, "defects") == [{"type": "bug"}]


def test_extract_array_missing_key_returns_none():
    raw = '{"other": 1}'
    assert extract_array(raw, "defects") is None


def test_extract_array_unbalanced_brackets_returns_none():
    raw = '{"defects": [{"type": "bug"'  # truncated mid-object, never closes
    assert extract_array(raw, "defects") is None


def test_recover_partial_findings_both_present():
    raw = '{"spec_conformance": [], "defects": [{"type": "bug"}], "architecture_no'
    result = recover_partial_findings(raw)
    assert result is not None
    assert result["defects"] == [{"type": "bug"}]
    assert result["spec_conformance"] == []


def test_recover_partial_findings_missing_defects_returns_none():
    raw = '{"spec_conformance": []}'
    assert recover_partial_findings(raw) is None
