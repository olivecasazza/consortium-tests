#!/usr/bin/env python3
"""upstream_sync_watch.py — weekly upstream-sync detector for consortium-tests.

Checks cea-hpc/clustershell for commits after the UPSTREAM_REF pin that touch
lib/, tests/, or conf/; categorizes them by conventional-commit type; extracts
new/changed test items; files one GitHub issue per commit (or writes draft
files when no token is available); and updates the vendored upstream content
(lib/, tests/, conf/) while re-applying consortium patches.

Usage:
    upstream_sync_watch.py [--repo PATH] [--dry-run] [--limit N]
                           [--no-pull] [--json]

Exit codes: 0 ok (possibly nothing new), 2 fetch/detection error,
            3 pull conflict — manual resolution required (see SKILL.md).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

UPSTREAM_REPO = "cea-hpc/clustershell"
UPSTREAM_URL = f"https://github.com/{UPSTREAM_REPO}.git"
ISSUE_REPO = "olivecasazza/consortium-tests"
SYNC_PATHS = ["lib/", "tests/", "conf/"]
STATE_FILE = ".upstream-sync-state.json"
DRAFT_DIR = "upstream-issues"
CATEGORIES = ["feat", "fix", "perf", "refactor", "test", "ci", "build",
              "docs", "style", "chore", "revert", "other"]
CC_RE = re.compile(r"^(\w+)(?:\([^)]*\))?!?:\s")
TESTDEF_RE = re.compile(r"^\+\s*def (test\w+)\s*\(")


def run(repo: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def resolve_token(repo: Path) -> str | None:
    if os.environ.get("GITHUB_TOKEN"):
        return os.environ["GITHUB_TOKEN"]
    env_local = repo / ".env.local"
    if env_local.exists():
        for line in env_local.read_text().splitlines():
            if line.startswith("GITHUB_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def categorize(subject: str) -> str:
    """Conventional-commit category: parsed from the prefix when present,
    otherwise inferred (upstream uses 'Component: verb ...' subjects)."""
    m = CC_RE.match(subject)
    if m and m.group(1).lower() in CATEGORIES:
        return m.group(1).lower()
    s = subject.lower()
    component = s.split(":", 1)[0] if ":" in s else ""
    if re.match(r"^(doc|docs|documentation|man)\b", s) or \
            any(k in s for k in ("spelling", "grammar", "readme")):
        return "docs"
    if re.search(r"\brelease\b", s):
        return "chore"
    if component.strip() in ("test", "tests"):
        return "test"
    if any(k in s for k in ("fix", "bug", "crash", "regression",
                            "broken", "typo", "error")):
        return "fix"
    if re.search(r"\btests?\b", s):
        return "test"
    if any(k in s for k in ("add", "implement", "support", "introduce",
                            "accept", "emit", "warn", "pin")):
        return "feat"
    return "other"


def commit_info(repo: Path, sha: str) -> dict:
    fmt = "%H%x00%ad%x00%s%x00%b"
    raw = run(repo, "show", "-s", f"--format={fmt}", "--date=short", sha)
    full, date, subject, body = raw.split("\x00", 3)
    files = run(repo, "show", "--name-only", "--format=", sha).split()
    test_items: list[str] = []
    new_test_files: list[str] = []
    for f in files:
        if f.startswith("tests/") and f.endswith(".py"):
            st = run(repo, "diff-tree", "--no-commit-id", "-r", sha)
            if f"A\t{f}" in st or any(
                    l.split("\t")[-1] == f and l.startswith("A")
                    for l in st.splitlines()):
                new_test_files.append(f)
    diff = run(repo, "show", "--format=", "--", sha, "--", *["tests/"],
               check=False) or run(repo, "show", sha, "--", "tests/")
    current_file = None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
        m = TESTDEF_RE.match(line)
        if m and current_file:
            test_items.append(f"{current_file}::{m.group(1)}")
    return {"sha": full.strip(), "date": date.strip(),
            "subject": subject.strip(), "body": body.strip(),
            "files": files, "category": categorize(subject.strip()),
            "new_test_files": sorted(set(new_test_files)),
            "test_items": sorted(set(test_items))}


def issue_payload(info: dict) -> tuple[str, str, list[str]]:
    sha7 = info["sha"][:7]
    title = f"[{info['category']}] {info['subject']} (upstream {sha7})"
    labels = ["upstream-sync", f"upstream-{info['category']}"]
    lines = [
        f"Upstream: [{UPSTREAM_REPO}@{sha7}](https://github.com/{UPSTREAM_REPO}/commit/{info['sha']}) — {info['date']}",
        f"Category: `{info['category']}`", "",
        "**Files touched:**"]
    lines += [f"- `{f}`" for f in info["files"]] or ["- (none)"]
    if info["new_test_files"]:
        lines += ["", "**New test files:**"]
        lines += [f"- `{f}`" for f in info["new_test_files"]]
    if info["test_items"]:
        lines += ["", "**New/changed test items:**"]
        lines += [f"- `{t}`" for t in info["test_items"]]
    if info["body"]:
        lines += ["", "**Commit message:**", "", "```", info["body"][:2000], "```"]
    lines += ["", "---", "**Porting checklist**",
              "- [ ] `lib/` oracle sync verified (weekly automation pull)",
              "- [ ] Rust analogue reviewed/ported "
              "(see module map in `.agents/skills/upstream-sync-watch/SKILL.md`)",
              "- [ ] PyO3 bindings reviewed (`consortium-py`)",
              "- [ ] Parity suite green after port",
              "",
              "_Filed by the weekly upstream-sync watch._"]
    return title, "\n".join(lines), labels


def ensure_label(repo_full: str, name: str, token: str) -> None:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo_full}/labels",
        data=json.dumps({"name": name, "color": "0e8a16"}).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json"},
        method="POST")
    try:
        urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        if e.code != 422:  # 422 = already exists
            raise


def create_issue(repo_full: str, title: str, body: str,
                 labels: list[str], token: str) -> str:
    for lab in labels:
        ensure_label(repo_full, lab, token)
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo_full}/issues",
        data=json.dumps({"title": title, "body": body,
                         "labels": labels}).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json"},
        method="POST")
    with urllib.request.urlopen(req) as r:
        return json.load(r)["html_url"]


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:50]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--no-pull", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    repo = Path(args.repo).resolve() if args.repo else Path(
        __file__).resolve().parents[4]
    pin_file = repo / "UPSTREAM_REF"
    if not pin_file.exists():
        print("UPSTREAM_REF missing", file=sys.stderr)
        return 2

    # 1. fetch upstream
    remotes = run(repo, "remote")
    if "upstream" not in remotes.split():
        run(repo, "remote", "add", "upstream", UPSTREAM_URL)
    run(repo, "fetch", "upstream", "--tags", "--quiet")

    pin = pin_file.read_text().strip()

    def resolve(ref: str) -> str:
        r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify",
                            "--quiet", f"{ref}^{{commit}}"],
                           capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""

    pin_sha = resolve(f"refs/tags/{pin}") or resolve(pin)
    if not pin_sha:
        print(f"cannot resolve UPSTREAM_REF={pin}", file=sys.stderr)
        return 2
    tip = run(repo, "rev-parse", "upstream/master").strip()

    shas = run(repo, "log", "--reverse", "--format=%H",
               f"{pin_sha}..upstream/master", "--", *SYNC_PATHS).split()
    state_path = repo / STATE_FILE
    state = json.loads(state_path.read_text()) if state_path.exists() \
        else {"processed": {}}
    todo = [s for s in shas if s not in state["processed"]][: args.limit]

    infos = [commit_info(repo, s) for s in todo]
    by_cat: dict[str, list[dict]] = {}
    for i in infos:
        by_cat.setdefault(i["category"], []).append(i)

    summary = {
        "pin": pin, "pin_sha": pin_sha, "upstream_tip": tip,
        "new_commits": len(shas), "unprocessed": len(infos),
        "by_category": {c: len(v) for c, v in sorted(by_cat.items())},
        "mode": "dry-run" if args.dry_run else
                ("issues" if resolve_token(repo) else "drafts"),
    }
    def emit_summary() -> None:
        print(json.dumps(summary, indent=2) if args.json else
              f"pin={pin[:12]} tip={tip[:12]} new={len(shas)} "
              f"todo={len(infos)} mode={summary['mode']}\n" +
              "\n".join(f"  {c}: {n}"
                        for c, n in summary["by_category"].items()))

    if args.dry_run or not infos:
        emit_summary()
        return 0

    # 2. file issues (or drafts)
    token = resolve_token(repo)
    for info in infos:
        title, body, labels = issue_payload(info)
        if token:
            url = create_issue(ISSUE_REPO, title, body, labels, token)
            state["processed"][info["sha"]] = {"issue": url,
                                               "category": info["category"]}
            print(f"issue  {info['sha'][:7]} [{info['category']}] {url}",
                  file=sys.stderr)
        else:
            d = repo / DRAFT_DIR
            d.mkdir(exist_ok=True)
            p = d / f"{info['sha'][:7]}-{slug(info['subject'])}.md"
            p.write_text(f"---\ntitle: {title}\nlabels: {', '.join(labels)}\n"
                         f"---\n\n{body}\n")
            state["processed"][info["sha"]] = {"draft": str(p.relative_to(repo)),
                                               "category": info["category"]}
            print(f"draft  {info['sha'][:7]} [{info['category']}] {p.name}",
                  file=sys.stderr)

    # 3. pull upstream content, re-applying consortium patches
    if not args.no_pull:
        with tempfile.NamedTemporaryFile(
                "w", suffix=".diff", delete=False) as tf:
            ours = run(repo, "diff", "--diff-filter=MD", tip, "--",
                       *SYNC_PATHS)
            tf.write(ours)
            patch_path = tf.name
        run(repo, "checkout", tip, "--", *SYNC_PATHS)
        if ours.strip():
            r = subprocess.run(["git", "-C", str(repo), "apply", patch_path],
                               capture_output=True, text=True)
            if r.returncode != 0:
                print(f"PULL CONFLICT re-applying consortium patches:\n"
                      f"{r.stderr}\nPatch saved at {patch_path}\n"
                      f"Resolve per SKILL.md, then finish manually.",
                      file=sys.stderr)
                state_path.write_text(json.dumps(state, indent=2))
                emit_summary()
                return 3
        if not [s for s in shas if s not in state["processed"]]:
            pin_file.write_text(tip + "\n")
            print(f"pulled lib/+tests/+conf to {tip[:12]}, UPSTREAM_REF bumped",
                  file=sys.stderr)
        else:
            print(f"pulled content to {tip[:12]}; UPSTREAM_REF kept at "
                  f"{pin[:12]} — {len(shas) - len(todo)} commits remain "
                  f"unprocessed (--limit), next run continues",
                  file=sys.stderr)

    state_path.write_text(json.dumps(state, indent=2))
    emit_summary()
    return 0


if __name__ == "__main__":
    sys.exit(main())
