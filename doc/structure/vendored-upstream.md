---
type: Architecture
title: Vendored upstream — ClusterShell
description: The directories mirrored from ClusterShell upstream, and why their paths are frozen.
tags: [structure, vendored, upstream, clustershell]
generated: { by: human:ocasazza, at: 2026-09-29T00:00:00Z }
status: stable
---

# Vendored upstream: ClusterShell

`consortium-tests` carries a copy of parts of ClusterShell so the Rust rewrite
can be checked against the behaviour it is replacing. Those copies are
**inputs to a sync**, not local code, which is what makes their paths frozen.

## The frozen set

| Path | What it is |
|---|---|
| `tests/` | ClusterShell Python test suite, 32 `*Test.py` files |
| `lib/` | the `ClusterShell` Python package |
| `conf/` | example configuration files |
| `bash_completion.d/` | `clush` / `cluset` completions |
| `setup.py`, `MANIFEST.in`, `packaging/` | ClusterShell's own packaging |
| `TEST_MAPPING.toml` | generated map from Python tests to Rust tests |

## Why the paths cannot move

Two independent sync tools write these paths, and both hardcode them:

- `.agents/skills/upstream-sync-watch/scripts/upstream_sync_watch.py:49`
  sets `SYNC_PATHS = ["lib/", "tests/", "conf/"]`, and uses it at
  `:372` and `:456` to compute both the diff and the copy set.
- `tools/sync_upstream_tests.sh` rsyncs `$EXTRACTED_DIR/tests/`,
  `lib/ClusterShell/` and `bash_completion.d/` back into the repo.

A rename would be silently undone on the next sync, or would make the two
tools disagree. Everything else at the top level was free to move, and was.

## Known inconsistency

The two tools do not agree on `conf/`. `upstream_sync_watch.py:49` syncs it;
`tools/sync_upstream_tests.sh` does not. They are used by different CI jobs
(`parity.yml:78` versus `upstream-watch.yml:131`). Recorded rather than fixed:
which one is authoritative is a question about the intended upstream policy,
not about layout.

## Upstream pin

`UPSTREAM_REF` at the repo root holds a 40-character commit SHA.
`tools/sync_upstream_tests.sh:17-18` reads it and builds a
`refs/tags/<ref>` URL, which cannot resolve for a SHA; the commit-URL
fallback at `:30-32` is what actually runs. The script works, but the tag
attempt is dead code that costs a 404 on every sync.

## Version drift

Three places claim `1.9.3` — `setup.py:26`, `packaging/rpm/clustershell.spec.in`
and the removed `doc-legacy/UPSTREAM_REF` — while the vendored code is
upstream `1.10.1`. The Cargo workspace separately declares `0.2.0`. Treat the
Python `1.9.3` numbers as the packaging version and the pinned SHA as the
truth about what code is actually here.

## Dead packaging references

`MANIFEST.in:14-33` and `setup.py:56-62` name 20 paths under `doc/txt/`,
`doc/man/`, `doc/sphinx/`, `doc/examples/` and `doc/epydoc/` that do not exist
here; `setup.py` would raise `FileNotFoundError` if it were run. `packaging/`
and `mkrpm.sh` are not referenced by any workflow. None of this is on a CI
path, so it is recorded rather than repaired.
