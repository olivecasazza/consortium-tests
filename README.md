# consortium-tests

Test infrastructure for [consortium](https://github.com/olivecasazza/consortium),
a Rust reimplementation of the ClusterShell toolchain. This repo owns:

- **Upstream ClusterShell parity suite** — the upstream Python test suite
  (`tests/`) synced from `cea-hpc/clustershell` at the ref pinned in
  `UPSTREAM_REF`, run against both the pure-Python oracle and the Rust
  (PyO3) bindings.
- **Python oracle** — the vendored upstream implementation (`lib/ClusterShell/`),
  used as the reference backend and as the fallback for not-yet-ported modules.
- **Comparison harness** (`tools/`) — sync, mapping, and scorecard tooling:
  - `sync_upstream_tests.sh` — re-sync `tests/`, `lib/`, and
    `bash_completion.d/` from upstream and update `UPSTREAM_REF`
  - `generate_test_mapping.py` — regenerate `TEST_MAPPING.toml` (Python test → Rust test)
  - `run_comparison.py` — run both pytest backends + Rust unit tests into JUnit XML
  - `render_summary.py` — render the migration scorecard from JUnit XML
  - `cargo_to_junit.py` — convert `cargo test` output to JUnit XML
- **Rust integration-test layer** — `crates/test-harness/` (the
  `consortium-test-harness` crate: DockerCompose mini-HPC clusters) and
  `crates/integration-tests/` (`docker_integration.rs` + `tool_integration.rs`,
  gated behind the `docker-tests` feature). Docker assets live in `tests/docker/`.
- **Audit backlog** — [`.specs/`](.specs/index.md): open issues found in this
  repo and how work moves, with the checklist in `.specs/tasks/roadmap.md`.

## Layout

```
tests/            upstream ClusterShell Python tests (+ tests/docker Docker assets)
lib/              vendored upstream Python implementation (the oracle)
bash_completion.d/ upstream clush/cluset completion scripts
conf/             upstream ClusterShell configuration examples
tools/            sync/mapping/comparison/scorecard scripts
packaging/        upstream RPM packaging files
crates/test-harness/       consortium-test-harness crate (Docker mini-HPC harness)
crates/integration-tests/  consortium-integration-tests crate (docker_tests feature-gated)
TEST_MAPPING.toml generated python-test → rust-test mapping
UPSTREAM_REF      pinned cea-hpc/clustershell ref for the parity suite
setup.py, setup.cfg, MANIFEST.in, pyproject.toml, COPYING.LGPLv2.1
                  upstream Python packaging/pytest config kept with the suite
```

Why each directory is where it is — including which ones are frozen because
they are mirrored from upstream — is in [doc/structure/](doc/structure/index.md).

## Sibling-checkout requirement

The Rust path dependencies and harness scripts expect the **consortium** repo
checked out as a sibling directory:

```
<parent>/
  consortium/        https://github.com/olivecasazza/consortium
  consortium-tests/  this repo
```

All Rust crates here reference `../consortium/crates/*` via path deps, and the
harness scripts resolve the consortium checkout via (in order):
`--consortium-repo` CLI flag → `$CONSORTIUM_REPO` → `../consortium`.

## Running the parity suite

Python environment (once):

```sh
python3 -m venv .venv
.venv/bin/pip install pytest pytest-timeout pyyaml maturin
```

**Oracle backend** (pure Python, no Rust build needed):

```sh
CONSORTIUM_BACKEND=python PYTHONPATH=lib .venv/bin/python -m pytest tests/
```

**Rust backend** — build the bindings into this repo's venv first:

```sh
cd ../consortium/crates/consortium-py
/path/to/consortium-tests/.venv/bin/maturin develop
cd ../../../consortium-tests
CONSORTIUM_BACKEND=rust .venv/bin/python -m pytest tests/
```

(`maturin develop` installs an editable `.pth` into the venv pointing at
`../consortium/crates/consortium-py`; the ClusterShell shim resolves the
oracle tree from this repo's `lib/` automatically in the sibling-checkout
layout, or via the `LIB_CLUSTERSHELL` env var.)

**Regenerate the test mapping** (scans Rust tests in the consortium repo):

```sh
CONSORTIUM_REPO=../consortium .venv/bin/python tools/generate_test_mapping.py --update
# or: .venv/bin/python tools/generate_test_mapping.py --update --consortium-repo=../consortium
```

**Sync upstream tests** (updates `tests/`, `lib/`, `bash_completion.d/`, and
`UPSTREAM_REF` here):

```sh
bash tools/sync_upstream_tests.sh            # pinned ref
bash tools/sync_upstream_tests.sh v1.10.1    # explicit ref
```

**Full comparison + scorecard:**

```sh
.venv/bin/python tools/run_comparison.py
.venv/bin/python tools/render_summary.py --results-dir=results
```

## Docker integration tests

Requires Docker. SSH keys are generated automatically by the harness.

```sh
cd crates/integration-tests  # or run from repo root with -p
cargo test -p consortium-integration-tests --features docker-tests --test docker_integration -- --test-threads=1
cargo test -p consortium-integration-tests --features docker-tests --test tool_integration -- --test-threads=1
```

The first run builds the `consortium-ssh-node` image from
`tests/docker/Dockerfile.ssh-node`. Tool tests additionally use the
`nix-node`/`ansible-node` images:

```sh
docker build -t consortium-nix-node -f tests/docker/Dockerfile.nix-node tests/docker/
docker build -t consortium-ansible-node -f tests/docker/Dockerfile.ansible-node tests/docker/
```

CI runs this suite via `.github/workflows/parity.yml`, which checks out both
repos side by side so the path dependencies resolve.
