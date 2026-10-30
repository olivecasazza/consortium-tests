#!/usr/bin/env python3
"""Nix-native smoke test for the Rust-backed ClusterShell Python API.

Exercises real consumer-visible Rust<->Python behavior, end to end:

  1. the compiled PyO3 extension ``ClusterShell._consortium`` is importable;
  2. ``ClusterShell.RangeSet`` hands out the native RangeSet and the Rust
     folding algorithm honors the ``autostep`` property (fold/unfold);
  3. the vendored ``ClusterShell.NodeSet`` shim folds/expands node patterns
     by delegating ``str(RangeSet)`` to the extension (NodeSetBase.__str__).
     Expected values are the module's own documented example.

Deterministic and hermetic: no network, no oracle checkout. Requires the
compiled extension (maturin-built ``_consortium`` cdylib) to be importable as
``ClusterShell._consortium`` (e.g. installed into the ClusterShell package
directory with the package on PYTHONPATH).

Exit codes: 0 = OK, 2 = extension missing, 1 = behavior mismatch.
"""

import os
import sys

# This gate exercises the Rust backend specifically; never let an ambient
# CONSORTIUM_BACKEND redirect ClusterShell to the pure-Python oracle.
os.environ["CONSORTIUM_BACKEND"] = "rust"


def expect(got, want, what):
    if got != want:
        raise AssertionError("%s: got %r, want %r" % (what, got, want))


def main():
    # 1. The compiled extension must be present and importable.
    try:
        import ClusterShell._consortium as native
    except ImportError as exc:
        print(
            "nix_smoke: FAIL: cannot import the ClusterShell._consortium PyO3 "
            "extension (is the compiled _consortium cdylib importable as "
            "'ClusterShell._consortium'?): %s" % exc,
            file=sys.stderr,
        )
        return 2

    # 2. RangeSet: the shim must re-export the native class and the Rust
    # folding must honor autostep. Default autostep is disabled, so step-2
    # elements stay unfolded; autostep=3 collapses them to a /step slice.
    from ClusterShell.RangeSet import RangeSet

    if RangeSet is not native.RangeSet:
        raise AssertionError(
            "ClusterShell.RangeSet.RangeSet must be the native extension "
            "class, not a Python reimplementation"
        )

    expect(str(RangeSet("0-8/2")), "0,2,4,6,8", "str(RangeSet('0-8/2')) default autostep")
    rs = RangeSet("0-8/2", autostep=3)
    expect(rs.autostep, 3, "autostep property read-back")
    expect(str(rs), "0-8/2", "str(RangeSet('0-8/2', autostep=3))")
    rs.autostep = None  # disabling stepping must unfold on the next fold
    expect(str(rs), "0,2,4,6,8", "str() after rs.autostep = None")

    # 3. NodeSet shim on top of the native RangeSet. NodeSetBase.__str__
    # renders patterns via str(rset) of the Rust-backed RangeSet.
    from ClusterShell.NodeSet import NodeSet, expand, fold

    ns = NodeSet("cluster[1-30]")
    ns.update("cluster32")
    ns.difference_update("cluster[2-5,8-31]")
    expect(str(ns), "cluster[1,6-7,32]", "NodeSet pdsh-style folding")
    expect(list(ns), ["cluster1", "cluster6", "cluster7", "cluster32"], "NodeSet iteration")
    expect(fold("node[1-5,6,7-10]"), "node[1-10]", "fold()")
    expect(
        expand("node[1,3-5,7-10]"),
        ["node1", "node3", "node4", "node5", "node7", "node8", "node9", "node10"],
        "expand()",
    )
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except AssertionError as exc:
        print("nix_smoke: FAIL: %s" % exc, file=sys.stderr)
        code = 1
    if code == 0:
        print("nix_smoke: OK (rust RangeSet autostep folding + NodeSet shim)")
    sys.exit(code)
