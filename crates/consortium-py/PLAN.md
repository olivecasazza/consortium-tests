# Bindings plan (Bindings_Engineer)

Goal: upstream tests (RangeSetTest, RangeSetNDTest, NodeSetTest, NodeSetGroupTest)
pass with CONSORTIUM_BACKEND=rust. Baseline: 0 pass (collection errors), oracle: 309.

The parity tests, oracle (`lib/ClusterShell`), and bindings live in
consortium-tests. The bindings depend on the sibling consortium Rust core.
Build with maturin from `crates/consortium-py`.

## Layering
- `_consortium` (PyO3/Rust): full `RangeSet` (core-backed: parse/ops/index/getitem/
  slices/pickle), scalar `NodeSet` (core-backed: parse/ops/index/get/slice/split),
  `expand`/`fold` fns, full exception hierarchies for both modules.
- Shim `ClusterShell/RangeSet.py`: re-export rust RangeSet + `RangeSetND` ported
  from oracle (pure Python over rust RangeSet; core lacks nD folding).
- Shim `ClusterShell/NodeSet.py`: oracle port (NodeSetBase/ParsingEngine/NodeSet/
  group plumbing) over shim RangeSet/RangeSetND — core NodeSet has no groups/nD.
- Shim `ClusterShell/NodeUtils.py`: oracle port (GroupSource/UpcallGroupSource
  incl. mapall/cache_time/clear_cache, GroupResolver, GroupResolverConfig,
  YAMLGroupLoader, FileGroupSource) — subclassability + `_upcall_read` hook +
  `_cache` introspection are Python-level contracts used by tests.
- Shim `ClusterShell/Defaults.py`, `ClusterShell/Event.py`: oracle ports
  (pure Python; Event gains nothing new — ev_pickup already in oracle 1.10.1).

## Order
1. PyO3 RangeSet + exceptions + pickle → RangeSetTest
2. Shim RangeSetND → RangeSetNDTest
3. Shim Defaults + NodeUtils + NodeSet → NodeSetTest
4. NodeSetGroupTest
5. PyO3 NodeSet + expand/fold; Event shim
6. Validate all, cargo test, commit
