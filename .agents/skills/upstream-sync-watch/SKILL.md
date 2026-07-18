---
name: upstream-sync-watch
description: Weekly watch of cea-hpc/clustershell for new upstream changes in the consortium-tests repo. Use when checking upstream ClusterShell for new commits, syncing the vendored oracle (lib/) and Python test suite (tests/, conf/) to upstream master, or filing/refreshing categorized GitHub issues for upstream changes and new test items. Triggered by the weekly cron job "consortium-upstream-sync-watch" and by manual requests like "check ClusterShell upstream", "sync upstream tests", or "file upstream parity issues".
---

# Upstream Sync Watch (consortium-tests)

Weekly procedure. Repo: the consortium-tests checkout (script auto-resolves
its root). Deterministic work lives in `scripts/upstream_sync_watch.py`;
this file covers judgment calls (conflicts, commits, pushes, verification).

## Module map (lib oracle ↔ Rust port, in the sibling consortium repo)

- `lib/ClusterShell/RangeSet.py` ↔ `crates/consortium/src/range_set.rs`
- `lib/ClusterShell/NodeSet.py` ↔ `crates/consortium/src/node_set.rs`
- `lib/ClusterShell/NodeUtils.py` ↔ `crates/consortium/src/node_utils.rs`
- `lib/ClusterShell/Task.py` ↔ `crates/consortium/src/task.rs`
- `lib/ClusterShell/Engine/*` ↔ `crates/consortium/src/engine/`
- `lib/ClusterShell/Worker/Tree.py` ↔ `crates/consortium/src/worker/tree.rs`
- `lib/ClusterShell/Worker/*` ↔ `crates/consortium/src/worker/`
- `lib/ClusterShell/Gateway.py` ↔ `crates/consortium/src/gateway.rs`
- `lib/ClusterShell/Communication.py` ↔ `crates/consortium/src/communication.rs`
- `lib/ClusterShell/Propagation.py` ↔ `crates/consortium/src/propagation.rs`
- `lib/ClusterShell/CLI/*` ↔ `crates/consortium-cli/` (claw/molt/pinch)
- `lib/ClusterShell/Topology.py` ↔ `crates/consortium/src/topology.rs`

## Weekly run

1. `git pull --ff-only` the consortium-tests repo (SSH key:
   `GIT_SSH_COMMAND='ssh -i ~/.ssh/olive_id_ed25519 -o IdentitiesOnly=yes'`).
2. Dry-run: `python3 .agents/skills/upstream-sync-watch/scripts/upstream_sync_watch.py --dry-run`.
   - `new=0` → nothing to do; report "upstream unchanged" and stop.
3. Real run: same command without `--dry-run`. It will, per new commit:
   categorize by conventional-commit prefix, extract new test items, and file
   one GitHub issue per commit (labels `upstream-sync`, `upstream-<category>`)
   — or write `upstream-issues/*.md` drafts when no `GITHUB_TOKEN` is
   available. It then syncs `lib/`, `tests/`, `conf/` to upstream master,
   re-applies consortium patches, and bumps `UPSTREAM_REF`.
4. Exit 0 → review `git status`/`git diff --stat` (expect: synced paths,
   `UPSTREAM_REF`, `.upstream-sync-state.json`, possibly `upstream-issues/`).
   Exit 3 → patch conflict: resolve per `references/consortium-patches.md`
   (upstream wins on semantics; re-port the intent of each consortium patch),
   then `git checkout --theirs`-style manual cleanup is FORBIDDEN — resolve
   hunks individually.
5. Smoke-verify the oracle still imports and parses:
   `PYTHONPATH=lib python3 -c "from ClusterShell.NodeSet import NodeSet; print(NodeSet('n[1-2]'))"`
   and run a fast parity slice: `CONSORTIUM_BACKEND=python PYTHONPATH=lib
   .venv/bin/python -m pytest tests/RangeSetTest.py tests/NodeSetTest.py -q`
   (create `.venv` with pytest if missing). New failures that did not exist
   pre-sync must be called out in the commit body and in your report.
6. Commit with `PRE_COMMIT_ALLOW_NO_CONFIG=1 git commit` staging explicit
   paths (never `git add -A`), message:
   `feat: sync ClusterShell upstream to <tip-sha7> (+N commits, M issues)`.
   Push with the SSH command from step 1.
7. Report: new commits by category, issues filed (URLs) or drafts written,
   pull result, parity-slice numbers, anything needing a human.

## Issue creation credentials

Set `GITHUB_TOKEN` (fine-grained PAT, `issues:write` on
olivecasazza/consortium-tests) in the environment or in a gitignored
`.env.local` at the repo root (`GITHUB_TOKEN=...`). Without it the script
writes reviewable drafts to `upstream-issues/` and the weekly report says so —
do not attempt unauthenticated API calls.

## Hard rules

- Never reset/stash/force-push; never edit `plan.md` in the sibling repo.
- `.upstream-sync-state.json` is the dedup record — never delete entries;
  a commit already recorded there must not get a second issue.
- If the pull touches `tests/TreeGatewayTimeoutTest.py`, `tests/bin/hostname`,
  or `tests/docker/` — these are consortium-only; upstream will never provide
  them, and they must never be deleted by a sync.
