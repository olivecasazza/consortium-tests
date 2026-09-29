---
type: Architecture
title: Rust crates
description: The Cargo workspace, and which crates are test suites versus test libraries.
tags: [structure, rust, cargo, testing]
generated: { by: human:ocasazza, at: 2026-09-29T00:00:00Z }
status: stable
---

# Rust crates

Two Cargo workspace members, both under `crates/`. Neither name matches its
directory, so read the mapping before the paths.

| Directory | Package | Kind | Tests |
|---|---|---|---|
| `crates/test-harness/` | `consortium-test-harness` v0.2.0 | **library** | 0 |
| `crates/integration-tests/` | `consortium-integration-tests` | integration test crate | 26 |

## `crates/test-harness` — a library, not a test

`consortium-test-harness` is a Rust **library** providing `DockerCluster` and
`ClusterTopology`: it builds a multi-node Docker cluster in a temp dir. It
contains no `#[test]`, no `#[cfg(test)]` module, and no `tests/` directory —
the single `mod tests` hit in the tree is inside a ```rust,ignore doc comment.

It was previously the top-level `test-harness/`, which read as a test suite.
Its only consumer is `crates/integration-tests`, which takes it as a
**dev-dependency** via `consortium-test-harness.workspace = true` — that is
inheritance from the workspace, not a path, so a directory move needs no edit
there.

## `crates/integration-tests` — the Rust suite

Two binaries, both gated on `#![cfg(feature = "docker-tests")]`:

- `docker_integration.rs` — 17 tests. **Run in CI** by `parity.yml:246`
  (6-node) and `:254` (33-node, filtered to `test_scale`).
- `tool_integration.rs` — 9 tests. **Never run in CI.** It needs the
  `consortium-nix-node` and `consortium-ansible-node` images, which CI never
  builds; `parity.yml:242` builds only `consortium-ssh-node`. `README.md`
  documents the two missing builds as manual steps.

Its fixtures point back at the vendored suite:
`crates/integration-tests/tests/docker_integration.rs:27-28` reads
`tests/docker/`.

## Unreferenced Docker assets

Under the vendored `tests/docker/`, two files are referenced by nothing in the
repo: `Dockerfile.slurm-controller` and `docker-compose.quick-test.yml`. The
compose file CI actually uses is `docker-compose.generated.yml`, written at
runtime by the harness library. Both are inside the frozen vendored set, so
they are recorded rather than removed.

## Workspace wiring

`Cargo.toml` lists both members and carries the `consortium-test-harness` path
dependency. `parity.yml:246` and `:254` invoke the integration tests with
`-p consortium-integration-tests` — a **package name**, which the directory
move does not change.
