#!/usr/bin/env python3
"""pr_review_items.py — derive a review items JSON from a pull request.

Pure functions build the items document from GitHub "list pull request
files" API responses; the CLI only performs GitHub I/O (via `gh api`) and
writes the file. The output feeds the reusable `ai-item-review` workflow
(the PR review gate in `.github/workflows/review-gate.yml`).

Items document shape (stable contract, consumed by the review instructions
and cross-checked by review_plan_check.py):

    {
      "pr": {"number", "title", "author", "base", "head",
             "additions", "deletions", "changed_files",
             "commit_count", "body"},
      "items": [{"path", "status", "additions", "deletions",
                 "patch", "previous_path"}],
      "patch_bytes_total": <sum of retained patch sizes in bytes>,
      "patch_missing_paths": [<paths the API shipped without a patch>],
      "patch_truncated_paths": [<paths whose patch was dropped for budget>]
    }
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Global budget for retained patch text; keeps the whole items document
# comfortably under zai_review.py's 512 KiB context guard.
DEFAULT_MAX_TOTAL_PATCH_BYTES = 256 * 1024

# "list pull request files" statuses (docs.github.com REST reference).
VALID_FILE_STATUSES = {
    "added", "removed", "modified", "renamed", "changed", "copied",
    "unchanged",
}

MAX_BODY_CHARS = 2000


def flatten_file_pages(parsed):
    """Normalize `gh api --paginate --slurp` output to a flat file list.

    Accepts a list of pages (each a list of file objects) or a single page
    (a list of file objects); anything else fails loud — a silent shape
    drift would hand the reviewer a truncated or empty diff.
    """
    if not isinstance(parsed, list):
        raise ValueError(
            f"expected a list of pages, got {type(parsed).__name__}")
    if not parsed:
        return []
    if all(isinstance(page, dict) for page in parsed):
        return list(parsed)
    if all(isinstance(page, list) for page in parsed):
        return [file for page in parsed for file in page]
    raise ValueError(
        "pages must be a list of pages or a single page of file objects, "
        "not a mix")


def _int_field(path, entry, key):
    value = entry.get(key)
    # bool is an int subclass in Python; the API never sends booleans here.
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{path}: {key} must be an integer, got {value!r}")
    return value


def build_items(files, pr, max_total_patch_bytes=DEFAULT_MAX_TOTAL_PATCH_BYTES):
    """Build the items document. Fails loud on any API shape drift.

    Patches are retained in file order up to `max_total_patch_bytes`
    (encoded length); anything over budget is dropped from `patch` and
    recorded in `patch_truncated_paths`. Files the API shipped without a
    patch (binary, too large) are recorded in `patch_missing_paths`.
    """
    items = []
    truncated, missing = [], []
    total = 0
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError(
                f"file entry must be an object, got {type(entry).__name__}")
        path = entry.get("filename")
        if not isinstance(path, str) or not path:
            raise ValueError(f"file entry without filename: {entry!r}")
        status = entry.get("status")
        if status not in VALID_FILE_STATUSES:
            raise ValueError(f"{path}: unknown file status {status!r}")
        additions = _int_field(path, entry, "additions")
        deletions = _int_field(path, entry, "deletions")
        patch = entry.get("patch")
        if patch is not None and not isinstance(patch, str):
            raise ValueError(f"{path}: patch must be a string or null")
        if patch is None:
            missing.append(path)
        else:
            size = len(patch.encode("utf-8"))
            if total + size <= max_total_patch_bytes:
                total += size
            else:
                truncated.append(path)
                patch = None
        items.append({
            "path": path,
            "status": status,
            "additions": additions,
            "deletions": deletions,
            "patch": patch,
            "previous_path": entry.get("previous_filename"),
        })
    return {
        "pr": {
            "number": pr["number"],
            "title": pr["title"],
            "author": pr["author"],
            "base": pr["base"],
            "head": pr["head"],
            "additions": pr["additions"],
            "deletions": pr["deletions"],
            "changed_files": pr["changed_files"],
            "commit_count": pr["commit_count"],
            "body": (pr.get("body") or "")[:MAX_BODY_CHARS],
        },
        "items": items,
        "patch_bytes_total": total,
        "patch_missing_paths": missing,
        "patch_truncated_paths": truncated,
    }


def _gh_json(argv):
    proc = subprocess.run(["gh", *argv], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(argv)} failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--pr", required=True, type=int, help="pull request number")
    ap.add_argument("--out", required=True, help="items JSON output path")
    args = ap.parse_args()

    try:
        pages = _gh_json(
            ["api", "--paginate", "--slurp",
             f"repos/{args.repo}/pulls/{args.pr}/files"])
        pr = _gh_json(["api", f"repos/{args.repo}/pulls/{args.pr}"])
        meta = {
            "number": pr["number"],
            "title": pr["title"],
            "author": pr["user"]["login"],
            "base": pr["base"]["ref"],
            "head": pr["head"]["ref"],
            "additions": pr["additions"],
            "deletions": pr["deletions"],
            "changed_files": pr["changed_files"],
            "commit_count": pr["commits"],
            "body": pr.get("body"),
        }
        doc = build_items(flatten_file_pages(pages), meta)
    except (RuntimeError, ValueError, KeyError, json.JSONDecodeError) as e:
        print(f"pr-review-items: {e}", file=sys.stderr)
        return 2

    Path(args.out).write_text(json.dumps(doc, indent=2) + "\n")
    print(f"{len(doc['items'])} file(s), {doc['patch_bytes_total']} patch "
          f"bytes, {len(doc['patch_truncated_paths'])} truncated, "
          f"{len(doc['patch_missing_paths'])} without patch -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
