# Consortium patches — must survive every upstream sync

The vendored `lib/` + `tests/` equal upstream master **plus** these deliberate
consortium patches. After every sync, `git diff upstream/master -- lib/ tests/ conf/`
must show exactly these (plus consortium-only files). If a sync conflict
involves one, re-port its INTENT onto the new upstream code; upstream wins on
unrelated semantics. Verified current as of pin 7d440c7 (upstream 1.10.1).

## lib/ClusterShell/Propagation.py

StartMessage timeout invalidation: when a channel's start message arrives
after its timeout already fired, the stale worker/channel state is invalidated
instead of resurrecting the channel. Prevents ghost channels in gateway tests.

## lib/ClusterShell/Task.py

`_pchannel` connect_timeout safety net (~line 1352): propagation channel
setup passes an explicit connect timeout so a hung gateway connect cannot
block Task resume indefinitely.

## tests/TLib.py

`CSTEST_HOSTNAME` env override: tests resolve the local hostname through a
helper that honors `CSTEST_HOSTNAME` (e.g. `localhost`) when the machine's
short hostname is not SSH-reachable — required on the nixlab Mac Mini.

## Consortium-only files (upstream never provides; keep)

- `tests/TreeGatewayTimeoutTest.py` — gateway reply-timeout regression tests
  (uses `from .TLib import` — package-relative, required since upstream added
  `tests/__init__.py`).
- `tests/bin/hostname` — test fixture (upstream has an identical copy).
- `tests/docker/` — Dockerfiles + compose for the Rust integration layer.

## History note

The 2026-07-17 sync to 1.10.1 (0cc8cc2..7d440c7) re-applied all three patches
cleanly; no patch has ever been dropped. Dropping one requires explicit owner
approval and a note in the sync commit body.
