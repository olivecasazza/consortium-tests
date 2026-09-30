"""Tests for the harness side of the cascade relay check.

`cascade_copy: "ok"` in the result JSON is the cascade CLI's exit status.
Nothing in it says the relay was used: a cascade that pushed the payload from
the host to each guest in turn exits 0 and would satisfy every other check the
harness makes. These tests pin the two pieces of wiring that turn that string
into evidence — the command must ask for the event stream, and the stream must
be turned into a verified topology or the run fails.
"""

from __future__ import annotations

import importlib.util
import json
import shlex
import tempfile
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


class CascadeRelayVerificationTest(unittest.TestCase):
    def test_a_relayed_cascade_is_reported_with_its_shape(self) -> None:
        summary = bench.verify_cascade_relay(relayed_stream(64), tree, count=64, fanout=2)

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
            bench.verify_cascade_relay(host_push_stream(64), tree, count=64, fanout=2)

    def test_the_node_count_must_match_the_fleet(self) -> None:
        # A cascade that converged over the wrong fleet is not evidence about
        # this one.
        with self.assertRaisesRegex(bench.HarnessError, "nodes"):
            bench.verify_cascade_relay(relayed_stream(8), tree, count=64, fanout=2)

    def test_an_empty_stream_is_not_a_passing_cascade(self) -> None:
        # Guards a silent regression: if the emitter stops emitting, an empty
        # capture must fail loudly, not quietly verify nothing.
        with self.assertRaises(bench.HarnessError):
            bench.verify_cascade_relay("", tree, count=64, fanout=2)


class CascadeEvidenceTest(unittest.TestCase):
    """A failed relay check must leave the stream behind.

    `run_checked` already truncates a command's output in the error it
    raises, so a rejected cascade used to leave only the tail - three events,
    the last one of which says everything and none of which says why. The whole
    point of the check is that a cascade that quietly degenerates is
    diagnosable, and a truncated tail is not.
    """

    def test_the_stream_is_written_even_when_the_command_itself_fails(self) -> None:
        """The case that hid this bug for a whole diagnosis.

        `cascade-copy` exits non-zero on a cascade that failed, and
        `run_checked` raises before returning — so writing the stream after it
        returns wrote nothing, and the one run that mattered left a
        three-event tail instead of the 126-event stream that showed every peer
        edge failing. The evidence has to be taken before the raise.
        """
        import subprocess as sp
        import tempfile

        stream = host_push_stream(8)
        ok = sp.CompletedProcess([], 0, stdout="", stderr="")
        failed = sp.CompletedProcess([], 1, stdout=stream, stderr="cascade-copy: boom")
        with tempfile.TemporaryDirectory() as tmp:
            evidence = Path(tmp)
            # The inventory upload succeeds; the cascade is what fails.
            with mock.patch.object(bench.subprocess, "run", side_effect=[ok, failed]):
                with self.assertRaises(bench.HarnessError):
                    bench.run_cascade(
                        ssh_key=Path("/dev/null"),
                        store_path=Path("/nix/store/abc"),
                        inventory_content="seed = 'root@127.0.0.1'\n",
                        module=tree,
                        count=8,
                        fanout=2,
                        evidence_dir=evidence,
                    )
            self.assertTrue((evidence / "cascade-events.jsonl").is_file(),
                            "a failing cascade must still leave its stream")

    def test_the_captured_stream_is_written_even_when_the_check_fails(self) -> None:
        import tempfile

        completed = SimpleNamespace(returncode=0, stdout=host_push_stream(64), stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            evidence = Path(tmp)
            with mock.patch.object(bench, "run_checked", return_value=completed):
                with self.assertRaises(bench.HarnessError):
                    bench.run_cascade(
                        ssh_key=Path("/dev/null"),
                        store_path=Path("/nix/store/abc"),
                        inventory_content="seed = 'root@127.0.0.1'\n",
                        module=tree,
                        count=64,
                        fanout=2,
                        evidence_dir=evidence,
                    )
            written = list(evidence.glob("*"))
            self.assertTrue(written, "no evidence written for a failed relay check")
            events = [
                json.loads(line)
                for f in written
                for line in f.read_text().splitlines()
                if line.strip()
            ]
            # The round count is the diagnosis: 63 rounds for 64 nodes is a
            # serial push, not a log-2 relay, and that is only visible in the
            # full stream.
            finished = [e for e in events if e["kind"] == "finished"]
            self.assertEqual(1, len(finished))
            # A star converges in one round. The real 64-node run took 63 and
            # looked exactly like this, one seed-served edge at a time.
            self.assertEqual(1, finished[0]["rounds"])
            self.assertEqual(
                {0},
                {e["src"] for e in events if e["kind"] == "edge_completed"},
                "every node was served by the seed, which is the defect",
            )


class CascadeTreeModuleLoadingTest(unittest.TestCase):
    """The verifier is a separate file, and it has to be found in the store.

    `flake.nix` copies `bench.py` into the store on its own, so the file next to
    it in the source tree is not next to it at runtime: a Nix store path for a
    copied file is `<hash>-<name>`, which no plain `import` will resolve. Found
    by running the real fleet, not by the unit tests, because those import
    `bench` from the source directory where the sibling does exist.
    """

    def test_it_loads_a_verifier_from_an_explicit_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            module_path = Path(tmp) / "cascade_tree.py"
            module_path.write_text(
                "MARKER = 'loaded from a store path'\n", encoding="utf-8"
            )
            loaded = bench.load_cascade_tree_module(module_path)
            self.assertEqual("loaded from a store path", loaded.MARKER)

    def test_it_loads_a_module_that_uses_dataclasses(self) -> None:
        # The real verifier is a dataclass module, and `@dataclass` resolves
        # `sys.modules[cls.__module__]`. Loading without registering the module
        # first dies inside the dataclass machinery with an error about
        # `NoneType.__dict__`, which says nothing about the cause. Found by
        # running the fleet; a marker-only module loads fine either way.
        with tempfile.TemporaryDirectory() as tmp:
            module_path = Path(tmp) / "cascade_tree.py"
            module_path.write_text(
                "from dataclasses import dataclass\n"
                "\n"
                "@dataclass(frozen=True)\n"
                "class Topology:\n"
                "    nodes: int\n",
                encoding="utf-8",
            )
            loaded = bench.load_cascade_tree_module(module_path)
            self.assertEqual(7, loaded.Topology(nodes=7).nodes)

    def test_a_missing_module_is_an_actionable_error(self) -> None:
        with self.assertRaisesRegex(bench.HarnessError, "cascade_tree"):
            bench.load_cascade_tree_module(Path("/nonexistent/cascade_tree.py"))


class CascadeStepTest(unittest.TestCase):
    """The step itself must hand the stream to the checker, not merely run it.

    `run_checked` is mocked because it is the SSH boundary, an external
    process. What is under test is the harness's handling of what came back:
    a cascade that exited 0 and left a host-push tree has to fail the run.
    """

    def _run(self, stream: str) -> dict:
        completed = SimpleNamespace(returncode=0, stdout=stream, stderr="")
        with mock.patch.object(bench, "run_checked", return_value=completed):
            return bench.run_cascade(
                ssh_key=Path("/dev/null"),
                store_path=Path("/nix/store/abc"),
                inventory_content="seed = 'root@127.0.0.1'\n",
                module=tree,
                count=64,
                fanout=2,
            )

    def test_a_relayed_cascade_returns_its_shape(self) -> None:
        self.assertEqual(64, self._run(relayed_stream(64))["nodes"])

    def test_a_green_cascade_that_did_not_relay_fails_the_run(self) -> None:
        with self.assertRaisesRegex(bench.HarnessError, "relay"):
            self._run(host_push_stream(64))


if __name__ == "__main__":
    unittest.main()
