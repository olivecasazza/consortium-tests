#!/usr/bin/env python3
"""NodeSet demo — the ClusterShell Python API, backed by consortium's Rust core.

This mirrors examples/rust/nodeset_algebra.rs through the Python bindings.
The bindings live in crates/consortium-py and are imported as the
`ClusterShell` package (a drop-in replacement for upstream ClusterShell).

Installation (from the repository root):

    pip install crates/consortium-py
    # or, for development:
    cd crates/consortium-py && maturin develop

Then run:

    python3 examples/python/nodeset_demo.py
"""

from ClusterShell.NodeSet import NodeSet, expand, fold


def banner(title):
    print(f"\n=== {title} ===")


def main():
    banner("Parse and fold")
    ns = NodeSet("node[01-05],login1")
    print("parsed  : node[01-05],login1")
    print("len     :", len(ns))
    print("folded  :", ns)

    banner("expand / fold free functions")
    print('expand("node[1-3]") ->', list(expand("node[1-3]")))
    print('fold("node1,node2,node3") ->', fold("node1,node2,node3"))

    banner("Membership and indexing")
    print('"node03" in ns :', "node03" in ns)
    print('"node99" in ns :', "node99" in ns)
    print('ns.index("node03"):', ns.index("node03"))
    print("ns[0]            :", ns[0])
    print("iter             :", list(ns))

    banner("Set algebra with operators")
    a = NodeSet("node[01-05]")
    b = NodeSet("node[04-08]")
    print("a     :", a)
    print("b     :", b)
    print("a | b :", a | b)  # union
    print("a & b :", a & b)  # intersection
    print("a - b :", a - b)  # difference
    print("a ^ b :", a ^ b)  # symmetric difference

    banner("update (in-place union with a pattern)")
    growing = NodeSet("node[01-05]")
    growing.update("node[06-07]")
    growing.update("login2")
    print("after updates:", growing)

    banner("split into N sub-nodesets")
    for i, part in enumerate(NodeSet("node[01-06]").split(2)):
        print(f"split 2 [{i}]: {part}")

    banner("contiguous sub-sets")
    for part in NodeSet("node[01-04],node[07-09]").contiguous():
        print("contiguous:", part)


if __name__ == "__main__":
    main()
