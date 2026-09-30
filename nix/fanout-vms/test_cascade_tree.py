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


class CascadeTopologyTest(unittest.TestCase):
    """Parsing a real event stream into the tree the fan-out tests describe."""

    def test_a_log2_cascade_becomes_a_tree_of_the_right_shape(self) -> None:
        # Seed n0 serves n1 and n2; each of those serves two more. 7 nodes in
        # 3 rounds; the deepest node is 2 edges below the seed, so the tree has
        # 3 levels counting the seed as level 0.
        events = jsonl([
            started(7, [0]),
            edge_completed(0, 0, 1), edge_completed(0, 0, 2),
            edge_completed(1, 1, 3), edge_completed(1, 1, 4),
            edge_completed(2, 2, 5), edge_completed(2, 2, 6),
            finished(7, 0, 3),
        ])
        topology = tree.parse_cascade_events(events)

        self.assertEqual(7, topology.n_nodes)
        self.assertEqual(3, topology.rounds)
        self.assertEqual(7, topology.converged_count)
        self.assertEqual(0, topology.failed_count)
        self.assertEqual({1: 0, 2: 0, 3: 1, 4: 1, 5: 2, 6: 2}, topology.parent)
        # edge-distance below a seeded node, per node id
        self.assertEqual({0: 0, 1: 1, 2: 1, 3: 2, 4: 2, 5: 2, 6: 2}, topology.depth_of)
        # depth is the longest relay chain below a seed: n6 is two hops from
        # n0. A host that pushed to every guest directly has depth 1.
        self.assertEqual(2, topology.depth)
        self.assertEqual({0: [1, 2], 1: [3, 4], 2: [5, 6]}, topology.children)
        # 7 nodes at fanout 2 still needs 3 rounds: the last round carries
        # only one node. That is what log-N means - not a balanced tree.
        self.assertEqual(3, topology.rounds)
        self.assertEqual(topology.expected_depth(fanout=2), topology.rounds)

    def test_a_stream_that_never_finished_is_rejected(self) -> None:
        # A truncated stream would otherwise read as "converged everything we
        # happened to see", which is the exact false pass being closed here.
        events = jsonl([started(4, [0]), edge_completed(0, 0, 1)])
        with self.assertRaisesRegex(tree.CascadeTreeError, "no finished event"):
            tree.parse_cascade_events(events)

    def test_a_failing_edge_is_recorded_rather_than_counted_as_converged(self) -> None:
        events = jsonl([
            started(3, [0]),
            edge_completed(0, 0, 1),
            {"kind": "edge_failed", "round": 0, "src": 1, "tgt": 2,
             "error": "boom"},
            finished(2, 1, 1),
        ])
        topology = tree.parse_cascade_events(events)

        self.assertEqual(2, topology.converged_count)
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
