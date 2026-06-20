#!/usr/bin/env python3
"""
Build KNOWLEDGE_MANIFEST.md for the Jarvis workspace.
Run: python3 scripts/build_manifest.py
Output: /home/ajpoole/.openclaw/workspace/KNOWLEDGE_MANIFEST.md
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_DESC_MAX = 100


def _truncate(s, n=_DESC_MAX):
    """Truncate to n chars at a word boundary, appending … if cut."""
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0]
    return cut + "…"


OUTPUT = Path.home() / ".openclaw/workspace/KNOWLEDGE_MANIFEST.md"
COMMITTED_KNOWLEDGE = REPO_ROOT / "knowledge"
PRIVATE_KNOWLEDGE = Path.home() / ".jarvis/knowledge"
SKILLS_DIR = REPO_ROOT / "skills"

# Directories to skip for entity scanning (too numerous / not named entities)
_SKIP_SUBDIRS = {"recipes", "examples", "dev-crew", "dev-notes", "school"}


def _parse_frontmatter(text):
    """Return flat dict of YAML frontmatter scalar values (single-level only)."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    block = text[3:end]
    out = {}
    for line in block.splitlines():
        m = re.match(r"^([\w][\w_-]*):\s*(.+)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip("\"'")
    return out


def _first_h2(text):
    """Return text of the first ## heading, or None."""
    for line in text.splitlines():
        m = re.match(r"^## (.+)", line)
        if m:
            return m.group(1).strip()
    return None


def _open_items(text):
    """Return up to 3 short lines containing ⭐, ★, or standalone TBD."""
    items = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or len(stripped) > 80:
            continue
        if "⭐" in stripped or "★" in stripped:
            items.append(stripped)
        elif re.search(r"\bTBD\b", stripped):
            # skip table separator rows and very generic TBD cells
            if stripped.startswith("|") and stripped.count("|") > 3:
                continue
            items.append(stripped)
        if len(items) >= 3:
            break
    return items


def _scan_knowledge_root(root, label):
    """Yield entity dicts from a knowledge root directory."""
    if not root.exists():
        return
    for md_file in sorted(root.rglob("*.md")):
        # skip excluded subdirectories
        parts = md_file.relative_to(root).parts
        if any(p in _SKIP_SUBDIRS for p in parts[:-1]):
            continue
        # skip index/schema files
        if md_file.name.upper() in {
            "KNOWLEDGE.md",
            "TIERS.md",
            "PEOPLE.md",
            "SCHOOL.md",
            "README.md",
        }:
            continue
        try:
            text = md_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        fm = _parse_frontmatter(text)
        name = fm.get("name") or fm.get("slug") or fm.get("title")
        if not name:
            continue
        slug = fm.get("slug", re.sub(r"[^a-z0-9-]", "-", name.lower()))
        relationship = fm.get("relationship") or fm.get("tier", "")
        h2 = _first_h2(text)
        tbds = _open_items(text)
        domain = parts[0] if len(parts) > 1 else label
        yield {
            "name": name,
            "slug": slug,
            "relationship": relationship,
            "domain": domain,
            "h2": h2,
            "tbds": tbds,
            "label": label,
        }


def _skill_description(skill_dir):
    """One-line description from README.md (first prose line) or skill.py."""
    readme = skill_dir / "README.md"
    if readme.exists():
        try:
            for line in readme.read_text(encoding="utf-8", errors="ignore").splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    # Take up to the first sentence end to avoid mid-thought cuts
                    m = re.search(r"([.!?])\s", stripped)
                    sentence = stripped[: m.end(1)] if m else stripped
                    return _truncate(sentence)
        except OSError:
            pass
    skill_py = skill_dir / "skill.py"
    if skill_py.exists():
        try:
            in_doc = False
            for line in skill_py.read_text(encoding="utf-8", errors="ignore").splitlines():
                stripped = line.strip()
                if stripped.startswith('"""') or stripped.startswith("'''"):
                    inner = stripped[3:].strip().rstrip("\"' ")
                    if inner:
                        return _truncate(inner)
                    in_doc = True
                    continue
                if in_doc:
                    if stripped.endswith('"""') or stripped.endswith("'''"):
                        break
                    if stripped:
                        return _truncate(stripped)
                if stripped.startswith("#") and not stripped.startswith("#!"):
                    return _truncate(stripped.lstrip("#").strip())
        except OSError:
            pass
    return ""


def build():
    lines = [
        "# KNOWLEDGE_MANIFEST",
        "",
        "_Auto-generated — run `python3 scripts/build_manifest.py` to refresh. Do not hand-edit._",
        "",
        "## Skills",
        "",
    ]

    if SKILLS_DIR.exists():
        for skill_dir in sorted(SKILLS_DIR.iterdir()):
            if not skill_dir.is_dir() or skill_dir.name.startswith("."):
                continue
            name = skill_dir.name
            desc = _skill_description(skill_dir)
            if desc:
                lines.append(f"- **{name}** — {desc}")
            else:
                lines.append(f"- **{name}**")

    lines += ["", "## Knowledge Entities", ""]

    entities = list(_scan_knowledge_root(COMMITTED_KNOWLEDGE, "committed"))
    try:
        entities += list(_scan_knowledge_root(PRIVATE_KNOWLEDGE, "private"))
    except Exception:
        pass

    if not entities:
        lines.append("_(none — check knowledge root paths)_")
    else:
        for e in entities:
            parts = [f"- **{e['name']}**"]
            meta = []
            if e["relationship"]:
                meta.append(e["relationship"])
            if e["domain"] not in ("committed", "private"):
                meta.append(e["domain"])
            if meta:
                parts.append(f"({', '.join(meta)})")
            if e["h2"]:
                parts.append(f"— {e['h2']}")
            if e["tbds"]:
                tbd_str = "; ".join(e["tbds"][:2])
                parts.append(f"[open: {tbd_str[:80]}]")
            lines.append(" ".join(parts))

    lines.append("")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        f"Written {OUTPUT} ({len(lines)} lines, {len(entities)} entities, "
        f"{sum(1 for ln in lines if ln.startswith('- **') and 'skill' not in ln.lower())} skill entries)"
    )


if __name__ == "__main__":
    build()
