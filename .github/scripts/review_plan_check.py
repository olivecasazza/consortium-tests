#!/usr/bin/env python3
"""review_plan_check.py — fail-loud shape validator for AI review plans.

The reusable `ai-item-review` workflow hands back a plan.json whose shape
is whatever the caller's instructions demanded. This script is the caller
half of the contract for the PR review gate: it strictly validates that
shape and exits non-zero listing every violation.

Expected plan shape (must match the instructions in review-gate.yml):

    {
      "verdict": "approve" | "request_changes",
      "summary": "<1..500 chars>",
      "findings": [
        {"severity": "blocker" | "major" | "minor" | "nit",
         "path": "<a path from the items, or \"\">",
         "note": "<1..500 chars>"}
      ]
    }

Exactly those keys, no more, no fewer.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

VERDICTS = ("approve", "request_changes")
SEVERITIES = ("blocker", "major", "minor", "nit")
TOP_KEYS = {"verdict", "summary", "findings"}
FINDING_KEYS = {"severity", "path", "note"}
MAX_SUMMARY_CHARS = 500
MAX_NOTE_CHARS = 500


def validate_plan(plan, allowed_paths=None):
    """Return a list of shape violations; empty list means the plan is valid."""
    if not isinstance(plan, dict):
        return [f"plan must be a JSON object, got {type(plan).__name__}"]
    errors = []
    for key in sorted(TOP_KEYS - set(plan)):
        errors.append(f"missing required key {key!r}")
    for key in sorted(set(plan) - TOP_KEYS):
        errors.append(f"unexpected key {key!r}")

    verdict = plan.get("verdict")
    if verdict not in VERDICTS:
        errors.append(f"verdict must be one of {list(VERDICTS)}, got {verdict!r}")

    summary = plan.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        errors.append("summary must be a non-empty string")
    elif len(summary) > MAX_SUMMARY_CHARS:
        errors.append(f"summary must be <= {MAX_SUMMARY_CHARS} chars, "
                      f"got {len(summary)}")

    findings = plan.get("findings")
    if not isinstance(findings, list):
        errors.append(f"findings must be an array, got {type(findings).__name__}")
    else:
        for i, finding in enumerate(findings):
            errors.extend(_validate_finding(i, finding, allowed_paths))
    return errors


def _validate_finding(index, finding, allowed_paths):
    label = f"findings[{index}]"
    if not isinstance(finding, dict):
        return [f"{label} must be an object, got {type(finding).__name__}"]
    errors = []
    for key in sorted(FINDING_KEYS - set(finding)):
        errors.append(f"{label}: missing required key {key!r}")
    for key in sorted(set(finding) - FINDING_KEYS):
        errors.append(f"{label}: unexpected key {key!r}")

    severity = finding.get("severity")
    if severity not in SEVERITIES:
        errors.append(f"{label}: severity must be one of {list(SEVERITIES)}, "
                      f"got {severity!r}")

    path = finding.get("path")
    if not isinstance(path, str):
        errors.append(f"{label}: path must be a string, got {path!r}")
    elif path and allowed_paths is not None and path not in allowed_paths:
        errors.append(f"{label}: path {path!r} is not one of the item paths")

    note = finding.get("note")
    if not isinstance(note, str) or not note.strip():
        errors.append(f"{label}: note must be a non-empty string")
    elif len(note) > MAX_NOTE_CHARS:
        errors.append(f"{label}: note must be <= {MAX_NOTE_CHARS} chars, "
                      f"got {len(note)}")
    return errors


def check_plan_file(plan_path, items_path=None):
    """Load plan (and optional items) from disk and validate. Never raises."""
    try:
        plan = json.loads(Path(plan_path).read_text())
    except OSError as e:
        return [f"cannot read plan file: {e}"]
    except json.JSONDecodeError as e:
        return [f"plan file is not valid JSON: {e}"]

    allowed = None
    if items_path is not None:
        try:
            items = json.loads(Path(items_path).read_text())
            allowed = {item["path"] for item in items["items"]}
        except OSError as e:
            return [f"cannot read items file: {e}"]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            return [f"items file unreadable: {e}"]
    return validate_plan(plan, allowed_paths=allowed)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plan", required=True, help="plan.json to validate")
    ap.add_argument("--items", default=None,
                    help="items JSON; when given, finding paths must be a "
                         "subset of its item paths")
    args = ap.parse_args()

    errors = check_plan_file(args.plan, args.items)
    if errors:
        for error in errors:
            print(f"review-plan-check: {error}", file=sys.stderr)
        return 1
    print("review-plan-check: plan shape OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
