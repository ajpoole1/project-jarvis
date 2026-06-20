"""Tests for is_spec_only_diff artifact detection."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_HELPERS_PATH = Path(__file__).parents[1] / "scripts" / "dev-loop" / "tom_qa_helpers.py"
_spec = importlib.util.spec_from_file_location("tom_qa_helpers", _HELPERS_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
is_spec_only_diff = _mod.is_spec_only_diff

SPEC_DIFF = """--- a/knowledge/dev-notes/queue/2026-0028-spec.md
+++ b/knowledge/dev-notes/queue/2026-0028-spec.md
@@ -1,3 +1,4 @@
+added line
"""

CODE_DIFF = """--- a/skills/finance/skill.py
+++ b/skills/finance/skill.py
@@ -1,3 +1,4 @@
+added line
"""

MIXED_DIFF = """--- a/knowledge/README.md
+++ b/knowledge/README.md
@@ -1 +1,2 @@
+note
--- a/skills/grocery/skill.py
+++ b/skills/grocery/skill.py
@@ -1 +1,2 @@
+code
"""

EMPTY_DIFF = ""


def test_spec_only():
    assert is_spec_only_diff(SPEC_DIFF) is True


def test_code_only():
    assert is_spec_only_diff(CODE_DIFF) is False


def test_mixed():
    assert is_spec_only_diff(MIXED_DIFF) is False


def test_empty():
    assert is_spec_only_diff(EMPTY_DIFF) is False
