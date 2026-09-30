"""Fan-out topology of a real cascade run, read from its own event stream.

The benchmark used to report `cascade_copy: "ok"`, which is the cascade CLI's
own exit status. That is not evidence the relay was used: a cascade that pushed
the closure from the host to each guest in turn satisfies every other check the
harness makes. `prove_guest_gateway_relay` shows the peer path is *available* -
a guest can reach its neighbour through the QEMU gateway with the credential
cascade-copy uses - and nothing showed it was *taken*.

consortium emits its whole cascade as a tagged event stream
(`consortium_nix::cascade_events`, one JSON object per line, discriminated by
`{"kind": ...}`), and its fan-out tests build the tree from exactly that. This
reads the same stream from a real 64-node run and asserts the tree's shape, so
the fleet proves a log-N peer-to-peer cascade happened rather than that a
command exited zero.

The wire format is taken from consortium's own tests
(`crates/consortium-cli/tests/cascade_viz_tests.rs`), not guessed:

    {"kind":"started","n_nodes":2,"seeded":[0],"strategy":"log2-fanout","at":0}
    {"kind":"edge_completed","round":0,"src":0,"tgt":1,"duration":100000000}
    {"kind":"finished","converged":2,"failed":0,"rounds":1}

This module is deliberately free of Nix and of QEMU: it is the generic part of
the fan-out/fan-in architecture, the part that has nothing to do with what is
being distributed. The cascade primitive itself still lives in `consortium-nix`
and reaches Nix through its `RoundExecutor`; that is the coupling worth breaking,
and it is out of scope here.
"""

from __future__ import annotations


import json
import math
from dataclasses import dataclass, field


class CascadeTreeError(RuntimeError):
    """A cascade run's event stream does not describe a tree we can trust."""


@dataclass(frozen=True)
class CascadeTopology:
    """The parent/child tree a cascade actually built.

    `parent` maps each node that received the payload to the node that served
    it, which is the whole record of whether peers relayed for each other. The
    seed is not in it: it was never served.
    """

    n_nodes: int
    seeded: tuple[int, ...]
    strategy: str
    rounds: int
    converged_count: int
    failed_count: int
    parent: dict[int, int]
    completed_edges: int = 0
    children: dict[int, list[int]] = field(default_factory=dict)
    depth_of: dict[int, int] = field(default_factory=dict)

    @property
    def depth(self) -> int:
        """The longest relay chain below a seeded node, in edges.

        A cascade that served every node straight from the seed has depth 1
        however many nodes it reached; a log-N relay over n nodes has a chain
        of ceil(log_N(n)) edges. This is not the round count: over 7 nodes at
        fanout 2 the last round carries one node, so rounds is 3 while the
        deepest chain is 2.
        """
        return max(self.depth_of.values(), default=0)

    def expected_depth(self, fanout: int) -> int:
        """Levels a log-N cascade over this many nodes needs.

        Seeded nodes are already converged, so they do not add a level.
        """
        if fanout < 1:
            raise CascadeTreeError(f"fanout must be at least 1, got {fanout}")
        pending = max(0, self.n_nodes - len(self.seeded))
        levels = 0
        while pending > 0:
            pending = pending - pending if pending < fanout else pending // fanout
            levels += 1
        return levels


def parse_cascade_events(stream: str) -> CascadeTopology:
    """Build the tree from a cascade's JSONL event stream.

    Only `edge_completed` writes a parent, so a node the run failed to deliver
    to is absent from `parent` rather than being recorded with a guessed
    source. That is deliberate: the harness asserts on what was served, and a
    failure surfaces as a missing node the caller can name.
    """
    started: dict[str, object] | None = None
    parent: dict[int, int] = {}
    children: dict[int, list[int]] = {}
    completed_edges = 0
    finished: dict[str, object] | None = None

    for lineno, line in enumerate(stream.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise CascadeTreeError(f"line {lineno} is not JSON: {error}") from error
        if not isinstance(event, dict):
            raise CascadeTreeError(f"line {lineno} is not a JSON object")
        kind = event.get("kind")
        if kind == "started":
            started = event
        elif kind == "edge_completed":
            src, tgt = int(event["src"]), int(event["tgt"])
            parent[tgt] = src
            completed_edges += 1
            children.setdefault(src, []).append(tgt)
        elif kind == "finished":
            finished = event

    if started is None:
        raise CascadeTreeError("event stream has no started event")
    if finished is None:
        raise CascadeTreeError(
            "event stream has no finished event; the run was truncated, so its "
            "nodes cannot be called converged"
        )

    seeded = tuple(int(node) for node in started.get("seeded", []))
    return CascadeTopology(
        n_nodes=int(started["n_nodes"]),
        seeded=seeded,
        strategy=str(started.get("strategy", "")),
        rounds=int(finished.get("rounds", 0)),
        converged_count=int(finished.get("converged", 0)),
        failed_count=int(finished.get("failed", 0)),
        parent=parent,
        completed_edges=completed_edges,
        children=children,
        depth_of=_depths(seeded, parent),
    )


def _depths(seeded: tuple[int, ...], parent: dict[int, int]) -> dict[int, int]:
    """Edge-distance below the nearest seeded node, per node.

    An edge cannot serve a node before its source is converged, so the tree
    grows strictly downwards and one pass in edge-insertion order settles it.
    A cycle - which no real cascade produces, and which would otherwise hang -
    leaves the node absent rather than looping.
    """
    depths = {node: 0 for node in seeded}
    for node in parent:
        source = parent[node]
        depth = depths.get(source)
        if depth is None:
            continue
        depths[node] = depth + 1
    return depths


def assert_relay_was_used(topology: CascadeTopology, fanout: int) -> None:
    """Assert the run was a peer-to-peer cascade, not a host push.

    Every stream this rejects is a green cascade by the CLI's own account. The
    tree shape is the only thing that distinguishes them.
    """
    served_twice = sorted(
        node for node, kids in topology.children.items() if len(kids) != len(set(kids))
    )
    if served_twice:
        raise CascadeTreeError(
            f"node(s) {served_twice} appear as a target more than once; the "
            "relay tree is not a tree"
        )
    # Reused as a parent after being served twice is the same defect seen from
    # the other side, and is caught above; here we only check the served set.

    unserved = sorted(
        node
        for node in range(topology.n_nodes)
        if node not in topology.seeded and node not in topology.parent
    )
    if unserved:
        raise CascadeTreeError(
            f"node(s) {unserved} were never served by anyone; the cascade did "
            f"not converge ({topology.converged_count}/{topology.n_nodes} did)"
        )

    # `completed_edges` counts every successful delivery. If that is more than
    # the number of distinct targets, something was served twice.
    if topology.completed_edges != len(topology.parent):
        raise CascadeTreeError(
            f"{topology.completed_edges} deliveries landed on "
            f"{len(topology.parent)} distinct nodes; a node was served twice, "
            "so the relay is not a tree"
        )

    # Relay was only possible if the seed could not have served everyone
    # itself in one round. Counting *unserved* nodes here would wave through
    # exactly the case this exists to catch: a host push reaches every node and
    # leaves none outstanding.
    if topology.n_nodes - len(topology.seeded) <= fanout:
        return

    if topology.depth <= 1:
        raise CascadeTreeError(
            f"the payload was not relayed: all {topology.n_nodes} nodes were "
            "served straight from the seed, so no peer served another"
        )

    expected = topology.expected_depth(fanout)
    if topology.rounds != expected:
        raise CascadeTreeError(
            f"a log-{fanout} cascade over {topology.n_nodes} nodes needs "
            f"{expected} round(s); this run took {topology.rounds}"
        )
