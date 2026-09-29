# Roadmap

Open items from a repository audit. Each names the file it lives in so it can be
picked up directly, and records how to confirm it is still true.

`tests/`, `lib/`, `conf/`, `bash_completion.d/`, `setup.py`, `MANIFEST.in` and
`packaging/` are mirrored from upstream and frozen — see
[`.specs/index.md`](../index.md).

## Sync and packaging
- [ ] **The two upstream sync tools disagree about what they sync.**
      `harness/sync_upstream_tests.sh` syncs `tests/`, `lib/ClusterShell/` and
      `bash_completion.d/`. The watcher at
      `.agents/skills/upstream-sync-watch/scripts/upstream_sync_watch.py:49` sets
      `SYNC_PATHS = ["lib/", "tests/", "conf/"]` and never touches
      `bash_completion.d/`. So a sync through one tool silently reverts what the
      other one synced: `conf/` gets reverted by the shell tool, completions get
      reverted by the watcher. Pick one canonical set and have both read it.

- [ ] **`UPSTREAM_REF` holds a commit SHA, but the tarball URL is built for a tag.**
      `UPSTREAM_REF` contains `7d440c7cce5ca358ff4a59729bc7b26e3e857d3e`, while
      `harness/sync_upstream_tests.sh:27` fetches
      `https://github.com/cea-hpc/clustershell/archive/refs/tags/$REF.tar.gz`.
      A SHA is not a tag, so the default no-argument invocation 404s. The
      watcher already handles both forms
      (`upstream_sync_watch.py:365`: `resolve(f"refs/tags/{pin}") or resolve(pin)`);
      the shell tool needs the same fallback, or the URL has to become a commit
      archive URL. Reproduce: `bash harness/sync_upstream_tests.sh`.

- [ ] **`MANIFEST.in` and `setup.py` reference paths this tree does not have.**
      21 of the 35 `include` lines in `MANIFEST.in` match nothing here — all of
      `doc/**` plus `ChangeLog` — and 7 more paths named in `setup.py`'s
      `data_files` are missing: `doc/man/man1/{clubak,cluset,clush,nodeset}.1`,
      `doc/man/man5/{clush.conf,groups.conf}.5`, `doc/txt/clustershell.rst`.
      An sdist built from this tree is missing everything it claims to ship.
      Reproduce:

      ```sh
      while read -r l; do
        case "$l" in
          include*) ls -d ${l#include } >/dev/null 2>&1 || echo "missing: $l" ;;
        esac
      done < MANIFEST.in
      ```

      The mirror drops upstream's `doc/` tree, so the fix is to stop advertising
      it rather than to restore it.

## CI coverage

- [ ] **`integration-tests/tests/tool_integration.rs` never runs in CI.**
      No workflow references `tool_integration` at all, and
      `.github/workflows/parity.yml:242` builds only the `consortium-ssh-node`
      image. The tool tests additionally require `consortium-nix-node` and
      `consortium-ansible-node` (from `tests/docker/Dockerfile.nix-node` and
      `Dockerfile.ansible-node`), which the README documents as a manual
      prerequisite and CI never builds. The test binary therefore cannot run
      even if it is invoked, so the layer has no coverage today. Build both
      images in the workflow, then add the test target.

## Developer environment

- [ ] **The repo `.venv` is broken.**
      `.venv/` has no `bin/python` — the interpreter it was created against has
      been removed — so every README command routed through `.venv/bin/python`
      fails, and the parity suite has had no runnable interpreter. The dev
      shell now supplies one (`nix develop`, then
      `PYTHONPATH=lib python -m pytest tests/`), which resolves the immediate
      breakage; what is left is to decide whether `.venv` is retired in favour
      of the dev shell or repaired, and to make the README say which.
      Reproduce: `ls .venv/bin`.
