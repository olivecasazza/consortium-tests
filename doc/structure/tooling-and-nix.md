---
type: Architecture
title: Tooling and Nix
description: The CI and scorecard tooling, the fanout64 benchmark flake, and how CI drives the whole repo.
tags: [structure, tooling, ci, nix, benchmark]
generated: { by: human:ocasazza, at: 2026-09-29T00:00:00Z }
status: stable
---

# Tooling and Nix

## `tools/` — CI and scorecard tooling

Five scripts, no tests. Previously the top-level `harness/`, a name that
collided with `tests/`, `test-harness/` and `integration-tests/`.

| Script | Purpose | In CI |
|---|---|---|
| `sync_upstream_tests.sh` | rsync vendored ClusterShell files back from upstream | yes — `parity.yml:78,80` |
| `cargo_to_junit.py` | Cargo test output to JUnit XML | yes — `parity.yml:106` |
| `generate_test_mapping.py` | regenerates `TEST_MAPPING.toml` | yes — `parity.yml:184` |
| `render_summary.py` | renders the run summary to the step summary | yes — `parity.yml:188` |
| `run_comparison.py` | local two-pass comparison | **no** — the documented local path |

`run_comparison.py` is the only script no workflow names. CI does the same
work with inline `python -m pytest tests/ -v` at `parity.yml:159` and `:173`.

`TEST_MAPPING.toml` is **generated**. `generate_test_mapping.py` emits both the
header comment and the TOML body, so the generator was updated alongside its
output; changing only the TOML would let the next `--update` reintroduce the
stale path.

The move to `tools/` preserved directory depth, so `sync_upstream_tests.sh`'s
`parent.parent` still resolves to the repo root.

## `nix/fanout-vms/` — the fanout64 benchmark

A flake source tree, not a test directory, holding the 64-microVM fanout
benchmark. It is a **fourth** place tests live: `test_bench.py` is run by a Nix
check (`flake.nix:112` `checks.fanout-bench-test`, discovered at `flake.nix:123`)
and by `nix-checks.yml:37`. Those tests are invisible to the repo's own pytest
config, because `pyproject.toml:2` sets `testpaths = ["tests"]`.

## The flake is not Snowfall Lib

`flake.nix` sits at the **repo root**, not inside `nix/`, and is a plain flake
with one `import ./nix/fanout-vms`. There is no `snowfall-lib` input and no
`mkFlake`; the string `snowfall` does not appear. The directory named after the
build system does not contain its entry point.

Adopting Snowfall Lib here would mean a `packages/`/`checks/` layout with each
entry a directory containing exactly `default.nix`. That is a real migration,
not a rename, and it would collide with the frozen vendored set. It is
recorded here as a known divergence, not silently assumed.

## CI shape

- `parity.yml` — the only workflow running the Python and Rust test layers.
- `nix-checks.yml` — `nix flake check --no-build` plus the fanout launcher tests.
- `upstream-watch.yml` — the weekly ClusterShell upstream sync.
- `ai-item-review.yml` — a reusable `workflow_call` gate used by the watcher.

## Secret on disk

`.env.local` holds a live GitHub PAT. It is correctly gitignored
(`.gitignore:29`) and is read deliberately as a token fallback by
`upstream_sync_watch.py:69-72`. Flagged because it is untracked-but-present
and any wholesale copy of the working tree would carry it.

## Committed private key

`nix/fanout-vms/keys/id_fanout` is a committed SSH private key, and
`flake.nix` passes it as `--ssh-key`. It is intentional and documented in
`keys/README.md` as test-only, baked into every guest as root's only
authorised key. It protects nothing real.
