#!/usr/bin/env python3
"""Focused safety tests for the fanout VM benchmark launcher."""

from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any
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


class VmScratchCapacityTest(unittest.TestCase):
    """A RAM scratch too small for the fleet must be declined, not accepted.

    Each guest's -snapshot overlay is a copy-on-write file over the guest's
    whole writable volume, and everything nix copy writes during the measured
    deployment phase lands in it. Running out mid-deployment surfaces as an
    opaque nix copy I/O error inside deployment_s, which would silently turn
    the number into a measurement of tmpfs headroom.

    The budget is the write the measured window can actually make, not the
    volume size: 64 nodes x 1 GiB is 64 GiB, which is more than the 63 GiB
    /dev/shm on the Linux host, so budgeting the volume would decline the
    scratch there and give back the 0.33 s tmpfs placement won.
    """

    def _statvfs(self, free_mib: int) -> SimpleNamespace:
        return SimpleNamespace(f_bavail=free_mib * 1024 * 1024 // 4096, f_frsize=4096)

    def test_declines_when_the_mount_cannot_hold_the_fleet(self) -> None:
        with (
            mock.patch.object(bench.os, "statvfs", return_value=self._statvfs(64)),
            mock.patch.object(bench.Path, "is_dir", return_value=True),
        ):
            self.assertIsNone(bench.create_vm_scratch(1024 * 1024 * 1024))

    def test_uses_the_mount_when_there_is_room(self) -> None:
        with (
            mock.patch.object(bench.os, "statvfs", return_value=self._statvfs(63 * 1024)),
            mock.patch.object(bench.Path, "is_dir", return_value=True),
            mock.patch.object(bench.tempfile, "mkdtemp", return_value="/dev/shm/fv-scratch-x"),
        ):
            # A 2 GiB budget fits the Linux host's 63 GiB /dev/shm, which the
            # volume-sized budget would not have.
            self.assertEqual(
                Path("/dev/shm/fv-scratch-x"),
                bench.create_vm_scratch(2 * 1024 * 1024 * 1024),
            )

    def test_the_real_fleet_budget_fits_the_linux_host(self) -> None:
        # 64 nodes x (tiny payload x2 + 16 MiB) is about 1 GiB, so Linux keeps
        # its tmpfs overlays. If this ever exceeds the mount, the fleet silently
        # falls back to disk-backed overlays and loses the tmpfs win.
        needed = 64 * (4096 * 2 + bench.SCRATCH_PER_GUEST_MIB * 1024 * 1024)
        self.assertLess(needed, 63 * 1024 * 1024 * 1024)

    def test_no_scratch_root_means_no_scratch(self) -> None:
        with (
            mock.patch.object(bench.os, "statvfs", side_effect=AssertionError("statvfs called")),
            mock.patch.object(bench.Path, "is_dir", return_value=False),
        ):
            self.assertIsNone(bench.create_vm_scratch(1024))

    def test_store_path_bytes_measures_the_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            payload = Path(temporary_directory) / "payload"
            payload.mkdir()
            (payload / "a").write_bytes(b"x" * 1000)
            (payload / "b").write_bytes(b"x" * 24)
            self.assertEqual(1024, bench.store_path_bytes(payload))


class PayloadOutputVerificationTest(unittest.TestCase):
    """payload_executions_verified must rest on a real output comparison.

    The count is reported as 64, so a check that silently degrades to an exit
    status makes the strongest field in the result JSON meaningless. The
    non-default binary path matters: the old gate compared output only for
    bin/hello and skipped every other path without a word.
    """

    def setUp(self) -> None:
        # verify_store_path only reads these two fields.
        self.vm = SimpleNamespace(index=7, ssh_port=bench.SSH_PORT_BASE + 7)
        self.store_path = Path("/nix/store/zzz-payload")
        self.binary = PurePosixPath("bin/other-tool")

    def _verify(self, stdout: str, expected: str) -> None:
        completed = [
            subprocess.CompletedProcess([], 0, stdout=f"{self.store_path}\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout=stdout, stderr=""),
        ]
        with mock.patch.object(bench, "run_checked", side_effect=completed):
            bench.verify_store_path(
                self.vm,  # type: ignore[arg-type]
                Path("/dev/null"),
                self.store_path,
                self.binary,
                expected,
            )

    def test_matching_output_passes_for_any_binary_path(self) -> None:
        self._verify("payload ok\n", "payload ok\n")

    def test_mismatched_output_fails_for_a_non_default_binary_path(self) -> None:
        with self.assertRaisesRegex(bench.HarnessError, "unexpected output"):
            self._verify("something else\n", "payload ok\n")

    def test_trailing_newline_is_part_of_the_comparison(self) -> None:
        with self.assertRaisesRegex(bench.HarnessError, "unexpected output"):
            self._verify("payload ok", "payload ok\n")


class ExpectStdoutDecodingTest(unittest.TestCase):
    """--expect-stdout arrives as shell text; a literal backslash-n is a newline.

    The flake wrapper passes 'Hello, world!\\n' through a Nix indented string
    and bash single quotes, both of which leave the two bytes backslash and n.
    Only the decode turns that into the newline the payload actually prints.
    """

    def _parse(self, expect_stdout: str) -> Any:
        # parse_args resolves --store-path strictly and requires it to be a
        # direct child of /nix/store, so borrow a real one.
        store = Path("/nix/store")
        if not store.is_dir():
            self.skipTest("no /nix/store on this host")
        payload = next(
            (entry for entry in sorted(store.iterdir()) if entry.is_dir()),
            None,
        )
        if payload is None:
            self.skipTest("/nix/store is empty on this host")
        with tempfile.TemporaryDirectory() as temporary_directory:
            tmpdir = Path(temporary_directory)
            runner = tmpdir / "runner"
            runner.write_text("#!/bin/sh\n", encoding="utf-8")
            runner.chmod(0o700)
            key = tmpdir / "id"
            key.write_text("", encoding="utf-8")
            with mock.patch.dict(os.environ, {"TMPDIR": str(tmpdir)}):
                return bench.parse_args(
                    [
                        "--runner", str(runner),
                        "--restore-runner", str(runner),
                        "--guest-mem-mib", "512",
                        "--ssh-key", str(key),
                        "--store-path", str(payload),
                        "--expect-stdout", expect_stdout,
                        "--count", "2",
                    ]
                )

    def test_backslash_n_decodes_to_a_newline(self) -> None:
        self.assertEqual("Hello, world!\n", self._parse("Hello, world!\\n").expect_stdout)

    def test_a_doubled_backslash_is_not_collapsed(self) -> None:
        # Only a backslash-n pair is decoded. That is what makes the flake
        # wrapper's single backslash load-bearing: a wrapper that emitted
        # '\\n' would decode to a stray backslash and fail every payload check
        # with a one-byte diff.
        self.assertEqual("a\\\\b\n", self._parse("a\\\\b\\n").expect_stdout)

    def test_the_argument_is_required(self) -> None:
        with self.assertRaises(SystemExit), mock.patch.dict(os.environ, {"TMPDIR": "/tmp"}):
            with mock.patch("sys.stderr"):
                bench.parse_args(["--runner", "/bin/sh", "--count", "2"])


if __name__ == "__main__":
    unittest.main()
