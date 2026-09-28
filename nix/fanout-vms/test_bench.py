#!/usr/bin/env python3
"""Focused safety tests for the fanout VM benchmark launcher."""

from __future__ import annotations

import importlib.util
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BENCH_PATH = Path(__file__).with_name("bench.py")
SPEC = importlib.util.spec_from_file_location("fanout_vm_bench", BENCH_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load benchmark module from {BENCH_PATH}")
bench = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bench
SPEC.loader.exec_module(bench)


class PartialLaunchCleanupTest(unittest.TestCase):
    def test_partial_launch_keeps_started_child_available_for_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_dir = Path(temporary_directory) / "fv-test"
            run_dir.mkdir()
            runner = Path(temporary_directory) / "runner"
            runner.write_text(
                "#!/bin/sh\n"
                "trap 'exit 0' TERM INT\n"
                "while :; do sleep 1; done\n",
                encoding="utf-8",
            )
            runner.chmod(0o700)

            original_popen = subprocess.Popen
            attempts = 0

            def fail_second_start(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
                nonlocal attempts
                attempts += 1
                if attempts == 2:
                    raise OSError("injected second-runner startup failure")
                return original_popen(*args, **kwargs)

            started: list[bench.VmProcess] = []
            try:
                with mock.patch.object(
                    bench.subprocess,
                    "Popen",
                    side_effect=fail_second_start,
                ):
                    with self.assertRaisesRegex(
                        bench.HarnessError,
                        "VM 2: failed to start",
                    ):
                        bench.launch_vms(runner, run_dir, 2, started)

                self.assertEqual(1, len(started))
                started_process = started[0].process
            finally:
                cleanup_errors = bench.terminate_vms(started)

            self.assertEqual([], cleanup_errors)
            self.assertIsNotNone(started_process.poll())
            self.assertFalse(bench.process_group_alive(started[0].process_group))


class PortReservationTest(unittest.TestCase):
    def test_reuses_loopback_port_after_prior_http_connection_closes(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            listener.listen(1)
            with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
                with listener.accept()[0] as accepted:
                    accepted.shutdown(socket.SHUT_WR)
                    self.assertEqual(b"", client.recv(1))

        with bench.reserve_port(port, "HTTP") as reservation:
            self.assertEqual(("127.0.0.1", port), reservation.getsockname())


class SnapshotCacheKeyTest(unittest.TestCase):
    """A cache hit must never restore a state captured under other parameters.

    guest_ram_args maps ram_mib against the cached file, and the capture
    guest's readiness probe writes READY_PROBE_FILE into the golden overlay
    that every restore shares, so both belong in the cache key.
    """

    def setUp(self) -> None:
        self.cache = Path("/tmp/fanout64-cache-key-test")
        self.runner = Path("/nix/store/abc-microvm-run")

    def test_same_parameters_reuse_one_directory(self) -> None:
        first = bench.snapshot_dir(self.cache, self.runner, 512)
        self.assertEqual(first, bench.snapshot_dir(self.cache, self.runner, 512))
        self.assertEqual(self.cache, first.parent)

    def test_a_different_ram_size_does_not_hit_the_cached_snapshot(self) -> None:
        self.assertNotEqual(
            bench.snapshot_dir(self.cache, self.runner, 512),
            bench.snapshot_dir(self.cache, self.runner, 1024),
        )

    def test_a_different_runner_does_not_hit_the_cached_snapshot(self) -> None:
        self.assertNotEqual(
            bench.snapshot_dir(self.cache, self.runner, 512),
            bench.snapshot_dir(self.cache, Path("/nix/store/xyz-microvm-run"), 512),
        )

    def test_a_different_probe_file_does_not_hit_the_cached_snapshot(self) -> None:
        baseline = bench.snapshot_dir(self.cache, self.runner, 512)
        with mock.patch.object(bench, "READY_PROBE_FILE", "/root/somewhere-else"):
            self.assertNotEqual(baseline, bench.snapshot_dir(self.cache, self.runner, 512))


if __name__ == "__main__":
    unittest.main()
