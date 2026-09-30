"""Tests for the harness side of the cascade relay check.

`cascade_copy: "ok"` in the result JSON is the cascade CLI's exit status.
Nothing in it says the relay was used: a cascade that pushed the payload from
the host to each guest in turn exits 0 and would satisfy every other check the
harness makes. These tests pin the two pieces of wiring that turn that string
into evidence — the command must ask for the event stream, and the stream must
be turned into a verified topology or the run fails.

The properties below are swept over many fleet shapes rather than asserted once
at 64 nodes with a fanout of 2. A single shape can be satisfied by a checker
that happens to be right about that one tree: `depth > 1` and `rounds == 6` are
both true of the 64-node case for reasons that do not generalize, and the fleet
sizes here deliberately include partial trees (15, 63, 65), which is where an
off-by-one in the round count hides. Every expectation is re-derived inside the
test from the strategy's own arithmetic, so the sweep is an independent oracle
rather than a restatement of whatever `cascade_tree` happens to compute.
"""

from __future__ import annotations

import importlib.util
import json
import shlex
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

BENCH_PATH = Path(__file__).with_name("bench.py")
SPEC = importlib.util.spec_from_file_location("fanout_vm_bench", BENCH_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load {BENCH_PATH}")
bench = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bench
SPEC.loader.exec_module(bench)

TREE_PATH = Path(__file__).with_name("cascade_tree.py")
TREE_SPEC = importlib.util.spec_from_file_location("cascade_tree_under_test", TREE_PATH)
if TREE_SPEC is None or TREE_SPEC.loader is None:
    raise RuntimeError(f"cannot load {TREE_PATH}")
tree = importlib.util.module_from_spec(TREE_SPEC)
sys.modules[TREE_SPEC.name] = tree
TREE_SPEC.loader.exec_module(tree)

# Fleet sizes for the sweep. 8 and 16 are exact powers of two; 15, 63 and 65
# are not, so they leave a partial final round — the level that a
# "pending //= fanout" round count gets wrong. 64 is the shape the harness
# actually launches, and 65 is the one-node-larger fleet that shows the count
# is derived from the tree rather than from a constant.
FLEET_SIZES = (8, 15, 16, 63, 64, 65)
# Every fanout the fleet size list is swept at. 3 in particular never appears in
# a power-of-two fleet, so a checker that had only ever seen binary trees would
# not survive it.
FANOUTS = (2, 3, 4)


def _rounds_for_fleet(n_nodes: int, fanout: int) -> int:
    """Rounds a log-N cascade needs, re-derived from the strategy's arithmetic.

    `LevelTreeFanOut` in consortium's `cascade_strategies.rs` gives node `i` the
    parent `(i - 1) / fanout` and advances exactly one tree level per round, so
    round `k` has room for `fanout ** k` nodes. This is written out here rather
    than calling `CascadeTopology.expected_depth`, because a test that asks the
    module under test what the answer is cannot fail.
    """
    delivered = 0
    rounds = 0
    while delivered < n_nodes - 1:
        rounds += 1
        delivered += fanout**rounds
    return rounds


def _naive_rounds_for_fleet(n_nodes: int, fanout: int) -> int:
    """The wrong round count, spelled out so the sweep can reject it.

    Dividing the pending count down each round throws away the partial level,
    which over-counts whenever the fleet is not an exact full tree. It is here
    as a counter-oracle: a stream claiming *these* rounds must be refused,
    which is what distinguishes a real capacity check from a formula that
    merely happens to agree on the powers of two.
    """
    pending = n_nodes - 1
    rounds = 0
    while pending:
        pending //= fanout
        rounds += 1
    return rounds


def _relay_depth(n_nodes: int, fanout: int) -> int:
    """Deepest relay chain over `n_nodes`, re-derived independently.

    Distance `k` below the seed holds at most `fanout ** k` nodes, so the
    deepest node is the last level the fleet actually reaches. Written out
    rather than taken from the summary so that a parser which reported the
    round count here instead of the depth would be caught.
    """
    placed = 1
    depth = 0
    while placed < n_nodes:
        depth += 1
        placed += fanout**depth
    return depth


def relayed_stream(n_nodes: int, fanout: int = 2) -> str:
    """A well-formed log-N cascade stream over n_nodes, as cascade-copy emits."""
    events: list[dict] = [
        {"kind": "started", "n_nodes": n_nodes, "seeded": [0],
         "strategy": "log2-fanout", "at": 0}
    ]
    parent: dict[int, int] = {}
    depth_of = {0: 0}
    frontier = [0]
    rnd = 0
    while len(parent) < n_nodes - 1:
        nxt: list[int] = []
        for src in frontier:
            for _ in range(fanout):
                tgt = len(parent) + 1
                if tgt >= n_nodes:
                    break
                parent[tgt] = src
                depth_of[tgt] = depth_of[src] + 1
                events.append({"kind": "edge_completed", "round": rnd,
                               "src": src, "tgt": tgt, "duration": 1})
                nxt.append(tgt)
            if len(parent) >= n_nodes - 1:
                break
        frontier = nxt
        rnd += 1
    events.append({"kind": "finished", "converged": n_nodes, "failed": 0,
                   "rounds": rnd})
    return "".join(json.dumps(e) + "\n" for e in events)


def host_push_stream(n_nodes: int) -> str:
    """A green cascade that never relayed: the seed serves everyone."""
    events: list[dict] = [
        {"kind": "started", "n_nodes": n_nodes, "seeded": [0],
         "strategy": "log2-fanout", "at": 0}
    ]
    events += [{"kind": "edge_completed", "round": 0, "src": 0, "tgt": n,
                "duration": 1} for n in range(1, n_nodes)]
    events.append({"kind": "finished", "converged": n_nodes, "failed": 0,
                   "rounds": 1})
    return "".join(json.dumps(e) + "\n" for e in events)


def multi_seed_relayed_stream(n_nodes: int, fanout: int, seeds: int) -> str:
    """A relayed stream over a fleet that started with `seeds` nodes converged.

    Several seeded nodes are the shape a resuming or partially-cached run
    reports: they are converged before any round starts, so they are not
    counted in `relayed_nodes` and they do not add a level. Kept separate from
    `relayed_stream` so that helper's meaning — one seed, the plain case — is
    not stretched to cover a shape it was never written for.

    The relaying sub-tree hangs off the *last* seed, not off all of them at
    once. That is deliberate: `CascadeTopology.expected_depth` is a
    single-root capacity model, so the fleet a pre-seeded stream describes has
    to be one root's worth of `n_nodes - seeds` pending nodes. A forest
    growing from every seed simultaneously fills faster and is correctly
    rejected as not describing this strategy.
    """
    if not 1 <= seeds < n_nodes:
        raise ValueError(f"cannot seed {seeds} of {n_nodes} nodes")
    events: list[dict] = [
        {"kind": "started", "n_nodes": n_nodes, "seeded": list(range(seeds)),
         "strategy": "log2-fanout", "at": 0}
    ]
    root = seeds - 1
    frontier = [root]
    next_tgt = seeds
    rnd = 0
    while next_tgt < n_nodes:
        nxt: list[int] = []
        for src in frontier:
            for _ in range(fanout):
                if next_tgt >= n_nodes:
                    break
                events.append({"kind": "edge_completed", "round": rnd,
                               "src": src, "tgt": next_tgt, "duration": 1})
                nxt.append(next_tgt)
                next_tgt += 1
            if next_tgt >= n_nodes:
                break
        frontier = nxt
        rnd += 1
    events.append({"kind": "finished", "converged": n_nodes, "failed": 0,
                   "rounds": rnd})
    return "".join(json.dumps(e) + "\n" for e in events)


class CascadeCommandTest(unittest.TestCase):
    def test_the_command_asks_for_the_event_stream(self) -> None:
        # Without this the CLI wires a NullSink over a non-TTY stdout and the
        # run emits nothing at all, which is why the harness could only ever
        # record an exit status.
        command = shlex.split(
            bench.cascade_command(Path("/nix/store/abc"), "/tmp/inv", fanout=2)
        )
        self.assertIn("--format", command)
        self.assertEqual("jsonl", command[command.index("--format") + 1])
        self.assertIn("log2-fanout", command)

    def test_the_command_asks_for_the_event_stream_at_every_fanout(self) -> None:
        # The strategy is the only thing that varies the stream's shape, so a
        # fanout that reached the command line as a default would still produce
        # a parseable log-2 tree over any fleet size and go unnoticed.
        for fanout in FANOUTS:
            with self.subTest(fanout=fanout):
                command = shlex.split(
                    bench.cascade_command(
                        Path("/nix/store/abc"), "/tmp/inv", fanout=fanout
                    )
                )
                self.assertIn("--format", command)
                self.assertEqual("jsonl", command[command.index("--format") + 1])
                self.assertIn("--fanout", command)
                self.assertEqual(
                    str(fanout), command[command.index("--fanout") + 1]
                )


class CascadeRelayVerificationTest(unittest.TestCase):
    def test_a_relayed_cascade_is_reported_with_its_shape(self) -> None:
        summary = bench.verify_cascade_relay(relayed_stream(64), count=64, fanout=2)

        self.assertEqual(64, summary["nodes"])
        self.assertEqual(6, summary["rounds"])
        self.assertGreater(summary["relay_depth"], 1)
        # 63 of the 64 were served by a peer; the seed was never served by
        # anyone, which is why this is not equal to `nodes`.
        self.assertEqual(63, summary["relayed_nodes"])

    def test_a_host_push_that_exited_zero_is_rejected(self) -> None:
        # This is the whole point. The cascade succeeds; the fleet did not
        # relay; the run must fail rather than record "ok".
        with self.assertRaisesRegex(bench.HarnessError, "relay"):
            bench.verify_cascade_relay(host_push_stream(64), count=64, fanout=2)

    def test_the_node_count_must_match_the_fleet(self) -> None:
        # A cascade that converged over the wrong fleet is not evidence about
        # this one.
        with self.assertRaisesRegex(bench.HarnessError, "nodes"):
            bench.verify_cascade_relay(relayed_stream(8), count=64, fanout=2)

    def test_an_empty_stream_is_not_a_passing_cascade(self) -> None:
        # Guards a silent regression: if the emitter stops emitting, an empty
        # capture must fail loudly, not quietly verify nothing.
        with self.assertRaises(bench.HarnessError):
            bench.verify_cascade_relay("", count=64, fanout=2)

    def test_a_relayed_cascade_reports_the_shape_the_tree_implies(self) -> None:
        """Sweep the accepted path over every fleet shape.

        The reported `rounds` and `relay_depth` are compared against the
        strategy arithmetic re-derived in this module, not against whatever the
        parser produced — otherwise the sweep would agree with an off-by-one by
        construction. `relayed_nodes` is `n - 1` because the seed is never
        served by anyone, which is the one number that can be silently wrong
        without any other assertion noticing.
        """
        for n_nodes in FLEET_SIZES:
            for fanout in FANOUTS:
                with self.subTest(n_nodes=n_nodes, fanout=fanout):
                    summary = bench.verify_cascade_relay(
                        relayed_stream(n_nodes, fanout),
                        count=n_nodes,
                        fanout=fanout,
                    )
                    self.assertEqual(n_nodes, summary["nodes"])
                    self.assertEqual(
                        _rounds_for_fleet(n_nodes, fanout), summary["rounds"]
                    )
                    self.assertEqual(
                        _relay_depth(n_nodes, fanout), summary["relay_depth"]
                    )
                    self.assertEqual(n_nodes - 1, summary["relayed_nodes"])

    def test_the_documented_rust_example_is_what_this_harness_counts(self) -> None:
        """Pin the example consortium's own strategy docs give for log-2.

        15 nodes at a fanout of 2 is the worked example in
        `cascade_strategies.rs`. Rounds 1 and 2 deliver 2 + 4 = 6 nodes, which
        is not the 14 that are pending, so the run needs a third. If the
        harness's own arithmetic drifts from the Rust's, this is where it shows:
        the number is spelled out here rather than computed.
        """
        summary = bench.verify_cascade_relay(
            relayed_stream(15, 2), count=15, fanout=2
        )
        self.assertEqual(15, summary["nodes"])
        self.assertEqual(3, summary["rounds"])
        self.assertEqual(3, summary["relay_depth"])
        self.assertEqual(14, summary["relayed_nodes"])

    def test_a_round_count_that_drops_a_partial_level_is_rejected(self) -> None:
        """Pin the off-by-one that a `pending //= fanout` count gets wrong.

        Dividing the pending count down discards the partial level on every
        step, so it over-counts the rounds a ragged fleet needs. Over 15 nodes
        at a fanout of 2 it says 4 where the strategy needs 3: 2 then 4 nodes
        is 6, and 2 + 4 + 8 = 14 is still one short of the 14 pending nodes,
        so a third round is required. 63 and 65 show the same disagreement, and
        65 at a fanout of 4 is the ragged case at a width where it is easy to
        miss.

        The stream's rounds are rewritten to the naive count, so each case here
        fails if the checker stops comparing against the real capacity. Cases
        where the two formulas coincide are skipped rather than asserted
        vacuously — a fleet that cannot tell the two apart proves nothing.
        """
        checked = 0
        for n_nodes in FLEET_SIZES:
            for fanout in FANOUTS:
                naive = _naive_rounds_for_fleet(n_nodes, fanout)
                if naive == _rounds_for_fleet(n_nodes, fanout):
                    continue
                checked += 1
                with self.subTest(n_nodes=n_nodes, fanout=fanout, naive=naive):
                    events = [
                        json.loads(line)
                        for line in relayed_stream(n_nodes, fanout).splitlines()
                    ]
                    for event in events:
                        if event["kind"] == "finished":
                            event["rounds"] = naive
                    stream = "".join(json.dumps(e) + "\n" for e in events)
                    with self.assertRaisesRegex(bench.HarnessError, "round"):
                        bench.verify_cascade_relay(
                            stream, count=n_nodes, fanout=fanout
                        )
        # The fleet sizes have to actually disagree somewhere, or this test is
        # passing without exercising anything.
        self.assertGreater(checked, 0)

    def test_a_host_push_that_exited_zero_is_rejected_at_every_shape(self) -> None:
        """The central property, swept rather than asserted once.

        A checker that only recognized the 64-node host push as a push would
        pass this suite on a fleet that grows. Every shape here converges
        cleanly and exits 0 by the CLI's own account; only the tree says no.
        The fleet sizes are all past `n - 1 > fanout`, which is the width at
        which the seed could not have served everyone alone — below it a push
        is indistinguishable from a relay, and the module correctly says so.
        """
        for n_nodes in FLEET_SIZES:
            for fanout in FANOUTS:
                with self.subTest(n_nodes=n_nodes, fanout=fanout):
                    with self.assertRaisesRegex(bench.HarnessError, "relay"):
                        bench.verify_cascade_relay(
                            host_push_stream(n_nodes),
                            count=n_nodes,
                            fanout=fanout,
                        )

    def test_a_host_push_at_or_under_the_relay_width_is_not_treated_as_one(self) -> None:
        """The rejection has a boundary, and the boundary is the strategy's own.

        A cascade of 5 nodes at a fanout of 4 has 4 nodes pending, which the
        seed can serve in a single round, so a depth of 1 there is not evidence
        of a push — it is the only shape that shape has. Pinning both sides
        stops a fix that lowers the depth threshold from quietly rejecting
        fleets that genuinely cannot relay, which would fail a real small-fleet
        run rather than a test.
        """
        # One node past the width: 5 nodes leave 4 pending against a fanout of
        # 3, so the seed provably could not have served everyone alone and a
        # depth of 1 is a push.
        with self.assertRaises(bench.HarnessError) as caught:
            bench.verify_cascade_relay(host_push_stream(5), count=5, fanout=3)
        self.assertIn("not relayed", str(caught.exception))

        # At the width: 4 pending against a fanout of 4, so one round from the
        # seed is a valid log-4 cascade and must be accepted.
        accepted = bench.verify_cascade_relay(
            host_push_stream(5), count=5, fanout=4
        )
        self.assertEqual(5, accepted["nodes"])
        self.assertEqual(1, accepted["rounds"])
        self.assertEqual(1, accepted["relay_depth"])


    def test_the_node_count_must_match_the_fleet_at_every_shape(self) -> None:
        """A wrong fleet size is rejected whichever direction it errs in.

        A cascade over too few nodes left part of the fleet unproven; one over
        too many says the stream came from a run this harness did not launch.
        Both are reported as a node-count failure rather than being folded into
        the relay error, so the operator is told which of the two it was.
        """
        for n_nodes in FLEET_SIZES:
            for fanout in FANOUTS:
                for reported in (n_nodes - 1, n_nodes + 1):
                    if reported < 2:
                        continue
                    with self.subTest(n_nodes=n_nodes, fanout=fanout, reported=reported):
                        with self.assertRaisesRegex(bench.HarnessError, "nodes"):
                            bench.verify_cascade_relay(
                                relayed_stream(n_nodes, fanout),
                                count=reported,
                                fanout=fanout,
                            )

    def test_a_wrong_round_count_is_rejected_at_every_shape(self) -> None:
        """The round count is checked, not merely recorded.

        A stream that relayed but claims a different number of rounds describes
        a strategy that is not the one the CLI was asked for, so it is evidence
        about some other run. Nothing in the tree's shape can catch it, which is
        why the harness compares against the strategy's own round arithmetic.
        Both directions are swept: one round too few is a cascade that claims
        to have converged faster than the strategy allows, and one too many is
        one that does not match the run at all.
        """
        for n_nodes in FLEET_SIZES:
            for fanout in FANOUTS:
                for delta in (-1, 1):
                    with self.subTest(
                        n_nodes=n_nodes, fanout=fanout, delta=delta
                    ):
                        events = [
                            json.loads(line)
                            for line in relayed_stream(n_nodes, fanout).splitlines()
                        ]
                        for event in events:
                            if event["kind"] == "finished":
                                event["rounds"] += delta
                        stream = "".join(json.dumps(e) + "\n" for e in events)
                        with self.assertRaises(bench.HarnessError):
                            bench.verify_cascade_relay(
                                stream, count=n_nodes, fanout=fanout
                            )

    def test_an_empty_stream_is_not_a_passing_cascade_at_every_shape(self) -> None:
        """The empty-stream guard, swept over the fleets the harness launches.

        A checker that validated the tree and then trusted the absence of a
        complaint would accept a capture whose emitter produced nothing. The
        fleet size and fanout reach the check as arguments, so both are swept:
        a default that short-circuited for small fleets or for a wide fanout
        would let one of these through.
        """
        for n_nodes in FLEET_SIZES:
            for fanout in FANOUTS:
                with self.subTest(n_nodes=n_nodes, fanout=fanout):
                    with self.assertRaises(bench.HarnessError) as caught:
                        bench.verify_cascade_relay(
                            "", count=n_nodes, fanout=fanout
                        )
                    # The failure has to name the absent event. A checker that
                    # reported "no cascade" generically would pass here while
                    # still accepting a capture that emitted a `started` and
                    # nothing else, which is the shape a half-flushed emitter
                    # actually produces.
                    self.assertIn("started", str(caught.exception))

    def test_a_pre_seeded_cascade_still_proves_a_relay(self) -> None:
        """A run that resumed with several nodes already converged still relays.

        Seeded nodes are converged before any round starts, so they add no
        level and are not counted among the relayed ones. The pending count is
        `n_nodes - seeds`, and the shape the strategy's round count is defined
        over is one root's worth of those, so the expected rounds and depth are
        re-derived against the pending count rather than the full fleet.
        Sweeping the seed count is what pins that: a checker that failed to
        subtract the seeds would over-count the rounds and reject every one of
        these.
        """
        for n_nodes in FLEET_SIZES:
            for fanout in FANOUTS:
                for seeds in (2, 3):
                    pending = n_nodes - seeds
                    with self.subTest(n_nodes=n_nodes, fanout=fanout, seeds=seeds):
                        stream = multi_seed_relayed_stream(n_nodes, fanout, seeds)
                        summary = bench.verify_cascade_relay(
                            stream, count=n_nodes, fanout=fanout
                        )
                        self.assertEqual(n_nodes, summary["nodes"])
                        # The seeds were never served, so they are not relayed.
                        self.assertEqual(pending, summary["relayed_nodes"])
                        # The relaying sub-tree hangs off one seed and spans
                        # `pending + 1` nodes including that seed, so its rounds
                        # and depth are those of a fleet one node larger than
                        # the pending count.
                        self.assertEqual(
                            _rounds_for_fleet(pending + 1, fanout),
                            summary["rounds"],
                        )
                        self.assertEqual(
                            _relay_depth(pending + 1, fanout),
                            summary["relay_depth"],
                        )

    def test_a_pre_seeded_host_push_is_still_rejected(self) -> None:
        """Seeding more nodes does not launder a host push.

        A run that starts with several nodes converged and then has a seed serve
        everyone directly is the same defect as the single-seed push, just with
        a wider first round. It must be rejected for the same reason, and it is
        the combination most likely to slip through: a checker that compared
        the round count only, or that skipped the depth test whenever more than
        one node was already converged, would wave this through.
        """
        for n_nodes in FLEET_SIZES:
            for fanout in FANOUTS:
                for seeds in (2, 3):
                    # Below the relay width a push is the only possible shape,
                    # so there is nothing for the check to catch.
                    if n_nodes - seeds <= fanout:
                        continue
                    with self.subTest(n_nodes=n_nodes, fanout=fanout, seeds=seeds):
                        stream = multi_seed_relayed_stream(n_nodes, fanout, seeds)
                        # Rewrite every edge as a delivery from a seed, which
                        # is exactly what a host push looks like.
                        events = [
                            json.loads(line) for line in stream.splitlines()
                        ]
                        for event in events:
                            if event["kind"] == "edge_completed":
                                event["src"] = 0
                            elif event["kind"] == "finished":
                                event["rounds"] = 1
                        push = "".join(json.dumps(e) + "\n" for e in events)
                        with self.assertRaisesRegex(bench.HarnessError, "relay"):
                            bench.verify_cascade_relay(
                                push, count=n_nodes, fanout=fanout
                            )

    def test_a_node_served_twice_is_not_a_relay_tree(self) -> None:
        """A node that appears as a target twice is not a tree.

        The harness counts successful deliveries, so a duplicated target would
        otherwise let a run claim coverage it did not reach: `parent` holds one
        entry for the node while the edges report two, so the fleet looks
        converged and the round count still matches. The stream is a real
        relayed one with a single extra edge, so every other property holds and
        the duplicate is the only thing wrong with it.
        """
        events = [
            json.loads(line)
            for line in relayed_stream(8, 2).splitlines()
        ]
        # Re-deliver node 3, which already has a parent, before the run finishes.
        events.insert(
            len(events) - 1,
            {"kind": "edge_completed", "round": 0, "src": 0, "tgt": 3,
             "duration": 1},
        )
        stream = "".join(json.dumps(e) + "\n" for e in events)
        with self.assertRaises(bench.HarnessError) as caught:
            bench.verify_cascade_relay(stream, count=8, fanout=2)
        # The error has to name the defect. "The cascade failed" would send an
        # operator looking at the fleet instead of at the run's own bookkeeping.
        self.assertIn("served twice", str(caught.exception))

    def test_a_truncated_stream_is_not_a_converged_cascade(self) -> None:
        """No `finished` event means the run was cut off, not that it converged.

        Without this the parser would return whatever edges it had seen, and a
        fleet that lost its run mid-cascade would be reported as green.
        """
        stream = "".join(
            line + "\n"
            for line in relayed_stream(16, 2).splitlines()
            if json.loads(line)["kind"] != "finished"
        )
        with self.assertRaisesRegex(bench.HarnessError, "truncated"):
            bench.verify_cascade_relay(stream, count=16, fanout=2)

    def test_a_failing_edge_is_reported_as_an_unconverged_node(self) -> None:
        """A node nobody served is named, so the operator knows which one.

        `edge_failed` does not write a parent, so the node stays absent from
        the served set. The error has to name it: "the cascade failed" is not
        actionable, and a run that reported green over 15 of 16 nodes is
        exactly what the exit status cannot rule out.
        """
        events = [
            json.loads(line) for line in relayed_stream(16, 2).splitlines()
        ]
        events = [
            e for e in events
            if not (e["kind"] == "edge_completed" and e["tgt"] == 9)
        ]
        events.insert(
            -1,
            {"kind": "edge_failed", "round": 3, "src": 3, "tgt": 9,
             "error": "connection refused"},
        )
        stream = "".join(json.dumps(e) + "\n" for e in events)
        with self.assertRaises(bench.HarnessError) as caught:
            bench.verify_cascade_relay(stream, count=16, fanout=2)
        self.assertIn("9", str(caught.exception))


class CascadeStepTest(unittest.TestCase):
    """The step itself must hand the stream to the checker, not merely run it.

    `run_checked` is mocked because it is the SSH boundary, an external
    process. What is under test is the harness's handling of what came back:
    a cascade that exited 0 and left a host-push tree has to fail the run.
    """

    def _run(self, stream: str, *, count: int = 64, fanout: int = 2) -> dict:
        completed = SimpleNamespace(returncode=0, stdout=stream, stderr="")
        with mock.patch.object(bench, "run_checked", return_value=completed):
            return bench.run_cascade(
                ssh_key=Path("/dev/null"),
                store_path=Path("/nix/store/abc"),
                inventory_content="seed = 'root@127.0.0.1'\n",
                count=count,
                fanout=fanout,
            )

    def test_a_relayed_cascade_returns_its_shape(self) -> None:
        self.assertEqual(64, self._run(relayed_stream(64))["nodes"])

    def test_a_green_cascade_that_did_not_relay_fails_the_run(self) -> None:
        with self.assertRaisesRegex(bench.HarnessError, "relay"):
            self._run(host_push_stream(64))

    def test_the_step_returns_the_shape_at_every_fleet_shape(self) -> None:
        """The step must pass the fleet size and fanout through to the check.

        `run_cascade` threads `count` and `fanout` from the command line into
        `verify_cascade_relay`. If either were dropped, the check would fall
        back to a default and a run over a non-default fleet would be judged
        against a tree shape it never produced. Sweeping both is what catches
        that: a hard-coded default agrees with the call at 64 nodes and
        disagrees everywhere else.
        """
        for n_nodes in FLEET_SIZES:
            for fanout in FANOUTS:
                with self.subTest(n_nodes=n_nodes, fanout=fanout):
                    summary = self._run(
                        relayed_stream(n_nodes, fanout),
                        count=n_nodes,
                        fanout=fanout,
                    )
                    self.assertEqual(n_nodes, summary["nodes"])
                    self.assertEqual(
                        _rounds_for_fleet(n_nodes, fanout), summary["rounds"]
                    )
                    self.assertEqual(n_nodes - 1, summary["relayed_nodes"])

    def test_the_step_fails_on_a_host_push_at_every_fleet_shape(self) -> None:
        """The same property, one layer up, over the same shapes.

        A checker that is called directly but never reached from `run_cascade`
        — because the step verified the exit status instead of the stream —
        would pass every direct test above and record "ok" for a host push.
        This is the only test that exercises the wiring between the SSH result
        and the failure, so it sweeps the same shapes rather than one.
        """
        for n_nodes in FLEET_SIZES:
            for fanout in FANOUTS:
                with self.subTest(n_nodes=n_nodes, fanout=fanout):
                    with self.assertRaisesRegex(bench.HarnessError, "relay"):
                        self._run(
                            host_push_stream(n_nodes),
                            count=n_nodes,
                            fanout=fanout,
                        )

    def test_the_step_fails_on_a_cascade_over_the_wrong_fleet(self) -> None:
        """A wrong fleet size fails the run, not just the checker.

        The step owns `count`, so a checker that raised correctly but a step
        that swallowed the error would leave the run green over a fleet it
        never proved.
        """
        for n_nodes in FLEET_SIZES:
            for fanout in FANOUTS:
                with self.subTest(n_nodes=n_nodes, fanout=fanout):
                    with self.assertRaisesRegex(bench.HarnessError, "nodes"):
                        self._run(
                            relayed_stream(n_nodes, fanout),
                            count=n_nodes + 1,
                            fanout=fanout,
                        )

    def test_the_step_fails_on_an_empty_capture(self) -> None:
        """A run that produced no output at all must not be recorded as green.

        This is the regression the whole event-stream check exists to catch:
        the CLI writing a NullSink over a non-TTY stdout, the harness seeing an
        empty capture, and the run exiting 0.
        """
        for n_nodes in FLEET_SIZES:
            for fanout in FANOUTS:
                with self.subTest(n_nodes=n_nodes, fanout=fanout):
                    with self.assertRaises(bench.HarnessError):
                        self._run("", count=n_nodes, fanout=fanout)

    def test_the_step_reports_a_pre_seeded_relay_at_every_fleet_shape(self) -> None:
        """A resumed run's stream is accepted through the step, not just the checker.

        Several seeded nodes are what a partially-cached fleet reports, and the
        stream is still a genuine relay, so the step must return its shape
        rather than reject a run it did not launch. The `count` the step was
        given is still the full fleet, which is the part worth sweeping here:
        a step that had started passing the seeded count through instead would
        report a fleet smaller than the one it launched.
        """
        for n_nodes in FLEET_SIZES:
            for fanout in FANOUTS:
                for seeds in (2, 3):
                    pending = n_nodes - seeds
                    with self.subTest(n_nodes=n_nodes, fanout=fanout, seeds=seeds):
                        summary = self._run(
                            multi_seed_relayed_stream(n_nodes, fanout, seeds),
                            count=n_nodes,
                            fanout=fanout,
                        )
                        self.assertEqual(n_nodes, summary["nodes"])
                        self.assertEqual(pending, summary["relayed_nodes"])
                        self.assertEqual(
                            _rounds_for_fleet(pending + 1, fanout),
                            summary["rounds"],
                        )


if __name__ == "__main__":
    unittest.main()
