#!/usr/bin/env python3
"""Fan-out topology assertions over a real cascade event stream.

The benchmark used to record `cascade_copy: "ok"`, which is only the CLI's own
exit status. `prove_guest_gateway_relay` showed the relay was *available* - a
guest can reach its peer through the QEMU gateway with the credential
cascade-copy uses - but nothing showed the relay was *used*. A cascade that
pushed host-to-each-guest directly would satisfy every check the harness made.

consortium already emits the whole cascade as a tagged event stream
(`consortium_nix::cascade_events`, one JSON object per line, `{"kind": ...}`),
and its own fan-out tests build the tree from exactly that. These tests do the
same over a real run's stream, so the fleet proves the log-N peer-to-peer tree
was actually built rather than that a command exited 0.

Wire format is taken from consortium's own tests, not guessed:
`crates/consortium-cli/tests/cascade_viz_tests.rs`.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("cascade_tree.py")
SPEC = importlib.util.spec_from_file_location("cascade_tree", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load {MODULE_PATH}")
tree = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = tree
SPEC.loader.exec_module(tree)


def started(n_nodes: int, seeded: list[int], strategy: str = "log2-fanout") -> dict:
    return {"kind": "started", "n_nodes": n_nodes, "seeded": seeded,
            "strategy": strategy, "at": 0}


def edge_completed(round_: int, src: int, tgt: int) -> dict:
    return {"kind": "edge_completed", "round": round_, "src": src, "tgt": tgt,
            "duration": 1}


def finished(converged: int, failed: int, rounds: int) -> dict:
    return {"kind": "finished", "converged": converged, "failed": failed,
            "rounds": rounds}


def jsonl(events: list[dict]) -> str:
    return "".join(json.dumps(event) + "\n" for event in events)


# The generator below seeds node 0 plus the `n_seeded - 1` highest-numbered
# nodes. Those are the deepest leaves of the heap, so seeding them removes only
# their own inbound edge and leaves the relay structure of the remaining
# `n_nodes - n_seeded + 1` nodes untouched: those nodes are exactly the heap
# tree of ids 0..n_nodes-n_seeded, whose depth is the round count. Seeding the
# *front* of the heap instead would hand several sub-trees to the peers at once
# and shorten the run, so the stream would no longer be the level-synchronized
# one LevelTreeFanOut describes.
FANOUTS = (2, 3, 4)
# 64 is a full 2-ary tree; the rest are not, which is where the round count and
# the tree depth stop agreeing and a "just log2 it" expectation would break.
FLEET_SIZES = (7, 9, 15, 17, 63, 64, 65)
SEED_COUNTS = (1, 2, 4)


def fleet_shapes() -> list[tuple[int, int, int]]:
    """Every (n_nodes, fanout, n_seeded) combination the suite asserts over.

    Composed rather than hand-listed so a new fanout or fleet size joins every
    test at once instead of quietly skipping the ones nobody remembered.
    """
    return [(n_nodes, fanout, n_seeded)
            for n_nodes in FLEET_SIZES
            for fanout in FANOUTS
            for n_seeded in SEED_COUNTS
            if n_seeded <= n_nodes]


def seeded_nodes(n_nodes: int, n_seeded: int) -> list[int]:
    """The node ids a run of this shape starts with the payload already on.

    Node 0 is always seeded - it is the root the cascade fans out from - and the
    remaining seeds are the deepest leaves.
    """
    if not 1 <= n_seeded <= n_nodes:
        raise ValueError(f"cannot seed {n_seeded} of {n_nodes} node(s)")
    return [0] + list(range(n_nodes - n_seeded + 1, n_nodes))


def cascade_stream(n_nodes: int, fanout: int, n_seeded: int) -> tuple[str, dict]:
    """Build a level-synchronized cascade stream and the tree it describes.

    Ids are heap-style: the parent of node `i` is `(i - 1) // fanout`, which is
    the tree `LevelTreeFanOut` pre-shapes in
    `crates/consortium-nix/src/cascade_strategies.rs`. One round populates
    exactly one level, so a node at level `k` is served in round `k - 1` and
    `finished.rounds` is the depth of the pending heap - the value
    `expected_depth` independently computes.

    Returns the JSONL stream plus an oracle describing what a parser must read
    back out of it, so each assertion has a value computed here rather than
    restated in the test body.
    """
    seeds = seeded_nodes(n_nodes, n_seeded)
    is_seed = set(seeds)

    # A seeded node is already converged, so nothing serves it; every other
    # node is served by its heap parent.
    parent = {node: (node - 1) // fanout for node in range(1, n_nodes)
              if node not in is_seed}
    # Level of each node, from the heap parentage above. Node 0 is level 0.
    level = {0: 0}
    for node in range(1, n_nodes):
        level[node] = level[(node - 1) // fanout] + 1
    # Round `k - 1` serves level `k`, so the deepest pending level is the last
    # round index plus one.
    rounds = max((level[node] for node in parent), default=0)

    # Depth is measured from the nearest seeded node, so a seeded node is 0 and
    # every other node is one hop below its parent.
    depth_of = {node: 0 for node in seeds}
    for node in sorted(parent):
        depth_of[node] = depth_of[parent[node]] + 1

    children: dict[int, list[int]] = {}
    for node, source in sorted(parent.items()):
        children.setdefault(source, []).append(node)

    events = [started(n_nodes, seeds)]
    events += [edge_completed(level[node] - 1, source, node)
               for node, source in sorted(parent.items())]
    events.append(finished(n_nodes, 0, rounds))

    oracle = {
        "n_nodes": n_nodes,
        "seeded": seeds,
        "rounds": rounds,
        "converged": n_nodes,
        "parent": parent,
        "children": children,
        "depth_of": depth_of,
        "depth": max(depth_of.values()),
    }
    return jsonl(events), oracle


class CascadeTopologyTest(unittest.TestCase):
    """Parsing a real event stream into the tree the fan-out tests describe."""

    def test_a_log2_cascade_becomes_a_tree_of_the_right_shape(self) -> None:
        # Seed n0 serves n1 and n2; each of those serves two more. 7 nodes in
        # 2 rounds; the deepest node is 2 edges below the seed, so the tree has
        # 3 levels counting the seed as level 0.
        #
        # Round 1 carries all four level-2 nodes, not just n3 and n4. Splitting
        # that level across two rounds is a hand-authored skew, not the
        # strategy: `LevelTreeFanOut` populates exactly one tree level per
        # round, so n5 and n6 are served alongside n3 and n4 or not at all.
        events = jsonl([
            started(7, [0]),
            edge_completed(0, 0, 1), edge_completed(0, 0, 2),
            edge_completed(1, 1, 3), edge_completed(1, 1, 4),
            edge_completed(1, 2, 5), edge_completed(1, 2, 6),
            finished(7, 0, 2),
        ])
        topology = tree.parse_cascade_events(events)

        self.assertEqual(7, topology.n_nodes)
        self.assertEqual(2, topology.rounds)
        self.assertEqual(7, topology.converged_count)
        self.assertEqual(0, topology.failed_count)
        self.assertEqual({1: 0, 2: 0, 3: 1, 4: 1, 5: 2, 6: 2}, topology.parent)
        # edge-distance below a seeded node, per node id
        self.assertEqual({0: 0, 1: 1, 2: 1, 3: 2, 4: 2, 5: 2, 6: 2}, topology.depth_of)
        # depth is the longest relay chain below a seed: n6 is two hops from
        # n0. A host that pushed to every guest directly has depth 1.
        self.assertEqual(2, topology.depth)
        self.assertEqual({0: [1, 2], 1: [3, 4], 2: [5, 6]}, topology.children)
        # rounds is the depth of the tree counting the seed as level 0, which
        # is not the deepest chain: that is the level-vs-rounds distinction the
        # `depth` property exists to keep straight.
        self.assertEqual(2, topology.rounds)
        self.assertEqual(topology.expected_depth(fanout=2), topology.rounds)

    def test_every_fleet_shape_parses_into_the_tree_that_was_delivered(self) -> None:
        # The single 7-node fixture above could not tell a strategy that
        # happens to be right at N=7 from one that is right by accident. Each
        # shape gets a stream generated from the heap structure the strategy
        # documents, so `parent`, `children` and `depth_of` are checked against
        # an oracle computed here rather than restated per case.
        for n_nodes, fanout, n_seeded in fleet_shapes():
            with self.subTest(n_nodes=n_nodes, fanout=fanout, n_seeded=n_seeded):
                events, oracle = cascade_stream(n_nodes, fanout, n_seeded)
                topology = tree.parse_cascade_events(events)

                self.assertEqual(n_nodes, topology.n_nodes)
                self.assertEqual(oracle["rounds"], topology.rounds)
                self.assertEqual(n_nodes, topology.converged_count)
                self.assertEqual(0, topology.failed_count)
                self.assertEqual(tuple(oracle["seeded"]), topology.seeded)
                # every non-seeded node is served by exactly its heap parent
                self.assertEqual(oracle["parent"], topology.parent)
                self.assertEqual(oracle["children"], topology.children)
                # depth is measured from the nearest seeded node, not the root,
                # so a pre-seeded node restarts the count at 0
                self.assertEqual(oracle["depth_of"], topology.depth_of)
                self.assertEqual(oracle["depth"], topology.depth)

    def test_expected_depth_agrees_with_the_level_synchronized_round_count(self) -> None:
        # `expected_depth` is what a reader trusts to say how long a log-N run
        # should take. It has to agree with the rounds a real level-synchronized
        # cascade actually reports, at every shape - not just the one 7-node
        # case that used to pin it.
        for n_nodes, fanout, n_seeded in fleet_shapes():
            with self.subTest(n_nodes=n_nodes, fanout=fanout, n_seeded=n_seeded):
                events, oracle = cascade_stream(n_nodes, fanout, n_seeded)
                topology = tree.parse_cascade_events(events)
                self.assertEqual(
                    oracle["rounds"], topology.expected_depth(fanout=fanout),
                    f"{n_nodes} nodes at fanout {fanout} seeded {n_seeded}",
                )

    def test_the_round_count_is_not_merely_ceiling_log_fanout(self) -> None:
        # 15 nodes at fanout 2 is the case `LevelTreeFanOut`'s own tests assert
        # converges in exactly 3 rounds. It is also the case that distinguishes
        # the round count from a naive `ceil(log_fanout(n))`: round k delivers
        # `fanout ** k` nodes cumulatively, so 15 nodes is 2+4+8 = 3 rounds.
        # If this ever reads 4, the level arithmetic has drifted from the Rust.
        events, oracle = cascade_stream(n_nodes=15, fanout=2, n_seeded=1)
        topology = tree.parse_cascade_events(events)

        self.assertEqual(3, topology.rounds)
        self.assertEqual(3, oracle["rounds"])
        self.assertEqual(3, topology.expected_depth(fanout=2))

    def test_a_stream_that_never_finished_is_rejected(self) -> None:
        # A truncated stream would otherwise read as "converged everything we
        # happened to see", which is the exact false pass being closed here.
        # Checked across fleet shapes because truncation is a property of a
        # run, not of any one fleet size.
        for n_nodes, fanout, n_seeded in ((4, 2, 1), (7, 2, 1), (15, 3, 1),
                                          (64, 2, 4), (65, 3, 4)):
            with self.subTest(n_nodes=n_nodes, fanout=fanout, n_seeded=n_seeded):
                events, _ = cascade_stream(n_nodes, fanout, n_seeded)
                truncated = "".join(
                    line + "\n" for line in events.splitlines()[:-1])
                with self.assertRaisesRegex(tree.CascadeTreeError,
                                            "no finished event"):
                    tree.parse_cascade_events(truncated)

        events = jsonl([started(4, [0]), edge_completed(0, 0, 1)])
        with self.assertRaisesRegex(tree.CascadeTreeError, "no finished event"):
            tree.parse_cascade_events(events)

    def test_a_failing_edge_is_recorded_rather_than_counted_as_converged(self) -> None:
        # The run is green by the CLI's own account either way; only the counts
        # distinguish a node that converged from one that did not.
        for n_nodes, fanout, n_seeded in ((3, 2, 1), (7, 2, 1), (15, 3, 1),
                                          (64, 2, 4), (65, 3, 4)):
            with self.subTest(n_nodes=n_nodes, fanout=fanout, n_seeded=n_seeded):
                events = jsonl([
                    started(n_nodes, seeded_nodes(n_nodes, n_seeded)),
                    edge_completed(0, 0, 1),
                    {"kind": "edge_failed", "round": 0, "src": 1, "tgt": 2,
                     "error": "boom"},
                    finished(n_nodes - 1, 1, 1),
                ])
                topology = tree.parse_cascade_events(events)

                self.assertEqual(n_nodes - 1, topology.converged_count)
                self.assertEqual(1, topology.failed_count)



class RelayWasUsedTest(unittest.TestCase):
    """The point of the whole exercise: the relay must be load-bearing.

    Every one of these streams is a green cascade by the CLI's own account.
    Only the tree shape tells them apart.
    """

    def _star(self, n_nodes: int) -> str:
        # Host pushes to every guest directly: 1 round, depth 1, no relaying.
        events = [started(n_nodes, [0])]
        events += [edge_completed(0, 0, n) for n in range(1, n_nodes)]
        events.append(finished(n_nodes, 0, 1))
        return jsonl(events)

    def _tree(self, n_nodes: int) -> str:
        # Genuine log-N peer-to-peer cascade from the seed.
        depth_of = {0: 0}
        parent = {}
        events = [started(n_nodes, [0])]
        frontier = [0]
        rnd = 0
        while len(parent) < n_nodes - 1:
            nxt = []
            for src in frontier:
                for _ in range(2):
                    tgt = len(parent) + 1
                    if tgt >= n_nodes:
                        break
                    parent[tgt] = src
                    depth_of[tgt] = depth_of[src] + 1
                    events.append(edge_completed(rnd, src, tgt))
                    nxt.append(tgt)
                if tgt >= n_nodes:
                    break
            frontier = nxt
            rnd += 1
        events.append(finished(n_nodes, 0, rnd))
        return jsonl(events)

    def test_a_direct_push_to_every_guest_is_not_a_relayed_cascade(self) -> None:
        topology = tree.parse_cascade_events(self._star(8))
        with self.assertRaisesRegex(tree.CascadeTreeError, "relayed"):
            tree.assert_relay_was_used(topology, fanout=2)

    def test_a_real_log2_cascade_passes(self) -> None:
        topology = tree.parse_cascade_events(self._tree(8))
        tree.assert_relay_was_used(topology, fanout=2)

    def test_a_genuine_relay_passes_at_every_fleet_shape(self) -> None:
        # The positive case, swept. One 8-node log-2 run cannot show the check
        # holds for a fleet that is not a full tree, at a fanout other than 2,
        # or when part of the fleet is pre-seeded - and each of those changes
        # the depth and round count the check compares against.
        for n_nodes, fanout, n_seeded in fleet_shapes():
            with self.subTest(n_nodes=n_nodes, fanout=fanout, n_seeded=n_seeded):
                events, _ = cascade_stream(n_nodes, fanout, n_seeded)
                topology = tree.parse_cascade_events(events)
                tree.assert_relay_was_used(topology, fanout=fanout)

    def test_a_direct_push_is_rejected_at_every_fleet_size(self) -> None:
        # The negative case, swept, and deliberately against the same sizes the
        # positive sweep uses: a host push is a green cascade by every count
        # except the tree shape, so "rejects the star" must not depend on size.
        for n_nodes in (4, 8, 15, 64):
            with self.subTest(n_nodes=n_nodes):
                topology = tree.parse_cascade_events(self._star(n_nodes))
                with self.assertRaisesRegex(tree.CascadeTreeError, "relayed"):
                    tree.assert_relay_was_used(topology, fanout=2)

    def test_a_run_that_took_the_wrong_number_of_rounds_is_rejected(self) -> None:
        # A real relay, but one round too slow - the shape a coordinator using
        # a different strategy than the one it reports would produce. The tree
        # is genuinely relayed, so only the round count gives it away.
        #
        # Restricted to fleets with more pending nodes than the fanout, because
        # `assert_relay_was_used` returns early below that: a seed that could
        # serve every remaining node itself has not relayed, by definition, so
        # there is no round count to disagree with. Those shapes are covered as
        # passing cases by the positive sweep instead.
        shapes = [(n_nodes, fanout, n_seeded)
                  for n_nodes, fanout, n_seeded in fleet_shapes()
                  if n_nodes - n_seeded > fanout]
        self.assertTrue(shapes, "expected at least one fleet past the fanout")
        for n_nodes, fanout, n_seeded in shapes:
            with self.subTest(n_nodes=n_nodes, fanout=fanout, n_seeded=n_seeded):
                events, oracle = cascade_stream(n_nodes, fanout, n_seeded)
                # Inflate the reported round count so it no longer matches the
                # level the tree was actually delivered over.
                stretched = events.replace(
                    f'"rounds": {oracle["rounds"]}',
                    f'"rounds": {oracle["rounds"] + 1}', 1)
                topology = tree.parse_cascade_events(stretched)
                with self.assertRaisesRegex(tree.CascadeTreeError, "needs"):
                    tree.assert_relay_was_used(topology, fanout=fanout)

    def test_the_depth_must_match_log2_of_the_node_count(self) -> None:
        # 8 nodes, fanout 2 -> depth exactly 3. A tree of depth 5 means the
        # coordinator was not using the strategy the run claims to report.
        topology = tree.parse_cascade_events(self._tree(8))
        self.assertEqual(3, topology.expected_depth(fanout=2))

    def test_a_fleet_of_one_is_trivially_converged(self) -> None:
        # A single seeded node has nothing to relay to and nothing to fetch.
        topology = tree.parse_cascade_events(
            jsonl([started(1, [0]), finished(1, 0, 0)])
        )
        tree.assert_relay_was_used(topology, fanout=2)
        self.assertEqual(0, topology.depth)

    def test_a_fleet_smaller_than_the_fanout_never_relays(self) -> None:
        # Nothing to relay to: with 2 nodes the seed serves the other one.
        topology = tree.parse_cascade_events(self._tree(2))
        tree.assert_relay_was_used(topology, fanout=2)

    def test_no_node_may_be_served_twice(self) -> None:

        events = jsonl([
            started(4, [0]),
            edge_completed(0, 0, 1), edge_completed(0, 0, 2),
            edge_completed(1, 0, 3), edge_completed(1, 1, 3),
            finished(4, 0, 2),
        ])
        topology = tree.parse_cascade_events(events)
        with self.assertRaisesRegex(tree.CascadeTreeError, "twice"):
            tree.assert_relay_was_used(topology, fanout=2)


if __name__ == "__main__":
    unittest.main()
