"""Unit tests for Tom QA threshold / skip logic (scripts/dev-loop/tom_qa_helpers.py)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_HELPERS_PATH = Path(__file__).parents[1] / "scripts" / "dev-loop" / "tom_qa_helpers.py"
_spec = importlib.util.spec_from_file_location("tom_qa_helpers", _HELPERS_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

count_changed_lines = _mod.count_changed_lines
make_skip_findings = _mod.make_skip_findings
parse_min_diff_lines = _mod.parse_min_diff_lines

_SAMPLE_DIFF = """\
diff --git a/skills/foo/skill.py b/skills/foo/skill.py
--- a/skills/foo/skill.py
+++ b/skills/foo/skill.py
@@ -1,5 +1,6 @@
 def foo():
-    return 1
+    return 2
+    # added comment

 def bar():
-    pass
+    return True
"""


def test_count_changed_lines_basic():
    # - return 1, + return 2, + # added comment, - pass, + return True = 5
    assert count_changed_lines(_SAMPLE_DIFF) == 5


def test_count_changed_lines_excludes_file_headers():
    diff = "--- a/file.py\n+++ b/file.py\n-old line\n+new line\n"
    assert count_changed_lines(diff) == 2


def test_count_changed_lines_empty_diff():
    assert count_changed_lines("") == 0


def test_count_changed_lines_only_context_lines():
    # Lines without + or - prefix are context — not counted
    diff = " context line one\n context line two\n"
    assert count_changed_lines(diff) == 0


def test_count_changed_lines_only_additions():
    diff = "+++ b/new.py\n+line one\n+line two\n+line three\n"
    # +++ is header — excluded; the three +line* are additions
    assert count_changed_lines(diff) == 3


def test_count_changed_lines_only_removals():
    diff = "--- a/old.py\n+++ b/old.py\n-removed a\n-removed b\n"
    assert count_changed_lines(diff) == 2


def test_trivial_diff_below_threshold():
    diff = "-old\n+new\n"
    assert count_changed_lines(diff) == 2
    assert count_changed_lines(diff) < 20


def test_trivial_diff_at_threshold_is_not_trivial():
    # Exactly at threshold (20) must NOT be skipped (boundary: skip only when strictly less)
    lines = "".join(f"+line{i}\n" for i in range(20))
    assert count_changed_lines(lines) == 20


def test_trivial_diff_above_threshold():
    lines = "".join(f"+line{i}\n" for i in range(50))
    assert count_changed_lines(lines) == 50


def test_make_skip_findings_schema():
    findings = make_skip_findings(12, 20)
    assert set(findings.keys()) == {"spec_conformance", "defects", "architecture_notes"}
    assert findings["spec_conformance"] == []
    assert findings["defects"] == []
    assert len(findings["architecture_notes"]) == 1


def test_make_skip_findings_note_mentions_lines_and_threshold():
    findings = make_skip_findings(12, 20)
    note = findings["architecture_notes"][0]
    assert "12" in note
    assert "20" in note
    assert "skipped" in note.lower()


def test_make_skip_findings_note_mentions_no_model_call():
    findings = make_skip_findings(7, 20)
    note = findings["architecture_notes"][0]
    assert "No model call" in note or "no model call" in note.lower()


def test_parse_min_diff_lines_normal():
    assert parse_min_diff_lines("30") == 30


def test_parse_min_diff_lines_empty_string_returns_default():
    # GHA passes "" when a workflow input is not provided — must not crash
    assert parse_min_diff_lines("") == 20


def test_parse_min_diff_lines_none_like_returns_default():
    assert parse_min_diff_lines(None) == 20


def test_parse_min_diff_lines_whitespace_returns_default():
    assert parse_min_diff_lines("   ") == 20


def test_parse_min_diff_lines_non_integer_returns_default():
    assert parse_min_diff_lines("abc") == 20


def test_parse_min_diff_lines_custom_default():
    assert parse_min_diff_lines("", default=15) == 15


def test_parse_min_diff_lines_zero():
    assert parse_min_diff_lines("0") == 0
