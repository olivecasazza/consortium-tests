#!/usr/bin/env python3
"""Focused safety tests for the fanout VM benchmark launcher."""

from __future__ import annotations

import ast
import builtins
import contextlib
import dataclasses
import functools
import http.server
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, Sequence
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


class ConnectQmpBackoffTest(unittest.TestCase):
    """The QMP socket poll must start tight and back off, not sit at one step.

    Every one of 64 VMs retries a socket path that does not exist yet for the
    first stretch of its bring-up. A flat step both wastes latency when the
    socket appears between two attempts and keeps ~3200 wakeups a second
    running on a host that is already oversubscribed by 64 QEMU processes.
    """

    def test_poll_backs_off_from_the_initial_step_to_the_cap(self) -> None:
        sleeps: list[float] = []
        attempts = {"n": 0}

        def fake_connect(_self: socket.socket, address: str) -> None:
            attempts["n"] += 1
            if attempts["n"] <= 6:
                raise FileNotFoundError(address)

        vm = SimpleNamespace(
            index=1,
            qmp_socket=Path("/nonexistent/fanout.qmp"),
            process=SimpleNamespace(poll=lambda: None),
            workdir=Path("/nonexistent"),
        )
        with (
            mock.patch.object(socket.socket, "connect", fake_connect),
            mock.patch.object(bench.time, "sleep", sleeps.append),
        ):
            connection = bench.connect_qmp(vm, time.monotonic() + 30)  # type: ignore[arg-type]
        connection.close()

        self.assertEqual(6, len(sleeps))
        self.assertEqual(
            [
                bench.QMP_POLL_INITIAL_S,
                bench.QMP_POLL_INITIAL_S * 2,
                bench.QMP_POLL_INITIAL_S * 4,
                bench.QMP_POLL_INITIAL_S * 8,
                bench.QMP_POLL_INITIAL_S * 16,
                bench.QMP_POLL_MAX_S,
            ],
            sleeps,
        )
        # The old behaviour polled every 20 ms from the first attempt.
        self.assertLess(sleeps[0], bench.QMP_POLL_MAX_S)
        self.assertTrue(all(step <= bench.QMP_POLL_MAX_S for step in sleeps))


class SnapshotPruningTest(unittest.TestCase):
    """The cache grows ~565 MB per guest build; only unused entries may go.

    A hit refreshes its directory's mtime, and that is the sole signal pruning
    has, so the two must be tested together: without the touch, pruning would
    delete the snapshot the next run is about to use.
    """

    def setUp(self) -> None:
        self.cache = Path(tempfile.mkdtemp(prefix="fanout64-prune-test-"))
        self.addCleanup(shutil.rmtree, self.cache, ignore_errors=True)
        self.used = self.cache / "used"
        self.stale = self.cache / "stale"
        for entry in (self.used, self.stale):
            entry.mkdir()
            (entry / "ram").write_bytes(b"x")
        # used: touched now. stale: a week and a half ago.
        old = time.time() - 8 * 24 * 60 * 60
        os.utime(self.stale, (old, old))

    def test_a_recently_used_snapshot_survives_pruning(self) -> None:
        bench.touch_snapshot(self.used)
        bench.prune_stale_snapshots(self.cache)
        self.assertTrue(self.used.is_dir())

    def test_an_unused_snapshot_older_than_the_window_is_removed(self) -> None:
        bench.prune_stale_snapshots(self.cache)
        self.assertFalse(self.stale.exists())

    def test_a_cache_hit_also_prunes(self) -> None:
        # Pruning only after a capture never runs in steady state, where every
        # run is a hit. Caught by running it: a nine-day-old entry survived a
        # run that used the cache, and no test caught it, because the others
        # called prune_stale_snapshots directly instead of going through
        # ensure_snapshot.
        runner = Path("/nix/store/abc-microvm-run")
        probe = "/nix/store/xyz-probe/bin/fanout-probe " + bench.READY_PROBE_FILE
        directory = bench.snapshot_dir(self.cache, runner, 512, probe)
        shutil.copytree(self.used, directory)
        for name in ("ram", "state", bench.OVERLAY_IMAGE_NAME):
            (directory / name).write_bytes(b"x")
        old = time.time() - 9 * 24 * 60 * 60
        os.utime(self.stale, (old, old))
        os.utime(directory, (time.time(), time.time()))

        bench.ensure_snapshot(runner, Path("/dev/null"), self.cache, 512, probe)

        self.assertFalse(
            self.stale.exists(),
            "a cache hit must prune too, or the cache never shrinks in steady state",
        )
        self.assertTrue(directory.is_dir())

    def test_pruning_leaves_a_capture_in_flight_alone(self) -> None:
        # A concurrent launcher's staging directory is named capture-* and is
        # not a published key; removing it would break that run mid-capture.
        staging = self.cache / "capture-abc123"
        staging.mkdir()
        os.utime(staging, (0, 0))
        bench.prune_stale_snapshots(self.cache)
        self.assertTrue(staging.is_dir())

    def test_a_cache_hit_refreshes_the_mtime_pruning_reads(self) -> None:
        # The end-to-end contract, through ensure_snapshot rather than the
        # helper: an old-but-valid snapshot is used, and using it is the only
        # thing that saves it from the next prune. Calling touch_snapshot
        # directly would pass even if the hit path stopped calling it.
        runner = Path("/nix/store/abc-microvm-run")
        probe = "/nix/store/xyz-probe/bin/fanout-probe " + bench.READY_PROBE_FILE
        directory = bench.snapshot_dir(self.cache, runner, 512, probe)
        shutil.copytree(self.used, directory)
        for name in ("ram", "state", bench.OVERLAY_IMAGE_NAME):
            (directory / name).write_bytes(b"x")
        old = time.time() - 8 * 24 * 60 * 60
        os.utime(directory, (old, old))

        snapshot, captured = bench.ensure_snapshot(
            runner, Path("/dev/null"), self.cache, 512, probe
        )

        self.assertIsNone(captured, "expected a cache hit, not a capture")
        self.assertEqual(directory, snapshot.ram.parent)
        self.assertGreater(
            directory.stat().st_mtime, time.time() - 7 * 24 * 60 * 60,
            "a cache hit must refresh the mtime pruning reads",
        )
        bench.prune_stale_snapshots(self.cache)
        self.assertTrue(directory.is_dir())


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
        self.probe = "/nix/store/xyz-fanout-probe/bin/fanout-probe /nix/.rw-store/p"

    def test_same_parameters_reuse_one_directory(self) -> None:
        first = bench.snapshot_dir(self.cache, self.runner, 512, self.probe)
        self.assertEqual(first, bench.snapshot_dir(self.cache, self.runner, 512, self.probe))
        self.assertEqual(self.cache, first.parent)

    def test_a_different_ram_size_does_not_hit_the_cached_snapshot(self) -> None:
        self.assertNotEqual(
            bench.snapshot_dir(self.cache, self.runner, 512, self.probe),
            bench.snapshot_dir(self.cache, self.runner, 1024, self.probe),
        )

    def test_a_different_runner_does_not_hit_the_cached_snapshot(self) -> None:
        self.assertNotEqual(
            bench.snapshot_dir(self.cache, self.runner, 512, self.probe),
            bench.snapshot_dir(self.cache, Path("/nix/store/xyz-microvm-run"), 512, self.probe),
        )

    def test_a_different_probe_file_does_not_hit_the_cached_snapshot(self) -> None:
        baseline = bench.snapshot_dir(self.cache, self.runner, 512, self.probe)
        with mock.patch.object(bench, "READY_PROBE_FILE", "/root/somewhere-else"):
            self.assertNotEqual(
                baseline, bench.snapshot_dir(self.cache, self.runner, 512, self.probe)
            )


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



@functools.cache
def store_payload() -> Path:
    """A real direct child of /nix/store, which is what the parser demands.

    Cached because the answer cannot change inside one test process and the
    scan is otherwise the whole cost of building a command line.
    """
    store = Path("/nix/store")
    if not store.is_dir():
        raise unittest.SkipTest("no /nix/store on this host")
    payload = next((entry for entry in sorted(store.iterdir()) if entry.is_dir()), None)
    if payload is None:
        raise unittest.SkipTest("/nix/store is empty on this host")
    return payload


def parse_harness_args(
    expect_stdout: str = "x", *, count: int = 2, extra: Sequence[str] = ()
) -> Any:
    """Run the real argument parser over a valid minimal command line.

    parse_args resolves --store-path strictly and requires it to be a direct
    child of /nix/store, so borrow a real one rather than inventing a path.
    """
    payload = store_payload()
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
                    "--ready-probe-binary", "/nix/store/xyz-probe/bin/fanout-probe",
                    "--cascade-tree-module", "/nix/store/xyz-cascade-tree.py",
                    "--count", str(count),
                    *extra,
                ]
            )


class ExpectStdoutDecodingTest(unittest.TestCase):
    """--expect-stdout arrives as shell text; a literal backslash-n is a newline.

    The flake wrapper passes 'Hello, world!\\n' through a Nix indented string
    and bash single quotes, both of which leave the two bytes backslash and n.
    Only the decode turns that into the newline the payload actually prints.
    """

    def test_backslash_n_decodes_to_a_newline(self) -> None:
        self.assertEqual("Hello, world!\n", parse_harness_args("Hello, world!\\n").expect_stdout)

    def test_a_doubled_backslash_is_not_collapsed(self) -> None:
        # Only a backslash-n pair is decoded. That is what makes the flake
        # wrapper's single backslash load-bearing: a wrapper that emitted
        # '\\n' would decode to a stray backslash and fail every payload check
        # with a one-byte diff.
        self.assertEqual("a\\\\b\n", parse_harness_args("a\\\\b\\n").expect_stdout)

    def test_the_argument_is_required(self) -> None:
        with self.assertRaises(SystemExit), mock.patch.dict(os.environ, {"TMPDIR": "/tmp"}):
            with mock.patch("sys.stderr"):
                bench.parse_args(["--runner", "/bin/sh", "--count", "2"])


class ReadyProbeCommandTest(unittest.TestCase):
    """The caller supplies the binary; the harness owns the file and the check.

    The probe path is a guest store path only the build knows, so it has to
    come in as an argument. Everything that makes the probe meaningful stays
    here: the file lives on the block-backed volume, and stdout is compared to
    the nonce byte for byte.
    """

    def test_the_command_pairs_the_binary_with_the_harness_probe_file(self) -> None:
        args = parse_harness_args()
        self.assertEqual(
            "/nix/store/xyz-probe/bin/fanout-probe " + bench.READY_PROBE_FILE,
            args.ready_probe_command,
        )

    def test_a_different_probe_binary_does_not_hit_the_cached_snapshot(self) -> None:
        cache, runner = Path("/tmp/fanout64-probe-key"), Path("/nix/store/abc-run")
        self.assertNotEqual(
            bench.snapshot_dir(cache, runner, 512, "/nix/store/one/probe /p"),
            bench.snapshot_dir(cache, runner, 512, "/nix/store/two/probe /p"),
        )

    def test_moving_the_probe_file_off_tmpfs_invalidates_the_cached_snapshot(self) -> None:
        # The probe file was once /root on the initrd tmpfs, where sync(2) is a
        # no-op and the round trip proved nothing. If it ever moves back, the
        # state captured against the durable path must not be reused.
        cache, runner = Path("/tmp/fanout64-probe-path"), Path("/nix/store/abc-run")
        durable = bench.snapshot_dir(cache, runner, 512, "/nix/store/p/probe /p")
        with mock.patch.object(bench, "READY_PROBE_FILE", "/root/fanout-ready-probe"):
            self.assertNotEqual(
                durable, bench.snapshot_dir(cache, runner, 512, "/nix/store/p/probe /p")
            )


class FleetEntropyIndependenceTest(unittest.TestCase):
    """Two restored VMs must not draw from one captured RNG stream.

    The write-isolation canary cannot see this: an identical entropy stream is
    not a cross-VM write leak, so each node's own round trip still passes. If
    this regressed, a fleet test that generated keys per node would be drawing
    from shared state without any check failing.
    """

    @staticmethod
    def _hex(char: str) -> str:
        return char * bench.ENTROPY_HEX_CHARS

    def _run(self, draws: list[str], expect_error: bool) -> None:
        vms = [SimpleNamespace(index=i + 1, ssh_port=22200 + i) for i in range(2)]
        completed = [subprocess.CompletedProcess([], 0, stdout=d, stderr="")
                     for d in draws]
        with mock.patch.object(bench, "run_checked", side_effect=completed):
            if expect_error:
                with self.assertRaises(bench.HarnessError):
                    bench.assert_fleet_entropy_is_independent(
                        vms, Path("/dev/null"), "/nix/store/p/probe"
                    )
            else:
                bench.assert_fleet_entropy_is_independent(
                    vms, Path("/dev/null"), "/nix/store/p/probe"
                )

    def test_distinct_draws_pass(self) -> None:
        self._run([self._hex("a"), self._hex("b")], expect_error=False)

    def test_identical_draws_fail(self) -> None:
        # The whole point: a shared stream must be caught, not tolerated.
        self._run([self._hex("c"), self._hex("c")], expect_error=True)

    def test_a_non_hex_draw_fails(self) -> None:
        # The guest prints hex; anything else means the probe is not the one
        # this harness was pointed at.
        self._run([self._hex("a"), "z" * bench.ENTROPY_HEX_CHARS], expect_error=True)

    def test_a_short_draw_fails(self) -> None:
        self._run([self._hex("a"), "b" * 4], expect_error=True)

    def test_a_single_vm_is_refused(self) -> None:
        # Two nodes is the minimum that can distinguish a replayed stream.
        with self.assertRaisesRegex(bench.HarnessError, "at least two VMs"):
            bench.assert_fleet_entropy_is_independent(
                [SimpleNamespace(index=1, ssh_port=22201)], Path("/dev/null"), "/p"
            )

    def test_the_command_uses_the_supplied_binary_and_byte_count(self) -> None:
        seen = []
        vms = [SimpleNamespace(index=i + 1, ssh_port=22200 + i) for i in range(2)]

        def record(description, command, timeout):
            seen.append(list(command))
            return subprocess.CompletedProcess([], 0, stdout="a" * 64, stderr="")

        with mock.patch.object(bench, "run_checked", side_effect=record):
            with self.assertRaises(bench.HarnessError):  # identical stdout
                bench.assert_fleet_entropy_is_independent(
                    vms, Path("/dev/null"), "/nix/store/xyz-probe/bin/fanout-probe"
                )
        # The whole flag and count travel as the single remote-command argument.
        self.assertEqual(
            [
                f"/nix/store/xyz-probe/bin/fanout-probe --entropy {bench.ENTROPY_BYTES}",
                f"/nix/store/xyz-probe/bin/fanout-probe --entropy {bench.ENTROPY_BYTES}",
            ],
            [c[-1] for c in seen],
        )


class PerVmGuestAddressTest(unittest.TestCase):
    """Every launch answers to its own NIC address.

    Nothing in a restored guest is written per VM: 64 of them resume one
    captured snapshot, so the address cannot be burned into the image, and
    fw_cfg is read only at boot, which a restore never reaches. The address is
    a property of the launch instead, and the guest reads it back from the
    device, so a single shared address leaves 64 nodes unable to tell which
    one they are.
    """

    def test_two_vms_do_not_answer_to_the_same_address(self) -> None:
        self.assertEqual("02:00:00:00:00:01", bench.guest_mac(1))
        self.assertEqual("02:00:00:00:00:02", bench.guest_mac(2))
        self.assertNotEqual(bench.guest_mac(1), bench.guest_mac(2))

    def test_no_two_vms_in_the_fleet_share_an_address(self) -> None:
        addresses = [bench.guest_mac(index) for index in range(1, bench.MAX_VM_COUNT + 1)]
        self.assertEqual(bench.MAX_VM_COUNT, len(set(addresses)))

    def test_every_address_is_a_unicast_locally_administered_one(self) -> None:
        for index in range(1, bench.MAX_VM_COUNT + 1):
            octets = [int(part, 16) for part in bench.guest_mac(index).split(":")]
            with self.subTest(vm=index):
                self.assertEqual(6, len(octets))
                # 0x02 marks the address locally administered, and it keeps the
                # whole fleet inside the 02:... range the harness already used.
                self.assertEqual(0x02, octets[0])
                # A clear low bit keeps it unicast; a multicast address here
                # would put the NIC in a state no guest can use.
                self.assertEqual(0, octets[0] & 0b1)
                # The locally-administered bit is what tells the host network
                # the address is not a vendor one it may already be using.
                self.assertEqual(0b10, octets[0] & 0b10)

    def test_a_vm_keeps_its_address_across_launches(self) -> None:
        # The address is derived from the index, not drawn at random: a fresh
        # fleet run has to reach the same guest, and the capture VM has to keep
        # the address its snapshot was taken with.
        self.assertEqual(bench.guest_mac(7), bench.guest_mac(7))
        self.assertEqual(
            ["-global", "virtio-net-pci.mac=02:00:00:00:00:07"], bench.guest_mac_args(7)
        )

    def test_the_index_is_carried_in_the_low_octets(self) -> None:
        first = bench.guest_mac(1).split(":")
        last = bench.guest_mac(bench.MAX_VM_COUNT).split(":")
        self.assertEqual(first[:4], last[:4])
        self.assertEqual(format(bench.MAX_VM_COUNT, "02x"), last[5])

    def test_an_index_the_address_cannot_hold_is_refused(self) -> None:
        for index in (0, -1, bench.GUEST_MAC_INDEX_LIMIT + 1):
            with self.subTest(vm=index), self.assertRaises(bench.HarnessError):
                bench.guest_mac(index)

    def test_each_vm_is_launched_with_its_own_address(self) -> None:
        # The address is per launch, so it has to reach QEMU per launch: one
        # argv built outside the loop is one address for the whole fleet, and
        # nothing downstream can tell that apart from a hardcoded MAC.
        launched: list[list[str]] = []

        class RunningRunner:
            """A runner that stays up, so launch_vms keeps the VM it started."""

            def __init__(self) -> None:
                self.pid = 4242
                self.returncode = None

            def poll(self) -> int | None:
                return None

        def record(argv: list[str], **kwargs: object) -> RunningRunner:
            launched.append(list(argv))
            return RunningRunner()

        with tempfile.TemporaryDirectory() as temporary_directory:
            started: list[bench.VmProcess] = []
            with mock.patch.object(bench.subprocess, "Popen", side_effect=record):
                bench.launch_vms(
                    Path("/nix/store/xyz-microvm-run"), Path(temporary_directory), 2, started
                )

            for vm in started:
                vm.log.close()

        self.assertEqual(2, len(launched))
        self.assertEqual(["-global", "virtio-net-pci.mac=02:00:00:00:00:01"], launched[0][-2:])
        self.assertEqual(["-global", "virtio-net-pci.mac=02:00:00:00:00:02"], launched[1][-2:])


    def test_every_restored_vm_is_launched_with_its_own_address(self) -> None:
        # The branch the fleet actually takes. All 64 nodes resume one captured
        # snapshot, so the restore argv is where a per-launch address has to
        # arrive, and two VMs without a snapshot exercise only the cold-boot
        # branch the benchmark never measures. Uniqueness is checked across
        # the whole fleet, because the failure this guards against is 63 of 64
        # launches quietly agreeing with their neighbour.
        launched: list[list[str]] = []

        class RunningRunner:
            """A runner that stays up, so launch_vms keeps the VM it started."""

            def __init__(self) -> None:
                self.pid = 4242
                self.returncode = None

            def poll(self) -> int | None:
                return None

        def record(argv: list[str], **kwargs: object) -> RunningRunner:
            launched.append(list(argv))
            return RunningRunner()

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            run_dir = root / "run"
            run_dir.mkdir()
            snapshot = bench.Snapshot(
                ram=root / "ram.img",
                ram_mib=512,
                state=root / "state",
                overlay=root / "overlay.img",
            )
            started: list[bench.VmProcess] = []
            with mock.patch.object(bench.subprocess, "Popen", side_effect=record):
                bench.launch_vms(
                    Path("/nix/store/xyz-microvm-run"),
                    run_dir,
                    bench.MAX_VM_COUNT,
                    started,
                    snapshot,
                )

            for vm in started:
                vm.log.close()

        self.assertEqual(bench.MAX_VM_COUNT, len(launched))
        carried: list[str] = []
        for index, argv in enumerate(launched, start=1):
            with self.subTest(vm=index):
                # Read the address back out of the argv this launch really
                # carried, so uniqueness is a statement about the launches and
                # not a second computation of what they should have said.
                addresses = [
                    argument
                    for argument in argv
                    if argument.startswith("virtio-net-pci.mac=")
                ]
                self.assertEqual(
                    [f"virtio-net-pci.mac={bench.guest_mac(index)}"], addresses
                )
                # The restore itself is still on the same argv: an address
                # that arrived by displacing the incoming state would restore
                # nothing.
                self.assertIn("-incoming", argv)
                self.assertIn("defer", argv)
                # Restores map the captured RAM private, not shared.
                self.assertTrue(
                    any("share=off" in argument for argument in argv),
                    f"restore argv does not map the captured RAM private: {argv}",
                )
            carried.extend(addresses)
        self.assertEqual(bench.MAX_VM_COUNT, len(set(carried)))


GUEST_NIX_PATH = Path(__file__).with_name("guest.nix")


def nix_block(text: str, opening: str, closing: str) -> str:
    """The body of one Nix attribute, from its opening line to its closing one."""
    match = re.search(re.escape(opening) + r"(.*?)" + closing, text, re.DOTALL)
    if match is None:
        raise AssertionError(f"no block starting {opening!r}")
    return match.group(1)

# The [Match] keys this guest's link unit is allowed to name, all of them
# keys systemd.link(5) accepts in [Match]. The names that page
# cross-references for their semantics (ConditionHost=,
# ConditionVirtualization=, ConditionKernelCommandLine=,
# ConditionKernelVersion=, ConditionVersion= and ConditionArchitecture=)
# are documented in systemd.unit(5) and are NOT keys of [Match] here, which
# is what Host=, Virtualization=, KernelCommandLine=, KernelVersion=,
# Version= and Architecture= refer to: a matchConfig naming one of those
# renders a line udev does not read, so a set carrying them described as the
# documented keys would licence exactly the inert line this guards against.
SYSTEMD_LINK_MATCH_KEYS = {
    "MACAddress",
    "PermanentMACAddress",
    "Path",
    "Driver",
    "Type",
    "Kind",
    "Property",
    "OriginalName",
    "Host",
    "Virtualization",
    "KernelCommandLine",
    "KernelVersion",
    "Version",
    "Credential",
    "Architecture",
}


class GuestImageAddressContractTest(unittest.TestCase):
    """The guest must not pin the address the harness sets on every launch.

    An explicit mac= on the interface beats the -global bench.py passes, so a
    hardcoded address in the image puts all 64 restored guests back on one. And
    a .network that matches a MAC stops matching the moment a VM is given its
    own, which costs that guest its DHCP address and its readiness: 64 VMs
    that never come up look exactly like a slow fleet.
    """

    def setUp(self) -> None:
        self.guest_nix = GUEST_NIX_PATH.read_text(encoding="utf-8")
        self.interfaces = nix_block(self.guest_nix, "    interfaces = [", r"\n    \];")
        self.network = nix_block(
            self.guest_nix, '  systemd.network.networks."10-user" = {', r"\n  \};"
        )
        self.link = nix_block(
            self.guest_nix, '  systemd.network.links."10-fanout" = {', r"\n  \};"
        )

    def test_the_interface_leaves_its_address_to_the_harness(self) -> None:
        self.assertNotRegex(self.interfaces, r"(?m)^\s*mac\s*=")

    def test_the_network_no_longer_matches_on_an_address(self) -> None:
        self.assertNotIn("MACAddress", self.network)

    def test_the_network_matches_the_name_and_still_asks_for_dhcp(self) -> None:
        self.assertIn('matchConfig.Name = "net0";', self.network)
        self.assertIn('DHCP = "ipv4";', self.network)

    def test_the_name_is_fixed_by_a_link_unit_that_ignores_the_address(self) -> None:
        # The kernel's own name for the NIC depends on PCI slot order, so it is
        # pinned here instead, and the match must stay off the address, which
        # differs per launch. Type= is what matches this NIC, from the uevent
        # the kernel emits at registration, before any driver is bound.
        self.assertIn('linkConfig.Name = "net0";', self.link)
        self.assertIn('Type = "ether";', self.link)
        self.assertNotIn("MACAddress", self.link)
        self.assertNotIn("PermanentMACAddress", self.link)

    def test_the_link_match_uses_only_keys_systemd_link_defines(self) -> None:
        # NixOS writes matchConfig into [Match] without checking it, so a key
        # that is not in systemd.link(5) renders a line udev does not read:
        # the unit still loads and the constraint it names has no effect at
        # all. Pinning such a key in a test would pin an inert line, which is
        # why the set below is the expectation rather than one chosen key.
        # It names the Condition* spellings nowhere, because systemd.link(5)
        # documents them under systemd.unit(5) as the conditions the plain
        # keys defer to; they are not [Match] keys of their own, and admitting
        # them would let this guard pass a line udev ignores.
        match = nix_block(self.link, "    matchConfig = {", "\n    };")
        keys = set(re.findall(r"(?m)^\s*([A-Za-z][A-Za-z0-9]*)\s*=", match))
        self.assertTrue(keys)
        self.assertLessEqual(keys, SYSTEMD_LINK_MATCH_KEYS)

    def test_the_bus_constraint_is_a_udev_property_the_kernel_emits(self) -> None:
        # There is no Bus= key to express this with, so the bus is named the
        # way the match can read it: as the udev property the kernel emits for
        # the device when it is registered.
        self.assertIn('Property = "ID_BUS=pci";', self.link)

    def test_the_harness_reads_the_name_the_guest_pins(self) -> None:
        # bench.py asks each guest for the address on one named interface. If
        # that is not the name the guest pinned, every node reports the wrong
        # device and the fleet identity check reads a different interface than
        # the one the network is configured on.
        pinned = re.search(r'linkConfig\.Name = "([^"]+)";', self.link)
        self.assertIsNotNone(pinned)
        self.assertEqual(pinned.group(1), bench.GUEST_NIC_NAME)

    def test_the_network_and_the_link_unit_agree_on_the_name(self) -> None:
        # They are two files. If they name different interfaces the guest keeps
        # the name no .network matches, and DHCP never runs on it.
        pinned = re.search(r'linkConfig\.Name = "([^"]+)";', self.link)
        matched = re.search(r'matchConfig\.Name = "([^"]+)";', self.network)
        self.assertIsNotNone(pinned)
        self.assertIsNotNone(matched)
        self.assertEqual(pinned.group(1), matched.group(1))


PROBE_PATH = Path(__file__).with_name("probe.c")
PROBE_BINARY = "/nix/store/xyz-probe/bin/fanout-probe"


def closed_socket() -> socket.socket:
    """A socket stand-in for the code paths that only connect and close."""
    return mock.MagicMock(spec=socket.socket)


class FakeQmp:
    """A QMP connection that records commands and answers the ones used here.

    mac is what QEMU holds for the NIC, so a caller exercising bring_up_vm
    has to pass the address that launch assigned (guest_mac of the VM's
    index): the run refuses a device holding anything else, and an unset mac
    fails it rather than passing a test on a placeholder.
    """

    def __init__(
        self,
        *,
        peripherals: list[dict[str, str]] | None = None,
        mac: str | None = None,
    ) -> None:
        self.peripherals = (
            [
                {"name": "i440FX", "type": "I440FX-pcihost"},
                {"name": "net0", "type": "virtio-net-pci"},
            ]
            if peripherals is None
            else peripherals
        )
        self.mac = mac
        self.commands: list[str] = []
        # The monitor command lines, which is where the host forwards a
        # bring-up installs are named.
        self.host_monitor: list[str] = []

    def negotiate(self, deadline: float) -> None:
        self.commands.append("qmp_capabilities")

    def execute(
        self, command: str, arguments: dict[str, Any] | None, deadline: float
    ) -> Any:
        self.commands.append(command)
        if command == "human-monitor-command":
            assert arguments is not None
            self.host_monitor.append(arguments["command-line"])
            return ""
        if command == "query-migrate":
            return {"status": "completed"}
        if command == "qom-list":
            return self.peripherals
        if command == "qom-get":
            assert arguments is not None
            self.queried = (arguments["path"], arguments["property"])
            return self.mac
        return None


def vm_process(index: int, **overrides: Any) -> SimpleNamespace:
    """A stand-in for one VM, carrying the dataclass's own defaults.

    The optional fields are read from VmProcess rather than listed here, so a
    field the harness adds reaches every test that stands in for a VM instead
    of failing in all of them with a missing attribute.
    """
    fields: dict[str, Any] = {
        field.name: field.default
        for field in dataclasses.fields(bench.VmProcess)
        if field.default is not dataclasses.MISSING
    }
    fields.update(
        {
            "index": index,
            "ssh_port": bench.SSH_PORT_BASE + index,
            "http_port": bench.HTTP_PORT_BASE + index,
            "device_address": None,
            "guest_address": None,
        }
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def reported(addresses: dict[int, str]) -> Any:
    """A run_checked stand-in returning each VM its scripted answer."""
    seen: list[list[str]] = []

    def run(description: str, command: Any, **kwargs: Any) -> SimpleNamespace:
        port = next(
            int(argument)
            for argument in command
            if argument.lstrip("-").isdigit() and 22200 <= int(argument) < 30000
        )
        seen.append(list(command))
        return SimpleNamespace(stdout=addresses[port - bench.SSH_PORT_BASE])

    run.seen = seen
    return run


class NicAddressOverQmpTest(unittest.TestCase):
    """The host's own view of the address, read from each VM's QMP socket."""

    def test_the_nic_is_found_by_device_type_not_by_an_assumed_id(self) -> None:
        # The runner does not name the NIC, so a harness that looked for a
        # known id would fail on a machine that happens to number it
        # differently. The type is what distinguishes it from every other
        # peripheral.
        qmp = FakeQmp()
        path = bench.find_nic_qom_path(qmp, 1, 0.0)
        self.assertEqual(f"{bench.QMP_PERIPHERAL_PATH}/net0", path)

    def test_a_machine_without_exactly_one_nic_is_refused(self) -> None:
        # Two NICs have no single address to report, and picking one would
        # report an address the run never assigned to anything.
        for peripherals in (
            [],
            [{"name": "i440FX", "type": "I440FX-pcihost"}],
            [
                {"name": "net0", "type": "virtio-net-pci"},
                {"name": "net1", "type": "virtio-net-pci"},
            ],
        ):
            with self.subTest(peripherals=peripherals):
                qmp = FakeQmp(peripherals=peripherals)
                with self.assertRaisesRegex(bench.HarnessError, "expected one virtio-net NIC"):
                    bench.find_nic_qom_path(qmp, 1, 0.0)

    def _bring_up(self, index: int, mac: str | None) -> tuple[Any, Any]:
        """Run bring_up_vm for one VM against a QMP connection holding mac."""
        qmp = FakeQmp(mac=mac)
        vm = vm_process(index)
        with (
            mock.patch.object(bench, "connect_qmp", return_value=closed_socket()),
            mock.patch.object(bench, "QmpConnection", return_value=qmp),
        ):
            bench.bring_up_vm(
                vm,
                bench.PortReservation(closed_socket(), closed_socket()),
                10.0,
                bench.Snapshot(Path("/tmp/ram"), 512, Path("/tmp/state"), Path("/tmp/ov")),
            )
        return qmp, vm

    def test_the_address_is_read_after_cont_and_recorded(self) -> None:
        # The read has to follow cont: before it, the restore has not landed
        # and QEMU is describing the paused capture, not this VM. The address
        # the device holds is the one this launch assigned VM 7, which is what
        # the run requires of the device and therefore the only value a bring-up
        # can pass with: a reading taken but never compared would let a device
        # no launch ever addressed through, and 64 restored nodes answering to
        # one address.
        qmp, vm = self._bring_up(7, bench.guest_mac(7))
        self.assertLess(
            qmp.commands.index("cont"),
            qmp.commands.index("qom-get"),
        )
        self.assertEqual(bench.guest_mac(7), vm.device_address)
        self.assertEqual(
            (f"{bench.QMP_PERIPHERAL_PATH}/net0", bench.QMP_NIC_MAC_PROPERTY),
            qmp.queried,
        )

    def test_a_device_holding_another_address_fails_the_bring_up(self) -> None:
        # The negative control for the host side. A device left on the
        # capture-time address is what a restore with no per-launch injection
        # looks like from QEMU, and it is indistinguishable fleet-wide from a
        # device that was given one and a guest that misread it, so it has to
        # be refused here rather than discovered 64 times over by the guests.
        with self.assertRaises(bench.HarnessError) as caught:
            self._bring_up(7, bench.guest_mac(1))
        message = str(caught.exception)
        self.assertIn("VM 7", message)
        self.assertIn(bench.guest_mac(1), message)
        self.assertIn(bench.guest_mac(7), message)

    def test_the_address_compared_is_this_launch_s_not_a_fixed_one(self) -> None:
        # The comparison is against the address this launch assigned, so it
        # holds for every VM rather than for one: VM 1's address is a pass
        # only on VM 1.
        for index in (1, 2, 64):
            with self.subTest(index=index):
                _, vm = self._bring_up(index, bench.guest_mac(index))
                self.assertEqual(bench.guest_mac(index), vm.device_address)
        for index in (2, 64):
            with self.subTest(index=index):
                with self.assertRaises(bench.HarnessError):
                    self._bring_up(index, bench.guest_mac(1))


class GuestReportsItsOwnAddressTest(unittest.TestCase):
    """Each node must name the address its own launch assigned it."""

    def test_every_node_reporting_its_own_address_passes(self) -> None:
        vms = [vm_process(index) for index in (1, 2, 3)]
        run = reported({1: bench.guest_mac(1), 2: bench.guest_mac(2), 3: bench.guest_mac(3)})
        with mock.patch.object(bench, "run_checked", side_effect=run):
            bench.assert_fleet_node_identity_is_per_launch(
                vms, Path("/tmp/key"), PROBE_BINARY
            )
        self.assertEqual(
            [bench.guest_mac(index) for index in (1, 2, 3)],
            [vm.guest_address for vm in vms],
        )

    def test_a_node_answering_for_another_node_fails(self) -> None:
        # The negative control. A shared address is what a restore with no
        # per-launch MAC looks like from the guest side, and it must not pass:
        # every node would be reporting the same identity.
        vms = [vm_process(index) for index in (1, 2, 3)]
        run = reported({1: bench.guest_mac(1), 2: bench.guest_mac(1), 3: bench.guest_mac(3)})
        with (
            mock.patch.object(bench, "run_checked", side_effect=run),
            self.assertRaises(bench.HarnessError),
        ):
            bench.assert_fleet_node_identity_is_per_launch(
                vms, Path("/tmp/key"), PROBE_BINARY
            )

    def test_the_address_the_capture_took_is_not_accepted_after_a_restore(self) -> None:
        # The capture was taken on VM 1, so every restored guest reading the
        # device state it resumed would report VM 1's address. That is the
        # specific way the per-launch identity can fail to take effect, and
        # it is exactly what makes the restored nodes indistinguishable.
        vms = [vm_process(index) for index in (1, 2)]
        run = reported({1: bench.guest_mac(1), 2: bench.guest_mac(1)})
        with (
            mock.patch.object(bench, "run_checked", side_effect=run),
            self.assertRaisesRegex(bench.HarnessError, "cannot be told apart by address"),
        ):
            bench.assert_fleet_node_identity_is_per_launch(
                vms, Path("/tmp/key"), PROBE_BINARY
            )

    def test_the_failure_names_both_addresses_and_what_the_device_holds(self) -> None:
        # Attributing the mismatch is the point of reading the device host-side
        # as well: a guest on the capture-time address while QEMU holds this
        # VM's own is a different problem from a device never given one.
        vms = [vm_process(2, device_address="02:00:00:00:00:02")]
        run = reported({2: bench.guest_mac(1)})
        with (
            mock.patch.object(bench, "run_checked", side_effect=run),
            self.assertRaises(bench.HarnessError) as caught,
        ):
            bench.assert_fleet_node_identity_is_per_launch(
                vms, Path("/tmp/key"), PROBE_BINARY
            )
        message = str(caught.exception)
        self.assertIn(bench.guest_mac(1), message)
        self.assertIn(bench.guest_mac(2), message)
        self.assertIn("02:00:00:00:00:02", message)

    def test_an_answer_that_is_not_an_address_fails(self) -> None:
        # A guest that answered with anything else did not perform the read,
        # and passing that on would compare the wrong thing.
        for answer in ("", "net0", "02:00:00:00:00", "02-00-00-00-00-01\n", "not a mac"):
            with self.subTest(answer=answer):
                vms = [vm_process(1)]
                run = reported({1: answer})
                with (
                    mock.patch.object(bench, "run_checked", side_effect=run),
                    self.assertRaisesRegex(bench.HarnessError, "is not a MAC address"),
                ):
                    bench.assert_fleet_node_identity_is_per_launch(
                        vms, Path("/tmp/key"), PROBE_BINARY
                    )

    def test_the_guest_is_asked_for_the_pinned_interface_at_request_time(self) -> None:
        # One SSH round trip per node, naming the interface the guest pinned.
        # Nothing here can be satisfied by a value the image was built with.
        vms = [vm_process(5)]
        run = reported({5: bench.guest_mac(5)})
        with mock.patch.object(bench, "run_checked", side_effect=run):
            bench.assert_fleet_node_identity_is_per_launch(
                vms, Path("/tmp/key"), PROBE_BINARY
            )
        remote = run.seen[0][-1]
        self.assertEqual(f"{PROBE_BINARY} --address {bench.GUEST_NIC_NAME}", remote)


class FailedRunPublishesItsMeasurementsTest(unittest.TestCase):
    """A run that fails still publishes what it measured on the way out.

    The guest-side divergence is the failure this exists for: it is expected
    rather than understood, so the run that observes it is the only source of
    the measurement, and a report that arrives without it has cost a whole
    fleet to produce nothing.
    """

    def test_a_guest_naming_another_address_is_published_with_its_device_s(self) -> None:
        vms = [
            vm_process(1, guest_address=bench.guest_mac(1), device_address=bench.guest_mac(1)),
            vm_process(2, guest_address=bench.guest_mac(1), device_address=bench.guest_mac(2)),
        ]
        report = bench.failed_result(vms, bench.HarnessError("per-launch node identity failed"), [])
        identities = report["node_identities"]
        self.assertEqual(bench.guest_mac(1), identities["guest_addresses"]["2"])
        self.assertEqual(bench.guest_mac(2), identities["device_addresses"]["2"])
        self.assertEqual("agrees_with_device", identities["node_statuses"]["1"])
        self.assertEqual("diverges_from_device", identities["node_statuses"]["2"])
        self.assertEqual(1, report["statuses"]["node_identity_verified"])
        self.assertEqual("failed", report["status"])

    def test_the_failing_run_prints_that_report_and_exits_nonzero(self) -> None:
        vms = [
            vm_process(1, guest_address=bench.guest_mac(1), device_address=bench.guest_mac(1)),
            vm_process(2, guest_address=bench.guest_mac(1), device_address=bench.guest_mac(2)),
        ]
        failure = bench.BenchmarkFailed(
            "VM 2: the guest reports address '02:00:00:00:00:01'",
            bench.failed_result(vms, bench.HarnessError("per-launch node identity failed"), []),
        )
        out, err = io.StringIO(), io.StringIO()
        with (
            # parse_args demands a runner, a key and a store path under
            # /nix/store, none of which this measurement is about.
            mock.patch.object(
                bench,
                "parse_args",
                return_value=SimpleNamespace(tmpdir=Path("/tmp"), negative_control_vm=None),
            ),
            mock.patch.object(bench, "create_run_dir", return_value=Path("/tmp/run")),
            mock.patch.object(bench, "execute_benchmark", side_effect=failure),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            self.assertEqual(1, bench.main([]))
        published = json.loads(out.getvalue())
        self.assertEqual(failure.report, published)
        self.assertIn("02:00:00:00:00:02", json.dumps(published))
        self.assertIn("failed", err.getvalue())


SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def bound_names(node: ast.AST, descend: bool) -> set[str]:
    """The names a scope binds, descending into nested scopes only if asked.

    A nested function's own name always counts, because the enclosing scope
    binds it by having it; its arguments and locals do not, and counting
    those would hide exactly the mistake this is looking for.
    """
    names: set[str] = set()
    stack = [node]
    while stack:
        for child in ast.iter_child_nodes(stack.pop()):
            if isinstance(child, ast.Name):
                if isinstance(child.ctx, (ast.Store, ast.Del)):
                    names.add(child.id)
            elif isinstance(child, SCOPES):
                names.add(child.name)
                if descend:
                    stack.append(child)
            elif isinstance(child, ast.Lambda):
                if descend:
                    stack.append(child)
            elif isinstance(child, (ast.Import, ast.ImportFrom)):
                names.update((alias.asname or alias.name).split(".")[0] for alias in child.names)
            elif isinstance(child, ast.ExceptHandler) and child.name:
                names.add(child.name)
            elif isinstance(child, (ast.Global, ast.Nonlocal)):
                names.update(child.names)
            elif isinstance(child, ast.arg):
                names.add(child.arg)
            else:
                stack.append(child)
    return names


class NoNameIsLoadedThatNothingBindsTest(unittest.TestCase):
    """Every name a function reads is bound by it, a scope it closes over,
    or the module.

    execute_benchmark built one entry of its result from a name it never
    bound, so the dict the run publishes could not be built at all: the
    failure was a NameError at the last statement of a run that had passed
    everything else, and no test reached it because the whole result is
    assembled at the end of a function that needs a fleet to run. The check
    is static so the whole class of that mistake is caught without one.
    """

    def test_no_function_reads_an_unbound_name(self) -> None:
        tree = ast.parse(BENCH_PATH.read_text(encoding="utf-8"))
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        module = bound_names(ast.Module(body=tree.body, type_ignores=[]), descend=False)
        module |= {"__name__", "__file__", "__doc__"} | set(dir(builtins))
        for function in [n for n in ast.walk(tree) if isinstance(n, SCOPES)]:
            scope = bound_names(function, descend=True)
            enclosing = parents.get(function)
            while isinstance(enclosing, SCOPES):
                scope |= bound_names(enclosing, descend=False)
                enclosing = parents[enclosing]
            read = {
                node.id
                for node in ast.walk(function)
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
            }
            with self.subTest(function=function.name):
                self.assertEqual([], sorted(read - scope - module))


@unittest.skipUnless(shutil.which("cc"), "no C compiler to build probe.c")
class GuestAddressReadTest(unittest.TestCase):
    """The guest reads its address out of the device, when it is asked.

    probe.c is the binary the harness already runs in the guest, so this
    builds that same source against a fixture sysfs tree. Running it is the
    only way to tell a read that happens per request from a constant the build
    already knew: the two return the same bytes for the first request and
    differ on the second, which is what test_the_second_request_sees_the_new
    _address checks.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory()
        root = Path(cls._temporary.name)
        cls.sysfs = root / "class-net"
        cls.sysfs.mkdir()
        cls.binary = root / "probe"
        compiled = subprocess.run(
            [
                shutil.which("cc") or "cc",
                "-std=gnu11",
                "-O1",
                "-Wall",
                "-Wextra",
                f'-DFANOUT_SYSFS_NET="{cls.sysfs}"',
                "-o",
                str(cls.binary),
                str(PROBE_PATH),
            ],
            capture_output=True,
            text=True,
        )
        if compiled.returncode != 0:
            raise AssertionError(f"probe.c did not compile:\n{compiled.stderr}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    def setUp(self) -> None:
        self.interface = self.sysfs / "net0"
        # Recreated per test: the binary's sysfs root is fixed at compile time,
        # so the tree has to exist again for each one. Tests that write an
        # address always write the one they ask about.
        self.interface.mkdir(parents=True, exist_ok=True)

    def _write(self, address: str) -> None:
        (self.interface / "address").write_text(f"{address}\n", encoding="ascii")

    def _ask(self, interface: str = "net0") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.binary), "--address", interface],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_the_guest_reports_the_address_of_the_interface_it_is_asked_about(self) -> None:
        self._write("02:00:00:00:00:07")
        result = self._ask()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("02:00:00:00:00:07\n", result.stdout)

    def test_each_interface_is_answered_with_its_own_address(self) -> None:
        # Naming the device has to select the device, or every node would
        # report whatever the first interface in the tree happened to be.
        loopback = self.sysfs / "lo"
        loopback.mkdir()
        self._write("02:00:00:00:00:07")
        (loopback / "address").write_text("00:00:00:00:00:00\n", encoding="ascii")
        self.assertEqual("00:00:00:00:00:00\n", self._ask("lo").stdout)
        self.assertEqual("02:00:00:00:00:07\n", self._ask("net0").stdout)

    def test_the_second_request_sees_the_new_address(self) -> None:
        # The laziness proof. One binary, one invocation per request, and the
        # file changing in between: a value the build or the boot knew would
        # answer the second request with the first request's bytes, and a
        # store file shared by all 64 restored guests would do the same.
        self._write("02:00:00:00:00:07")
        first = self._ask()
        self._write("02:00:00:00:00:3f")
        second = self._ask()
        self.assertEqual("02:00:00:00:00:07\n", first.stdout)
        self.assertEqual("02:00:00:00:00:3f\n", second.stdout)
        self.assertNotEqual(first.stdout, second.stdout)

    def test_an_interface_that_is_not_there_fails_instead_of_guessing(self) -> None:
        # Printing an all-zero or empty address here would compare as a
        # mismatch against the wrong thing and hide a guest with no NIC.
        result = self._ask("net9")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual("", result.stdout)

    def test_a_name_that_is_not_an_interface_name_is_refused(self) -> None:
        # The name becomes a path component, so anything outside the kernel's
        # own alphabet is a different file rather than a different device.
        for name in ("../../etc/passwd", "", "net0/address", "x" * 16):
            with self.subTest(name=name):
                result = self._ask(name)
                self.assertNotEqual(0, result.returncode)
                self.assertEqual("", result.stdout)

    def test_the_probe_exchange_and_entropy_modes_still_work(self) -> None:
        # The identity mode is a third mode, not a replacement: the readiness
        # round trip and the restored-guest entropy draw are what the fleet is
        # measured on.
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "probe-file"
            exchanged = subprocess.run(
                [str(self.binary), str(target)],
                input="fanout-nonce\n",
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(0, exchanged.returncode, exchanged.stderr)
            self.assertEqual("fanout-nonce\n", exchanged.stdout)
            entropy = subprocess.run(
                [str(self.binary), "--entropy", str(bench.ENTROPY_BYTES)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(0, entropy.returncode, entropy.stderr)
            self.assertEqual(bench.ENTROPY_HEX_CHARS, len(entropy.stdout.strip()))


class StaysUp:
    """A runner that never exits, so launch_vms keeps every VM it started."""

    pid = 4242
    returncode = None

    def poll(self) -> int | None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        return 0


def echo_nonce(command: Any, **kwargs: Any) -> Any:
    """A subprocess.run that hands the probe's own bytes back, as a guest does.

    The readiness exchange compares stdout against the nonce it sent, so a
    stand-in that returns it is the one answer that makes the SSH leg pass;
    anything else fails that comparison and is not what these runs are about.
    """
    return SimpleNamespace(returncode=0, stdout=kwargs.get("input", ""), stderr="")


class ReadyHealthHandler(http.server.BaseHTTPRequestHandler):
    """The guest's side of the readiness check: HEALTH_BODY on /health."""

    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(bench.HEALTH_BODY)))
        self.end_headers()
        self.wfile.write(bench.HEALTH_BODY)

    def log_message(self, *arguments: Any) -> None:
        pass


HEALTH_FORWARD = re.compile(
    rf"^hostfwd_add \S+ tcp:127\.0\.0\.1:(\d+)-:{bench.GUEST_HEALTH_PORT}$"
)


def start_health_forward(port: int) -> http.server.ThreadingHTTPServer:
    """A real listener on this host port, standing in for the guest behind it.

    Nothing answers the readiness health check except a forward the harness
    installed for that node, so the listener is started by the QMP stand-in
    that receives the forward rather than by the test: a control that ran
    against listeners the test had opened regardless would pass on a harness
    that withholds nothing at all.
    """
    # A stand-in that cannot bind fails the control rather than skipping it.
    # The port is one the stand-in was itself given, held by this test's own
    # reservation and released to the bring-up a moment earlier, so there is
    # no environmental condition here a skip could honestly stand in for: the
    # only outcome a skip leaves is a control that never ran reported as a
    # control that passed, which is the one outcome this control must not be
    # mistaken for. A HarnessError is what a raise on the bring-up's own
    # thread is read as; a SkipTest on that thread is an ordinary exception
    # the aggregate collects, and a SkipTest on the test's own thread is a
    # green OK, so neither is worth keeping the distinction for.
    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), ReadyHealthHandler)
    except OSError as error:
        raise bench.HarnessError(
            f"127.0.0.1:{port} cannot serve a test health forward: {error}"
        ) from error
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def close_forwards(forwards: Sequence[http.server.ThreadingHTTPServer]) -> None:
    for server in forwards:
        server.shutdown()
        server.server_close()


def hold_health_ports(count: int) -> list[bench.PortReservation]:
    """One kernel-assigned health port per node, held until its bring-up.

    The readiness check polls the port a node's health forward binds, so a
    stand-in that binds the fixed fleet base is a second fleet on the ports a
    real run uses: two suites at once collide, and the node that ends up
    holding a port answers for a node that is not there. Binding port 0
    leaves the choice to the kernel, and the socket stays bound until the
    bring-up closes the reservation, which is the handoff the real launcher
    makes before QEMU binds.
    """
    reservations: list[bench.PortReservation] = []
    for _ in range(count):
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            holder.bind(("127.0.0.1", 0))
            holder.listen(1)
        except OSError:
            holder.close()
            raise
        reservations.append(bench.PortReservation(closed_socket(), holder))
    return reservations


def close_reservations(reservations: Sequence[bench.PortReservation]) -> None:
    for reservation in reservations:
        reservation.close()


def home_health_ports(
    vms: Sequence[bench.VmProcess], reservations: Sequence[bench.PortReservation]
) -> None:
    """Point every node's health check at the port held for that node.

    Read off the holder the reservation carries, because closing the
    reservation is the one moment the port is free and after it the number is
    gone with the socket.
    """
    for vm, reservation in zip(vms, reservations, strict=True):
        vm.http_port = reservation.http.getsockname()[1]


def launch_on_held_ports(
    reservations: Sequence[bench.PortReservation],
) -> Any:
    """A launch that hands each node the port held for it.

    The launch names every node's health port from the port base and the
    forward is bound to the port the bring-up names, so the node has to be
    pointed at the held port for the readiness check to poll the port that
    actually answers. The real launch is still the one that runs.
    """
    real_launch_vms = bench.launch_vms

    def launch(
        runner: Path, run_dir: Path, count: int, vms: list[bench.VmProcess], *rest: Any, **kw: Any
    ) -> None:
        real_launch_vms(runner, run_dir, count, vms, *rest, **kw)
        home_health_ports(vms, reservations)

    return launch


class ForwardingQmp(FakeQmp):
    """A QMP connection whose host forwards are the forwards it is asked for.

    QEMU answers hostfwd_add by binding the host port and carrying it into the
    guest; standing in for the guest, this binds the host port and serves the
    health endpoint on it. The port the readiness check polls is therefore
    reachable only through a forward this connection was given.
    """

    def __init__(self, *, mac: str, forwards: list[http.server.ThreadingHTTPServer]) -> None:
        super().__init__(mac=mac)
        self.forwards = forwards

    def execute(
        self, command: str, arguments: dict[str, Any] | None, deadline: float
    ) -> Any:
        if command == "human-monitor-command" and arguments is not None:
            forward = HEALTH_FORWARD.match(arguments["command-line"])
            if forward is not None:
                self.forwards.append(start_health_forward(int(forward.group(1))))
        return super().execute(command, arguments, deadline)


# The control fails its node's health check by running the readiness deadline
# out, so the deadline is what bounds a control run here: long enough for the
# nodes that are healthy to be polled and record a reading, short enough that
# the test does not wait on it.
NEGATIVE_CONTROL_DEADLINE_S = 0.5


class ReadinessNegativeControlTest(unittest.TestCase):
    """The readiness checks are shown a bad node and have to reject the run.

    Every check in the readiness path is fail-closed, and a fail-closed check
    nobody has seen fail is a claim rather than evidence: a launcher that
    answered every health request with "ready" would score a perfect time
    forever. The control is the run that tells the two apart, by denying one
    node its health endpoint at the launch and requiring the ordinary run to
    be rejected for that node and no other.
    """

    def control_args(self, root: Path, *, count: int, control_vm: int | None) -> Any:
        """A parsed command line for one test fleet, on a scratch that stays.

        The parser's own scratch directory is gone by the time it returns, and
        a run has to work in a directory and copy a key, so both are pointed
        at this test's own.
        """
        extra = ["--boot", "cold", "--startup-deadline", str(NEGATIVE_CONTROL_DEADLINE_S)]
        if control_vm is not None:
            extra += ["--negative-control-vm", str(control_vm)]
        args = parse_harness_args(count=count, extra=extra)
        key = root / "id_test"
        key.write_text("", encoding="utf-8")
        args.tmpdir = root
        args.ssh_key = key
        return args

    @contextlib.contextmanager
    def fleet(self, *, count: int) -> Any:
        """The harness's own benchmark, over guests this host stands in for.

        Nothing of the readiness path is replaced: launch_vms, bring_up_vm, the
        readiness loop, the SSH data exchange and the HTTP health check all
        run as they do in a benchmark, and a node's health port answers only
        because the bring-up installed the forward that names it. What is
        replaced is the two things a unit test has no way to provide: the
        QEMU process a launch spawns, and the guest behind ssh.
        """
        forwards: list[http.server.ThreadingHTTPServer] = []
        with contextlib.ExitStack() as stack:
            # The scratch budget walks the payload to size it; how large a
            # store path is belongs to VmScratchCapacityTest, and every one of
            # these runs would pay for the same walk.
            stack.enter_context(mock.patch.object(bench, "store_path_bytes", return_value=0))
            stack.callback(close_forwards, forwards)
            reservations = hold_health_ports(count)
            stack.callback(close_reservations, reservations)
            stack.enter_context(
                mock.patch.object(bench, "reserve_ports", return_value=reservations)
            )
            stack.enter_context(
                mock.patch.object(
                    bench, "launch_vms", side_effect=launch_on_held_ports(reservations)
                )
            )
            stack.enter_context(mock.patch.object(bench, "create_vm_scratch", return_value=None))
            stack.enter_context(mock.patch.object(bench, "process_group_alive", return_value=False))
            stack.enter_context(
                mock.patch.object(bench.subprocess, "Popen", side_effect=lambda *a, **k: StaysUp())
            )
            stack.enter_context(
                mock.patch.object(bench.subprocess, "run", side_effect=echo_nonce)
            )
            stack.enter_context(
                mock.patch.object(bench, "connect_qmp", return_value=closed_socket())
            )
            stack.enter_context(
                mock.patch.object(
                    bench,
                    "QmpConnection",
                    side_effect=lambda connection, index: ForwardingQmp(
                        mac=bench.guest_mac(index), forwards=forwards
                    ),
                )
            )
            yield

    def control(self, *, count: int, control_vm: int) -> dict[str, Any]:
        """One control run, as a record, over a fleet the host stands in for."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.control_args(root, count=count, control_vm=control_vm)
            run_dir = Path(tempfile.mkdtemp(prefix="fv-", dir=root))
            with self.fleet(count=count):
                return bench.run_negative_control(args, run_dir)

    def test_the_run_is_rejected_for_the_node_denied_its_health_endpoint(self) -> None:
        record = self.control(count=3, control_vm=2)
        self.assertTrue(record["detected"], record)
        self.assertEqual("detected", record["status"])
        self.assertEqual("health_forward_withheld", record["fault"])
        self.assertEqual(2, record["negative_control_vm"])

    def test_the_failure_names_the_node_that_was_denied_and_no_other(self) -> None:
        # A harness that named any node at all would satisfy "the run failed",
        # so the name is checked against the node the control actually
        # denied, and against the two it did not. The aggregate is checked as
        # well, because the control's value is that it produces the error an
        # operator sees from a genuine multi-VM startup failure: a readiness
        # failure re-raised bare out of its thread names the right node and
        # loses the "fleet startup failed:" and its per-node detail lines that
        # the operator reads it by.
        for control_vm in (1, 2, 3):
            with self.subTest(control_vm=control_vm):
                record = self.control(count=3, control_vm=control_vm)
                error = record["observation"]["error"]
                self.assertTrue(
                    error.startswith("fleet startup failed:"), error
                )
                self.assertIn(f"\n  - VM {control_vm}: ", error)
                for other in (1, 2, 3):
                    if other != control_vm:
                        self.assertNotIn(f"VM {other}", error)

    def test_every_other_node_stayed_ready(self) -> None:
        # The control denies one node, so a harness that rejected all three
        # would pass the first test while proving nothing about the node the
        # fault belongs to.
        record = self.control(count=3, control_vm=2)
        health = record["observation"]["last_http_results"]
        self.assertEqual(bench.HEALTH_OK, health["1"])
        self.assertEqual(bench.HEALTH_OK, health["3"])
        self.assertNotEqual(bench.HEALTH_OK, health["2"])
        self.assertEqual(2, record["statuses"]["nodes_health_ready"])

    def test_the_denied_node_passed_its_ssh_exchange(self) -> None:
        # What makes the failure attributable rather than a deadline that
        # could mean anything: the node was up and moving data the whole time,
        # and the leg that stopped is the one the fault removed.
        record = self.control(count=3, control_vm=2)
        self.assertEqual(bench.SSH_EXCHANGE_OK, record["observation"]["last_ssh_results"]["2"])
        self.assertIn("Connection refused", record["observation"]["last_http_results"]["2"])

    def test_a_run_whose_health_checks_all_answer_is_not_a_detected_control(self) -> None:
        # The mutation this control exists to catch: a launcher that reports
        # every node ready. Readiness then passes, the run goes on and fails
        # later for a different reason, and a verdict taken from the presence
        # of any error at all would call that a detected control.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.control_args(root, count=3, control_vm=2)
            run_dir = Path(tempfile.mkdtemp(prefix="fv-", dir=root))
            with (
                self.fleet(count=3),
                mock.patch.object(
                    bench, "check_http_health", return_value=(True, bench.HEALTH_OK)
                ),
            ):
                record = bench.run_negative_control(args, run_dir)
        self.assertFalse(record["detected"], record)
        self.assertEqual("not_detected", record["status"])

    def test_a_control_that_detects_a_run_which_never_failed_is_not_detected(self) -> None:
        # A fault that never took leaves a run that was never rejected, and a
        # control that passed on that would be measuring the run's exit
        # status rather than its checks.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(bench, "execute_benchmark", return_value={"status": "ok"}):
                record = bench.run_negative_control(
                    self.control_args(root, count=2, control_vm=1),
                    Path(tempfile.mkdtemp(prefix="fv-", dir=root)),
                )
        self.assertFalse(record["detected"], record)
        self.assertEqual("the run was accepted", record["observation"]["error"])

    def test_a_failure_on_another_node_is_not_this_node_s_control(self) -> None:
        # The verdict is read off the node that was denied, so a report whose
        # only unhealthy node is a different one cannot satisfy it, however
        # clearly the run was rejected.
        def report(ready: list[str], unhealthy: str) -> dict[str, Any]:
            return {
                "readiness": {
                    "http_ready": [index for index in ("1", "2", "3") if index != unhealthy],
                    "last_http_results": {
                        index: "ready" if index != unhealthy else "refused"
                        for index in ("1", "2", "3")
                    },
                    "ready": ready,
                }
            }

        self.assertFalse(bench.control_detected(report(["1", "2"], "4"), index=3, count=3))
        # And a run whose denied node is counted ready anyway is a launcher
        # that accepted the failed health leg, whatever the reading says.
        self.assertFalse(bench.control_detected(report(["1", "2", "3"], "3"), index=3, count=3))
        self.assertTrue(bench.control_detected(report(["1", "2"], "3"), index=3, count=3))

    def test_an_ordinary_run_forwards_every_node_s_health_endpoint(self) -> None:
        # The option is off by default, so the launch withholds nothing and
        # the bring-up issues the forwards it always issued.
        with tempfile.TemporaryDirectory() as directory:
            started: list[bench.VmProcess] = []
            with mock.patch.object(
                bench.subprocess, "Popen", side_effect=lambda *a, **k: StaysUp()
            ):
                bench.launch_vms(Path("/nix/store/xyz-microvm-run"), Path(directory), 2, started)
            for vm in started:
                vm.log.close()
        self.assertEqual([True, True], [vm.health_forwarded for vm in started])

    def test_an_ordinary_fleet_becomes_ready_through_the_readiness_path(self) -> None:
        # The same loop, the same two checks, every node answering: the control
        # is a property of one node's launch, not of the readiness path.
        count = 3
        with self.fleet(count=count), contextlib.ExitStack() as stack:
            vms = [vm_process(index, process=StaysUp()) for index in range(1, count + 1)]
            reservations = hold_health_ports(count)
            stack.callback(close_reservations, reservations)
            home_health_ports(vms, reservations)
            bench.start_all_vms(
                vms, reservations, Path("/tmp/id_test"), time.monotonic() + 10.0, None, "probe /p"
            )
        self.assertEqual(
            [bench.HEALTH_OK] * count, [vm.last_http_result for vm in vms]
        )
        self.assertEqual(
            [bench.SSH_EXCHANGE_OK] * count, [vm.last_ssh_result for vm in vms]
        )

    def test_the_health_forward_is_withheld_from_the_denied_node_only(self) -> None:
        # An ordinary bring-up issues exactly the two forwards it always
        # issued, in that order; a denied node's issues one, and the other is
        # its health endpoint rather than the node: its SSH forward is still
        # there, so it boots and answers and only /health is unreachable.
        qmp, healthy = self._bring_up(3, health_forwarded=True)
        self.assertEqual(
            [
                f"hostfwd_add net0 tcp:127.0.0.1:{healthy.ssh_port}-:{bench.GUEST_SSH_PORT}",
                f"hostfwd_add net0 tcp:127.0.0.1:{healthy.http_port}-:{bench.GUEST_HEALTH_PORT}",
            ],
            qmp.host_monitor,
        )
        denied_qmp, denied = self._bring_up(3, health_forwarded=False)
        self.assertEqual(
            [f"hostfwd_add net0 tcp:127.0.0.1:{denied.ssh_port}-:{bench.GUEST_SSH_PORT}"],
            denied_qmp.host_monitor,
        )
        self.assertEqual(healthy.http_port, denied.http_port)

    def _bring_up(self, index: int, *, health_forwarded: bool) -> tuple[Any, Any]:
        qmp = FakeQmp(mac=bench.guest_mac(index))
        vm = vm_process(index, health_forwarded=health_forwarded)
        with (
            mock.patch.object(bench, "connect_qmp", return_value=closed_socket()),
            mock.patch.object(bench, "QmpConnection", return_value=qmp),
        ):
            bench.bring_up_vm(
                vm,
                bench.PortReservation(closed_socket(), closed_socket()),
                10.0,
                bench.Snapshot(Path("/tmp/ram"), 512, Path("/tmp/state"), Path("/tmp/ov")),
            )
        return qmp, vm

    def test_the_control_is_off_unless_it_is_asked_for(self) -> None:
        self.assertIsNone(parse_harness_args().negative_control_vm)

    def test_a_node_the_fleet_does_not_have_is_refused(self) -> None:
        for value in ("0", "3", "65", "x"):
            with self.subTest(value=value), mock.patch("sys.stderr"):
                with self.assertRaises(SystemExit):
                    parse_harness_args(extra=["--negative-control-vm", value])

    def test_a_node_of_the_fleet_is_accepted(self) -> None:
        self.assertEqual(
            2, parse_harness_args(extra=["--negative-control-vm", "2"]).negative_control_vm
        )

    def test_the_readiness_path_is_not_told_which_node_was_denied(self) -> None:
        # A branch in the readiness path that recognised the denied node would
        # turn the control into a rehearsal of that branch, and the branch
        # into the thing the control proves. The fault is installed at the
        # launch, so the code a control run exercises must read nothing about
        # it: it meets a port nothing is listening on, which is all a guest
        # that never serves health looks like from the host.
        control_names = {"health_forwarded", "negative_control", "withheld_health"}
        functions = (
            "wait_for_vm_ready",
            "check_http_health",
            "start_all_vms",
            "collect_parallel_failures",
        )
        tree = ast.parse(BENCH_PATH.read_text(encoding="utf-8"))
        for name in functions:
            function = next(
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name == name
            )
            read = {
                node.id if isinstance(node, ast.Name) else node.attr
                for node in ast.walk(function)
                if isinstance(node, (ast.Name, ast.Attribute))
            }
            with self.subTest(function=name):
                self.assertEqual([], sorted(read & control_names))



DRIVER_PATH = Path(__file__).with_name("run.sh")
DEFAULT_NIX_PATH = Path(__file__).with_name("default.nix")
REPO_FLAKE_PATH = Path(__file__).parents[2] / "flake.nix"


def driver_console() -> str:
    """run.sh's own log and die, so a refusal is the driver's own message."""
    return (
        "set -euo pipefail\n"
        'log() { printf \'[fanout64] %s\\n\' "$*" >&2; }\n'
        'die() { log "ERROR: $*"; exit 1; }\n'
    )


def linux_system_guard() -> str:
    """run.sh's expansion and check of FANOUT_LINUX_SYSTEM, taken verbatim.

    The three statements that make the decision, and not the assignments that
    happen to sit between them: the requested system, the systems the flake
    builds for, and the test that refuses anything else. Slicing one block out
    of the middle instead would run the rest of the driver's defaults with
    their own environment requirements, which is not what is under test.
    """
    text = DRIVER_PATH.read_text(encoding="utf-8")
    statements = [
        r"(?m)^LINUX_SYSTEM=\$\{FANOUT_LINUX_SYSTEM[^\n]*",
        r'(?m)^LINUX_SYSTEMS="[^"]*"',
        r'(?ms)^\[\[ " \$LINUX_SYSTEMS " ==.*\n  die [^\n]*\n[^\n]*$',
    ]
    found = []
    for statement in statements:
        match = re.search(statement, text)
        if match is None:
            raise AssertionError(f"run.sh has no statement matching {statement!r}")
        found.append(match.group(0))
    return "\n".join(found)


def run_linux_system_guard(system: str | None) -> subprocess.CompletedProcess[str]:
    """The guard on its own, with nothing above or below it in run.sh executed.

    run.sh's set -u is part of what is being tested, so the slice runs under it.
    None leaves FANOUT_LINUX_SYSTEM out of the environment, which is the unset
    case rather than the empty one.
    """
    environment = dict(os.environ)
    environment.pop("FANOUT_LINUX_SYSTEM", None)
    if system is not None:
        environment["FANOUT_LINUX_SYSTEM"] = system
    return subprocess.run(
        [
            "bash",
            "-c",
            driver_console()
            + linux_system_guard()
            + '\nprintf "accepted %s\\n" "$LINUX_SYSTEM"\n',
        ],
        capture_output=True,
        text=True,
        env=environment,
    )


def linux_build_statement() -> str:
    """The one statement in run_linux that asks nix for the fleet package."""
    text = DRIVER_PATH.read_text(encoding="utf-8")
    match = re.search(r"(?ms)^  out=\$\(nix build.*?\) \|\|$", text)
    if match is None:
        raise AssertionError("run.sh has no nix build statement in run_linux")
    # The trailing `||` belongs to the error handler, which is not under test.
    return match.group(0)[: -len(" ||")]


def run_linux_build(system: str | None) -> subprocess.CompletedProcess[str]:
    """run.sh's own build statement, against a nix that reports its installattrs.

    The attribute is what this exists to observe, so it is read out of the
    invocation rather than out of the source text: a driver still asking for
    the literal packages.x86_64-linux.fanout64 prints that same attribute for
    either system, which is the defect these tests exist to fail.
    """
    environment = dict(os.environ)
    environment.pop("FANOUT_LINUX_SYSTEM", None)
    if system is not None:
        environment["FANOUT_LINUX_SYSTEM"] = system
    return subprocess.run(
        [
            "bash",
            "-c",
            driver_console()
            + linux_system_guard()
            + "\nLINUX_HOST=root@fleet.example\nstage=/tmp/prefixed-commit\n"
            + "results=$(mktemp -d)\n"
            + 'nix() { local arg; for arg in "$@"; do :; done; printf "%s\\n" "$arg"; }\n'
            + linux_build_statement()
            + '\nprintf "built %s\\n" "$out"\n',
        ],
        capture_output=True,
        text=True,
        env=environment,
    )


class LinuxTargetSystemTest(unittest.TestCase):
    """The Linux leg's package attribute has to follow the host it builds for.

    The fleet spans two architectures and FANOUT_LINUX_HOST can name any host in
    it, so the system being built for is a separate fact about the target.
    Asking nix for packages.x86_64-linux.fanout64 regardless of it does not fail
    the build: the remote --store hands that closure to the target host, which
    then runs the wrong architecture. So the attribute is derived from the
    requested system, and a system the flake cannot build is refused by name
    before nix is reached.
    """

    def test_the_requested_package_attribute_follows_the_requested_system(self) -> None:
        built = {}
        for system in ("x86_64-linux", "aarch64-linux"):
            result = run_linux_build(system)
            self.assertEqual(0, result.returncode, result.stderr)
            built[system] = result.stdout
        self.assertEqual(
            "built path:/tmp/prefixed-commit#packages.x86_64-linux.fanout64\n",
            built["x86_64-linux"],
        )
        self.assertEqual(
            "built path:/tmp/prefixed-commit#packages.aarch64-linux.fanout64\n",
            built["aarch64-linux"],
        )
        # The two requests must not resolve to one attribute. That is the whole
        # defect: a value that ignores what was asked for, so both requests get
        # the x86_64 closure and an aarch64 host is handed one it cannot run.
        self.assertNotEqual(built["x86_64-linux"], built["aarch64-linux"])

    def test_an_unsupported_system_never_reaches_a_build(self) -> None:
        result = run_linux_build("riscv64-linux")
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertEqual("", result.stdout)

    def test_the_linux_build_asks_for_the_package_of_that_system(self) -> None:
        driver = DRIVER_PATH.read_text(encoding="utf-8")
        self.assertIn('"path:$stage#packages.${LINUX_SYSTEM}.fanout64"', driver)
        self.assertNotIn("packages.x86_64-linux.fanout64", driver)

    def test_an_unsupported_system_is_refused_naming_the_request_and_the_set(self) -> None:
        result = run_linux_system_guard("riscv64-linux")
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("riscv64-linux", result.stderr)
        for supported in ("aarch64-linux", "x86_64-linux"):
            self.assertIn(supported, result.stderr)
        # Refused, not quietly taken as the default: the run must not proceed on
        # any system other than the one that was refused.
        self.assertEqual("", result.stdout)

    def test_an_empty_system_is_refused_rather_than_taken_as_the_default(self) -> None:
        result = run_linux_system_guard("")
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn("asked for ''", result.stderr)
        self.assertEqual("", result.stdout)

    def test_an_unset_system_defaults_to_one_the_flake_builds(self) -> None:
        result = run_linux_system_guard(None)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("accepted x86_64-linux\n", result.stdout)


class PlatformMapAgreementTest(unittest.TestCase):
    """flake.nix and default.nix have to name the same set of host systems.

    They are two files with no shared declaration: supportedSystems decides
    which hosts the flake has attribute paths for, and guestSystems decides
    which host a microVM image can be built on. A system in one and not the
    other is a host the driver can be pointed at and cannot get an image from,
    or an image with no attribute path to ask for.
    """

    def setUp(self) -> None:
        flake = REPO_FLAKE_PATH.read_text(encoding="utf-8")
        self.supported = set(
            re.findall(
                r'"([^"]+)"',
                nix_block(flake, "      supportedSystems = [", r"\n      \];"),
            )
        )
        default_nix = DEFAULT_NIX_PATH.read_text(encoding="utf-8")
        self.guests = dict(
            re.findall(
                r'(?m)^\s*([a-z0-9_-]+)\s*=\s*"([a-z0-9_-]+)";',
                nix_block(default_nix, "  guestSystems = {", r"\n  \};"),
            )
        )

    def test_every_supported_system_has_a_guest_mapping(self) -> None:
        self.assertEqual(self.supported, set(self.guests))

    def test_every_guest_system_is_one_the_flake_builds(self) -> None:
        for host, guest in self.guests.items():
            with self.subTest(host=host):
                self.assertIn(guest, self.supported)

    def test_an_aarch64_linux_host_runs_an_aarch64_linux_guest(self) -> None:
        # The identity mapping, unlike the aarch64-darwin host above it: an
        # aarch64-linux host runs an aarch64-linux guest on its own
        # architecture, with no emulation and no cross builder. This is what
        # makes the pdx-nxmm hosts usable.
        self.assertEqual("aarch64-linux", self.guests["aarch64-linux"])

    def test_the_driver_offers_only_systems_the_flake_builds(self) -> None:
        offered = re.search(r'(?m)^LINUX_SYSTEMS="([^"]+)"', linux_system_guard())
        self.assertIsNotNone(offered)
        for system in offered.group(1).split():
            with self.subTest(system=system):
                self.assertIn(system, self.supported)
                self.assertIn(system, self.guests)


if __name__ == "__main__":
    unittest.main()
