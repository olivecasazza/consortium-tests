#!/usr/bin/env python3
"""RangeSet demo — numeric range sets, backed by consortium's Rust core.

This mirrors examples/rust/rangeset_basics.rs through the Python bindings.
The bindings live in crates/consortium-py and are imported as the
`ClusterShell` package (a drop-in replacement for upstream ClusterShell);
`ClusterShell.RangeSet.RangeSet` is the Rust-backed implementation.

Installation (from the repository root):

    pip install crates/consortium-py
    # or, for development:
    cd crates/consortium-py && maturin develop

Then run:

    python3 examples/python/rangeset_demo.py
"""

from ClusterShell.RangeSet import RangeSet


def banner(title):
    print(f"\n=== {title} ===")


def main():
    banner("Parse and display (folded form)")
    rs = RangeSet("1-4,6,8-12")
    print("parsed  : 1-4,6,8-12")
    print("len     :", len(rs))
    print("folded  :", rs)
    print("sorted  :", list(rs))

    banner("add_range — half-open [start, stop) with step")
    built = RangeSet()
    built.add_range(0, 10, 2)  # 0,2,4,6,8
    print("add_range(0, 10, 2) :", built)

    banner("Set algebra with operators")
    a = RangeSet("1-10")
    b = RangeSet("5-15")
    print("a     :", a)
    print("b     :", b)
    print("a | b :", a | b)  # union
    print("a & b :", a & b)  # intersection
    print("a - b :", a - b)  # difference
    print("a ^ b :", a ^ b)  # symmetric difference

    banner("Iteration, membership, contiguous sub-sets")
    rs = RangeSet("1-5,9-10")
    print("iter        :", list(rs))
    print("3 in rs     :", 3 in rs)
    print("9 in rs     :", 9 in rs)
    for part in rs.contiguous():
        print("contiguous  :", part)

    banner("Autostep — fold 0,2,4,...,16 as 0-16/2")
    stepped = RangeSet("0,2,4,6,8,10,12,14,16", autostep=3)
    print("with autostep=3 :", stepped)
    plain = RangeSet("0,2,4,6,8,10,12,14,16")
    print("without autostep:", plain)

    banner("split into N sub-sets")
    for i, part in enumerate(RangeSet("1-8").split(2)):
        print(f"split 2 [{i}]: {part}")


if __name__ == "__main__":
    main()
