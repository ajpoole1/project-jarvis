"""Discord verdict notification for Tom QA workflow.

Called by qa.yml Discord notification step. Reads all inputs from env vars
set by the step's env: block so the workflow contains no inline Python.
"""

import json
import os
import sys
import urllib.request


def main() -> None:
    webhook = os.environ.get("DISCORD_DEVLOOP_WEBHOOK", "")
    if not webhook:
        return

    verdict = os.environ.get("VERDICT", "ERROR")
    blocking = os.environ.get("BLOCKING", "?")
    artifact_type = os.environ.get("ARTIFACT_TYPE", "code")
    findings_raw = os.environ.get("FINDINGS", "")
    reviewer = os.environ.get("REVIEWER", "gemini")
    fallback_reason = os.environ.get("FALLBACK_REASON", "")
    pr = os.environ.get("PR_NUM", "?")
    item = os.environ.get("ITEM_ID", "")
    run_url = os.environ.get("RUN_URL", "")

    item_label = f" ({item})" if item and item != "none" else ""
    if reviewer == "claude-fallback":
        reason_note = f" ({fallback_reason})" if fallback_reason else ""
        fallback_label = f" [Claude fallback — Gemini unavailable{reason_note}]"
    else:
        fallback_label = ""

    if artifact_type == "spec":
        msg = f"[SPEC — advisory] ✅ Tom QA PASS — PR #{pr}{item_label} — spec-only diff, advisory notes only."
    elif verdict == "PASS":
        msg = f"✅ Tom QA PASS{fallback_label} — PR #{pr}{item_label} — no blocking issues. Ready to merge, sir."
    elif verdict == "SKIP":
        msg = f"⏭️ Tom QA SKIP — PR #{pr}{item_label} — trivial diff, no review needed."
    elif verdict == "FAIL":
        lines = [
            f"\U0001f6ab Tom QA FAIL{fallback_label} — PR #{pr}{item_label} — {blocking} blocking issue(s). "
            "Summon HM with these findings to fix:"
        ]
        try:
            findings = json.loads(findings_raw)
            blocking_c = [
                x for x in findings.get("spec_conformance", []) if x.get("severity") == "blocking"
            ]
            blocking_d = [x for x in findings.get("defects", []) if x.get("severity") == "blocking"]
            for x in blocking_c:
                lines.append(f"  • [conformance] {x.get('deviation', str(x))[:120]}")
            for x in blocking_d:
                loc = x.get("location", "")
                desc = x.get("description", str(x))[:120]
                t = x.get("type", "defect")
                lines.append(f"  • [{t}] {loc + ': ' if loc else ''}{desc}")
        except Exception:
            pass
        msg = "\n".join(lines)
        if len(msg) > 1900:
            msg = msg[:1900] + "\n  … (truncated)"
    else:
        reason_note = f" Reason: {fallback_reason}." if fallback_reason else ""
        msg = (
            f"⚠️ Tom QA ERROR — PR #{pr}{item_label} — Tom could not run "
            f"(Gemini and Claude fallback both failed).{reason_note} Merge is blocked. See: {run_url}"
        )

    data = json.dumps({"content": msg}).encode()
    req = urllib.request.Request(webhook, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req)
    except Exception as e:
        print(f"Warning: Discord notification failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
