---
name: upstream-sync-watch
description: Weekly watch of cea-hpc/clustershell for new upstream changes in the consortium-tests repo. Use when checking upstream ClusterShell for new commits, syncing the vendored oracle (lib/) and Python test suite (tests/, conf/) to upstream master, or filing/refreshing categorized GitHub issues for upstream changes and new test items. Runs as the weekly GitHub Action "upstream-watch" (AI-reviewed by Z.AI GLM via the reusable ai-item-review workflow) and by manual requests like "check ClusterShell upstream", "sync upstream tests", or "file upstream parity issues".
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

## Weekly run — GitHub Action (primary)

`.github/workflows/upstream-watch.yml` runs every Monday 15:41 UTC
(`cron: "41 15 * * 1"`, plus `workflow_dispatch`) in three jobs:

1. **detect** — `upstream_sync_watch.py --json-items items.json`: fetches
   upstream, categorizes new commits, extracts test items, uploads
   `items.json`. Zero commits → the run stops here.
2. **review** — the generic reusable workflow
   `.github/workflows/ai-item-review.yml` sends the items plus split
   instructions to Z.AI GLM-5.2 and demands strict JSON back. Invalid JSON
   after retries → the job fails and NOTHING is filed or synced (fail-loud
   gate; next week's run re-detects the same commits).
3. **file** — `upstream_sync_watch.py --plan plan.json --commit-push`:
   strictly validates the plan (every detected sha appears exactly once
   across `issues[].shas` and `skip[].sha`; categories must be known),
   files one issue per plan entry (labels `upstream-sync`,
   `upstream-<category>`, `ai-reviewed`), marks skips as processed without
   filing, syncs `lib/` `tests/` `conf/`, bumps `UPSTREAM_REF`, commits and
   pushes. A validation failure or a patch conflict (exit 2/3) fails the
   run before any push.

Required secret on olivecasazza/consortium-tests: `ZAI_API_KEY`
(https://docs.z.ai). Issue filing and pushing use the workflow's built-in
`GITHUB_TOKEN`. The model never touches the repo or the GitHub API — it
only shapes JSON; all side effects stay in the deterministic script.

### Plan JSON contract (AI output → `--plan`)

```json
{
  "issues": [{"category": "feat|fix|perf|refactor|test|ci|build|docs|style|chore|revert|other",
              "title": "imperative title", "shas": ["<full sha>"],
              "rationale": "why grouped / what matters for the Rust port"}],
  "skip": [{"sha": "<full sha>", "rationale": "why non-actionable"}]
}
```

### Reusing the AI review gate for other agent reviews

`ai-item-review.yml` is generic: any workflow (any repo) can call it with
an items artifact + free-form instructions and get back a validated
`plan.json` artifact:

```yaml
jobs:
  review:
    uses: olivecasazza/consortium-tests/.github/workflows/ai-item-review.yml@master
    with:
      items_artifact: my-items      # artifact containing items.json
      instructions: |
        Review these items. Respond with STRICT JSON: {"verdict": ...}
    secrets:
      ZAI_API_KEY: ${{ secrets.ZAI_API_KEY }}
```

## Manual / fallback run (local)

The same engine runs locally, filing one issue per commit (no AI split):

1. `git pull --ff-only` the consortium-tests repo (SSH key:
   `GIT_SSH_COMMAND='ssh -i ~/.ssh/olive_id_ed25519 -o IdentitiesOnly=yes'`).
2. Dry-run: `python3 .agents/skills/upstream-sync-watch/scripts/upstream_sync_watch.py --dry-run`.
   - `new=0` → nothing to do; report "upstream unchanged" and stop.
3. Real run: same command without `--dry-run` (optionally `--plan plan.json`
   to replay an AI-reviewed split, `--commit-push` to commit and push the
   result). It files one GitHub issue per commit (labels `upstream-sync`,
   `upstream-<category>`) — or `upstream-issues/*.md` drafts when no
   `GITHUB_TOKEN` is available — then syncs `lib/`, `tests/`, `conf/`,
   re-applies consortium patches, and bumps `UPSTREAM_REF`.
4. Exit 3 → patch conflict: resolve per `references/consortium-patches.md`
   (upstream wins on semantics; re-port the intent of each consortium patch).
   `git checkout --theirs`-style wholesale cleanup is FORBIDDEN — resolve
   hunks individually.
5. Smoke-verify the oracle still imports and parses:
   `PYTHONPATH=lib python3 -c "from ClusterShell.NodeSet import NodeSet; print(NodeSet('n[1-2]'))"`
   and run a fast parity slice: `CONSORTIUM_BACKEND=python PYTHONPATH=lib
   .venv/bin/python -m pytest tests/RangeSetTest.py tests/NodeSetTest.py -q`.
   New failures that did not exist pre-sync must be called out in the commit
   body and in your report.
6. If not using `--commit-push`, commit with
   `PRE_COMMIT_ALLOW_NO_CONFIG=1 git commit` staging explicit paths (never
   `git add -A`), message:
   `feat: sync ClusterShell upstream to <tip-sha7> (+N commits, M issues)`,
   and push with the SSH command from step 1.
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
