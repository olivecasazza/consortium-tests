#!/usr/bin/env python3
"""Launch and benchmark the test-only fanout microVM fleet."""

from __future__ import annotations

import argparse
import concurrent.futures
import functools
import hashlib
import http.client
import json
import os
import platform
import shlex
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Any, Callable, Sequence

SSH_PORT_BASE = 22200
HTTP_PORT_BASE = 28200
MAX_VM_COUNT = 64
QMP_SOCKET_NAME = "fanout.qmp"
# Darwin's sockaddr_un.sun_path is 104 bytes including the trailing NUL.
MAX_QMP_SOCKET_PATH_BYTES = 103
# The goal this fleet is measured against: 64 nodes ready on both platforms in
# under 8 seconds. Do not loosen this to make a regression look like a pass.
READINESS_TARGET_S = 8.0
HOST_COPY_TIMEOUT_S = 600.0
CASCADE_TIMEOUT_S = 900.0
VERIFY_TIMEOUT_S = 30.0
PEER_PROBE_TIMEOUT_S = 15.0
# Per-attempt readiness probe limits. 64 concurrent SSH handshakes on a busy
# host can take seconds; killing an attempt that is still progressing and
# starting over livelocks the fleet. The startup deadline bounds the total.
READY_PROBE_CONNECT_TIMEOUT_S = 5
READY_PROBE_TIMEOUT_S = 10.0
# Readiness data exchange: write stdin to a guest file, sync it, read it back.
# The file must live on the guest's block-backed writable store, not on the
# initrd tmpfs that backs /. sync(2) on a tmpfs file is a no-op, so a probe
# under /root would pass without the write ever reaching virtio-blk, the
# qcow2 overlay, or the host. /nix/.rw-store is the ext4 volume from
# microvm.volumes[]; assert_probe_file_is_durable keeps it that way.
READY_PROBE_FILE = "/nix/.rw-store/fanout-ready-probe"
# The bytes a ready guest serves on /health, and the word check_http_health
# reports for a health request that got them. Both are recorded per node: a
# run that fails on readiness has to say which leg of the exchange stopped
# answering, and an expired deadline on its own says neither.
HEALTH_BODY = b"ready\n"
HEALTH_OK = "ready"
# The readiness exchange's name for an SSH leg that passed: the exact bytes
# the probe sent came back out of the guest's store.
SSH_EXCHANGE_OK = "nonce echoed"
# Random bytes each node draws for the restored-guest entropy check. The
# guest prints them hex-encoded, so 32 bytes arrive as 64 characters.
ENTROPY_BYTES = 32
ENTROPY_HEX_CHARS = ENTROPY_BYTES * 2
# The remote command that performs the exchange is supplied by the caller
# (--ready-probe-command), because the probe binary lives in the guest's store
# closure and its path is a build output. The verification stays here and is
# not negotiable: whatever the command is, its stdout must equal the nonce
# byte for byte, so a wrong command fails loudly rather than passing quietly.
TERMINATE_GRACE_S = 5.0
INVENTORY_GUEST_PATH = "/tmp/consortium-fanout-inventory.toml"
# Must match microvm.volumes[].image in guest.nix; the runner creates it in cwd.
OVERLAY_IMAGE_NAME = "overlay.img"
# The three octets every per-VM address starts from: 0x02 is the
# locally-administered bit, and the low bit of the address stays clear, so the
# result is a unicast address the host network cannot already own. guest.nix
# carries this prefix in the interface's mac, because microvm.nix requires one,
# and matches its network on the interface name rather than on this, because
# the address differs per launch.
GUEST_MAC_PREFIX = 0x020000
GUEST_MAC_INDEX_LIMIT = 0xFFFFFF
# The interface the guest reads its own address from. guest.nix pins this
# name with a .link unit, so it is the name the captured guest and all 64
# restores have; the harness asks for the address by this name.
GUEST_NIC_NAME = "net0"
# Where a guest finds the interface, the address it currently has, the device
# behind it, and the driver that probes that device. All four are read inside
# the guest: the address is what the re-probe is for, the device's name is what
# the unbind and bind are addressed by, and the driver is the re-probe itself.
GUEST_NIC_SYSFS_PATH = f"/sys/class/net/{GUEST_NIC_NAME}"
GUEST_NIC_ADDRESS_PATH = f"{GUEST_NIC_SYSFS_PATH}/address"
GUEST_NIC_DEVICE_PATH = f"{GUEST_NIC_SYSFS_PATH}/device"
VIRTIO_NET_DRIVER_PATH = "/sys/bus/virtio/drivers/virtio_net"
# The address a guest reports to: slirp's gateway, which is the host as the guest
# sees it, and the only way back from a node whose forward no longer aims at it.
GUEST_REPORT_GATEWAY = "10.0.2.2"
# A re-probe costs its node the network for about a second, after which networkd
# re-acquires a lease -- a different one, since a new MAC is a new client to
# slirp's DHCP server, which is why the forwards are re-aimed afterwards. What
# is waited out here is generous on purpose: this budget is readiness, and a
# node that never reports is a failed run rather than a slow one.
NIC_REPROBE_TIMEOUT_S = 120.0
NIC_REPROBE_POLL_S = 0.25
# The guest-side ports the two host forwards carry. Named because a forward
# is a claim about one of them, and which one it is has to be readable where
# the forward is issued.
GUEST_SSH_PORT = 22
GUEST_HEALTH_PORT = 8080
# QEMU holds the address it programmed into a NIC as a QOM property, which is
# the host's own view of the device and needs no help from the guest to read.
# The machine's own child containers are the places a device can be, so the
# NIC is looked for among them rather than at one path this harness names.
QMP_MACHINE_PATH = "/machine"
QMP_NIC_MAC_PROPERTY = "mac"
# qom-list reports a child of the listed path as child<TYPE> and a property of
# it as that property's own type, so the type alone tells the two apart. A
# match has to be a child: every container here also lists a property named
# "type" whose type is the string "string", and a link property reports
# link<TYPE>, so neither is a device and neither can be a NIC.
QMP_CHILD_TYPE_PREFIX = "child<"
QMP_CONTAINER_TYPE = "container"
QMP_NIC_TYPE = "virtio-net"
# The environment variable the runner substitutes into the NIC's -device line
# for the address this launch names (see default.nix). It is per-child because
# QEMU realizes the device while building the machine: a realized device's
# properties are read-only, so the address has to be chosen as the device is
# created, not written once the monitor is reachable.
QMP_NIC_MAC_ENV = "FANOUT_NIC_MAC"
# RAM-backed home for the fleet's -snapshot disk overlays (see create_vm_scratch).
VM_SCRATCH_ROOT = Path("/dev/shm")
# Per-guest RAM-scratch allowance for the qcow2 overlay itself, its ext4
# metadata, and the readiness probe's write, on top of the payload size.
SCRATCH_PER_GUEST_MIB = 16
# Bump when the capture procedure or state format changes so stale cached
# snapshots are never restored into a mismatched launcher.
SNAPSHOT_FORMAT = "shared-ram-v1"
SNAPSHOT_CAPTURE_TIMEOUT_S = 180.0
# Each distinct runner (any guest change) adds a ~565 MB snapshot directory
# and nothing evicts it. Keep whatever has been used in this window; anything
# older is a build that is no longer referenced and can only be recaptured.
SNAPSHOT_RETENTION_DAYS = 7
SNAPSHOT_RETENTION_S = SNAPSHOT_RETENTION_DAYS * 24 * 60 * 60
MIGRATION_POLL_S = 0.01
# connect_qmp retries the QMP socket path while QEMU is still starting. Start
# tight so a fast starter is picked up promptly, then back off to the old flat
# step so a slow one is not polled hundreds of times.
QMP_POLL_INITIAL_S = 0.001
QMP_POLL_MAX_S = 0.02
# Settle guest writes and drop caches before capture, so the RAM image holds
# no stale page cache (init_on_free=1 in guest.nix zeroes what is freed).
GUEST_SNAPSHOT_PREP = (
    "sync && echo 3 > /proc/sys/vm/drop_caches && echo 1 > /proc/sys/vm/compact_memory"
)
# Guest RAM lives in a file-backed memory backend, so migration skips it
# (x-ignore-shared) and the state file holds only device state and small
# RAM blocks (mapped-ram).
GUEST_RAM_ID = "fanout-ram"
SNAPSHOT_CAPABILITIES = {
    "capabilities": [
        {"capability": "mapped-ram", "state": True},
        {"capability": "x-ignore-shared", "state": True},
    ]
}



class HarnessError(RuntimeError):
    """An actionable benchmark failure."""


class BenchmarkFailed(HarnessError):
    """A run that failed, carrying what it had measured when it did.

    A run stops at the first check it cannot pass, but the phases before that
    point have already recorded what they measured, and a reader given only
    the message has to repeat the whole run to recover it. The report travels
    with the failure so main can print it where the result of a passing run
    is printed, which keeps a failed run's output as readable as a passing
    one's.
    """

    def __init__(self, message: str, report: dict[str, Any]) -> None:
        super().__init__(message)
        self.report = report


@dataclass
class PortReservation:
    ssh: socket.socket
    http: socket.socket

    def close(self) -> None:
        self.ssh.close()
        self.http.close()


@dataclass(frozen=True)
class Snapshot:
    """A booted guest captured once per runner: its RAM, device state, and disk."""

    ram: Path
    ram_mib: int
    state: Path
    overlay: Path


def guest_ram_args(ram: Path, ram_mib: int, *, share: bool) -> list[str]:
    """QEMU arguments placing guest RAM in the file ram.

    Capture maps it shared, so the running guest's memory is the snapshot
    image. Restores map it private: pages are shared copy-on-write through the
    host page cache, so a restore loads nothing and each VM owns only the pages
    it writes.
    """
    return [
        "-object",
        f"memory-backend-file,id={GUEST_RAM_ID},size={ram_mib}M,"
        f"mem-path={ram},share={'on' if share else 'off'}",
        "-machine",
        f"memory-backend={GUEST_RAM_ID}",
    ]


def guest_mac(index: int) -> str:
    """The MAC address of VM ``index`` (1-based, as every VM number here).

    Nothing in a restored guest is written per VM: 64 of them resume one
    captured snapshot, and fw_cfg is read only at boot, which a restore never
    reaches. The image's own mac cannot carry a per-VM value either, so the
    NIC's address is chosen by the launch, which puts it on the device as the
    machine is built (see launch_vms), and the guest reads it back from there.
    0x02 in the top octet marks it locally administered and the low bit stays
    clear, so it is unicast and cannot collide with a vendor address on the
    host network. The index fills the low three octets, so VM 1 keeps
    02:00:00:00:00:01, which is both IMAGE_GUEST_MAC and the address the
    capture was taken with.
    """
    if not 1 <= index <= GUEST_MAC_INDEX_LIMIT:
        raise HarnessError(f"VM {index}: index is out of range for a derived MAC address")
    packed = GUEST_MAC_PREFIX << 24 | index
    return ":".join(f"{octet:02x}" for octet in packed.to_bytes(6, "big"))


# The address the guest image carries (guest.nix, microvm.interfaces[].mac), and
# the default the runner falls back to when a launch names none.
# microvm.nix declares mac with no default, so the image cannot omit it, and it
# writes that value straight into the NIC's -device line. The runner substitutes
# the launch's own address in its place (see QMP_NIC_MAC_ENV), because a
# -device is realized while QEMU builds the machine and a realized device's
# properties are read-only. The capture guest is VM 1, so carrying VM 1's
# address is what keeps the capture booting on the address its snapshot is read
# back under; every launch names its own.
IMAGE_GUEST_MAC = guest_mac(1)


@dataclass
class VmProcess:
    index: int
    workdir: Path
    qmp_socket: Path
    ssh_port: int
    http_port: int
    process: subprocess.Popen[bytes]
    process_group: int
    log: IO[bytes]
    # The exact bytes this VM echoed back from READY_PROBE_FILE. Every
    # restore shares one CoW-mapped RAM image and one read-only golden
    # overlay, so a leak between VMs is the failure mode every restore
    # optimization could introduce; the nonce is what makes it detectable.
    probe_nonce: str | None = None
    # The address this guest read out of its own NIC, and the one QEMU holds
    # for it. They are recorded from opposite sides so a disagreement can be
    # attributed to the device or to the guest rather than guessed at.
    guest_address: str | None = None
    device_address: str | None = None
    # The last reading from each side of this VM's readiness exchange. The
    # loop's locals are gone once it raises, and a run that stops on
    # readiness has to publish which leg stopped rather than only that a
    # deadline passed.
    last_ssh_result: str = "not attempted"
    last_http_result: str = "not attempted"
    # False for a node whose health forward the launch withheld. Nothing
    # downstream is told: its readiness loop polls its health port exactly as
    # every other node's is polled and finds nothing listening.
    health_forwarded: bool = True
    # Whether this VM's readiness exchange both legs ever completed. A node
    # whose exchange is still unfinished when the run stops is the node the
    # run stopped on, which a last reading alone does not say: a launcher that
    # accepted a failed health leg would have a reading and no failure.
    ready: bool = False

class QmpConnection:
    """Minimal line-oriented QEMU Machine Protocol client."""

    def __init__(self, connection: socket.socket, vm_index: int) -> None:
        self._connection = connection
        self._vm_index = vm_index
        self._buffer = bytearray()
        self._next_id = 1

    def negotiate(self, deadline: float) -> None:
        greeting = self._receive(deadline)
        if "QMP" not in greeting:
            raise HarnessError(
                f"VM {self._vm_index}: invalid QMP greeting: {greeting!r}"
            )
        self.execute("qmp_capabilities", None, deadline)

    def execute(
        self,
        command: str,
        arguments: dict[str, Any] | None,
        deadline: float,
    ) -> Any:
        request_id = self._next_id
        self._next_id += 1
        request: dict[str, Any] = {"execute": command, "id": request_id}
        if arguments is not None:
            request["arguments"] = arguments
        payload = json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\r\n"

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HarnessError(
                f"VM {self._vm_index}: startup deadline expired before QMP {command}"
            )
        self._connection.settimeout(remaining)
        try:
            self._connection.sendall(payload)
        except (OSError, TimeoutError) as error:
            raise HarnessError(
                f"VM {self._vm_index}: failed to send QMP {command}: {error}"
            ) from error

        while True:
            response = self._receive(deadline)
            if response.get("id") != request_id:
                # QMP events have no matching request id and can arrive at any time.
                continue
            if "error" in response:
                raise HarnessError(
                    f"VM {self._vm_index}: QMP {command} failed: "
                    f"{json.dumps(response['error'], sort_keys=True)}"
                )
            if "return" not in response:
                raise HarnessError(
                    f"VM {self._vm_index}: QMP {command} returned no result: {response!r}"
                )
            return response["return"]

    def _receive(self, deadline: float) -> dict[str, Any]:
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                raw = bytes(self._buffer[:newline]).rstrip(b"\r")
                del self._buffer[: newline + 1]
                if not raw:
                    continue
                try:
                    message = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError) as error:
                    raise HarnessError(
                        f"VM {self._vm_index}: malformed QMP response: {raw!r}"
                    ) from error
                if not isinstance(message, dict):
                    raise HarnessError(
                        f"VM {self._vm_index}: non-object QMP response: {message!r}"
                    )
                return message

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HarnessError(
                    f"VM {self._vm_index}: timed out waiting for a QMP response"
                )
            self._connection.settimeout(remaining)
            try:
                chunk = self._connection.recv(65536)
            except (OSError, TimeoutError) as error:
                raise HarnessError(
                    f"VM {self._vm_index}: failed while reading QMP: {error}"
                ) from error
            if not chunk:
                raise HarnessError(
                    f"VM {self._vm_index}: QMP disconnected before responding"
                )
            self._buffer.extend(chunk)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Launch test microVMs and benchmark log2 Nix closure distribution."
    )
    parser.add_argument("--runner", required=True, type=Path, help="microVM runner executable")
    parser.add_argument("--ssh-key", required=True, type=Path, help="test-only host SSH key")
    parser.add_argument("--store-path", required=True, type=Path, help="Nix store path to copy")
    parser.add_argument(
        "--count",
        type=int,
        default=64,
        help="number of VMs (2 through 64; default: 64)",
    )
    parser.add_argument(
        "--startup-deadline",
        type=float,
        default=120.0,
        metavar="SECONDS",
        help=(
            "hard safety timeout for fleet startup (default: 120); "
            f"the separately reported performance target remains {READINESS_TARGET_S:g} seconds"
        ),
    )
    parser.add_argument(
        "--binary-relative-path",
        default="bin/hello",
        help="executable path relative to the copied store path (default: bin/hello)",
    )
    parser.add_argument(
        "--expect-stdout",
        required=True,
        help=(
            "exact stdout the deployed executable must produce; \\n is decoded to a "
            "newline. Required: without it, payload verification degrades to an "
            "exit-status check that still reports full marks"
        ),
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        help=(
            "directory to record the cascade event stream in, including when "
            "the relay check rejects the run. It must survive the run: the "
            "fleet's own scratch directory is removed during cleanup, which "
            "is exactly when the evidence matters most"
        ),
    )
    parser.add_argument(
        "--ready-probe-binary",
        required=True,
        help=(
            "guest binary that performs the readiness data exchange and "
            "reports the guest's own NIC address: it must read stdin, write it "
            "to " + READY_PROBE_FILE + ", fsync, and write the bytes it reads "
            "back to stdout, and it must answer --address INTERFACE with the "
            "address that interface currently has, read at the time it is "
            "asked. Required, and the only part of the probe the caller "
            "supplies, because it is a guest store path that only the build "
            "knows. The file path and both comparisons stay here: stdout must "
            "equal the nonce byte for byte, and the reported address must be "
            "the one this launch assigned, so a wrong binary fails loudly "
        ),
    )
    parser.add_argument(
        "--boot",
        choices=("snapshot", "cold"),
        default="snapshot",
        help=(
            "snapshot (default): restore every VM from a booted guest captured once "
            "per runner and cached; cold: boot every VM from the kernel"
        ),
    )
    parser.add_argument(
        "--snapshot-cache",
        type=Path,
        default=Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
        / "fanout64-snapshots",
        help="directory holding captured snapshots, keyed by runner (default: %(default)s)",
    )
    parser.add_argument(
        "--restore-runner",
        type=Path,
        help=(
            "runner for restored VMs: the --runner machine without a kernel to boot "
            "(required with --boot snapshot)"
        ),
    )
    parser.add_argument(
        "--guest-mem-mib",
        type=int,
        help="guest RAM size the runner passes to -m (required with --boot snapshot)",
    )
    parser.add_argument(
        "--negative-control-vm",
        type=int,
        metavar="N",
        help=(
            "negative control: withhold node N's health forward and require the run "
            "to be rejected for that node (N is 1-based, as every VM number here; "
            "default: off, every node is checked)"
        ),
    )
    args = parser.parse_args(argv)

    if not 2 <= args.count <= MAX_VM_COUNT:
        parser.error(f"--count must be between 2 and {MAX_VM_COUNT}")
    if args.negative_control_vm is not None and not 1 <= args.negative_control_vm <= args.count:
        parser.error(
            "--negative-control-vm must name a node of this fleet: 1-based, as every "
            f"VM number here, so between 1 and --count ({args.count})"
        )
    if args.startup_deadline <= 0:
        parser.error("--startup-deadline must be greater than zero")
    binary_relative_path = PurePosixPath(args.binary_relative_path)
    if (
        binary_relative_path.is_absolute()
        or not binary_relative_path.parts
        or any(part in {".", ".."} for part in binary_relative_path.parts)
    ):
        parser.error("--binary-relative-path must be a normalized relative guest path")
    args.binary_relative_path = binary_relative_path
    args.expect_stdout = args.expect_stdout.replace("\\n", "\n")
    args.ready_probe_command = f"{args.ready_probe_binary} {READY_PROBE_FILE}"

    try:
        args.runner = args.runner.resolve(strict=True)
    except OSError as error:
        parser.error(f"--runner does not resolve to an existing path: {error}")
    if not args.runner.is_file() or not os.access(args.runner, os.X_OK):
        parser.error(f"--runner must be an executable file: {args.runner}")

    if args.boot == "snapshot":
        if args.restore_runner is None:
            parser.error("--boot snapshot requires --restore-runner")
        try:
            args.restore_runner = args.restore_runner.resolve(strict=True)
        except OSError as error:
            parser.error(f"--restore-runner does not resolve to an existing path: {error}")
        if not args.restore_runner.is_file() or not os.access(args.restore_runner, os.X_OK):
            parser.error(f"--restore-runner must be an executable file: {args.restore_runner}")
        if args.guest_mem_mib is None or args.guest_mem_mib <= 0:
            parser.error("--boot snapshot requires a positive --guest-mem-mib")

    try:
        args.ssh_key = args.ssh_key.resolve(strict=True)
    except OSError as error:
        parser.error(f"--ssh-key does not resolve to an existing path: {error}")
    if not args.ssh_key.is_file():
        parser.error(f"--ssh-key must be a file: {args.ssh_key}")

    try:
        args.store_path = args.store_path.resolve(strict=True)
    except OSError as error:
        parser.error(f"--store-path does not resolve to an existing path: {error}")
    if args.store_path.parent != Path("/nix/store"):
        parser.error(
            "--store-path must be a direct child of /nix/store, not a symlink or subpath"
        )

    tmpdir_value = os.environ.get("TMPDIR")
    if not tmpdir_value:
        parser.error("TMPDIR must name the caller-provided benchmark scratch directory")
    try:
        args.tmpdir = Path(tmpdir_value).resolve(strict=True)
    except OSError as error:
        parser.error(f"TMPDIR does not resolve to an existing directory: {error}")
    if not args.tmpdir.is_dir():
        parser.error(f"TMPDIR is not a directory: {args.tmpdir}")

    return args


def reserve_ports(count: int) -> list[PortReservation]:
    reservations: list[PortReservation] = []
    try:
        for index in range(1, count + 1):
            ssh_socket = reserve_port(SSH_PORT_BASE + index, "SSH")
            try:
                http_socket = reserve_port(HTTP_PORT_BASE + index, "HTTP")
            except BaseException:
                ssh_socket.close()
                raise
            reservations.append(PortReservation(ssh_socket, http_socket))
    except BaseException:
        for reservation in reservations:
            reservation.close()
        raise
    return reservations


def reserve_port(port: int, purpose: str) -> socket.socket:
    reservation = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # QEMU health checks can leave this listener port in TIME_WAIT.
        reservation.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        reservation.bind(("127.0.0.1", port))
        reservation.listen(1)
    except OSError as error:
        reservation.close()
        raise HarnessError(
            f"{purpose} host port 127.0.0.1:{port} is unavailable; "
            "stop the prior fanout benchmark or select a host without a collision: "
            f"{error}"
        ) from error
    return reservation


def launch_vms(
    runner: Path,
    run_dir: Path,
    count: int,
    vms: list[VmProcess],
    snapshot: Snapshot | None = None,
    extra_args: Sequence[str] = (),
    scratch: Path | None = None,
    # The node whose health forward this launch withholds, for the readiness
    # negative control. None is every node forwarded, which is what every run
    # that is not a control gets.
    withheld_health: int | None = None,
) -> None:
    # A restore waits for its state via QMP (-incoming defer). Every restore
    # shares the captured disk; -snapshot sends this VM's writes to a
    # temporary overlay in its TMPDIR (the workdir), so the image stays pristine.
    #
    # -S holds every launch at reset, and the per-VM address reaches QEMU in
    # this VM's environment rather than on the shared argv: the runner
    # substitutes FANOUT_NIC_MAC into the NIC's own -device line (see
    # default.nix), because a -device is realized while QEMU builds the
    # machine and a realized device's properties are read-only, so the address
    # cannot be written afterwards over QMP. One argv can therefore carry 64
    # launches only because the value that has to differ is per-child.
    argv = [str(runner), "-S", *extra_args]
    if snapshot is not None:
        argv += ["-snapshot", "-incoming", "defer"]
        argv += guest_ram_args(snapshot.ram, snapshot.ram_mib, share=False)
    for index in range(1, count + 1):
        # The image's address is VM 1's and the argv is shared, so the value
        # that differs per VM travels in this child's environment: the capture
        # guest is VM 1 of its own run, and the fleet restores 64 copies of it
        # that must not answer to one address.
        workdir = run_dir / f"vm{index}"
        workdir.mkdir(mode=0o700)
        qmp_socket = workdir / QMP_SOCKET_NAME
        if len(os.fsencode(qmp_socket)) > MAX_QMP_SOCKET_PATH_BYTES:
            raise HarnessError(
                f"QMP socket path is too long ({qmp_socket}); set TMPDIR to a shorter "
                "caller-owned path"
            )

        if snapshot is not None:
            try:
                (workdir / OVERLAY_IMAGE_NAME).symlink_to(snapshot.overlay)
            except OSError as error:
                raise HarnessError(f"VM {index}: cannot link snapshot disk: {error}") from error

        log_path = workdir / "runner.log"
        try:
            log = log_path.open("wb")
        except OSError as error:
            raise HarnessError(f"VM {index}: cannot open log {log_path}: {error}") from error

        # QEMU puts its -snapshot overlays (this VM's disk writes) in TMPDIR.
        vm_tmpdir = workdir
        if scratch is not None:
            vm_tmpdir = scratch / f"vm{index}"
            vm_tmpdir.mkdir(mode=0o700)
        child_env = os.environ.copy()
        child_env["TMPDIR"] = str(vm_tmpdir)
        child_env[QMP_NIC_MAC_ENV] = guest_mac(index)
        try:
            process = subprocess.Popen(
                argv,
                cwd=workdir,
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as error:
            log.close()
            raise HarnessError(f"VM {index}: failed to start {runner}: {error}") from error

        vms.append(
            VmProcess(
                index=index,
                workdir=workdir,
                qmp_socket=qmp_socket,
                ssh_port=SSH_PORT_BASE + index,
                http_port=HTTP_PORT_BASE + index,
                process=process,
                process_group=process.pid,
                log=log,
                health_forwarded=index != withheld_health,
            )
        )

        if process.poll() is not None:
            raise HarnessError(
                f"VM {index}: runner exited immediately with status {process.returncode}; "
                f"log tail:\n{read_log_tail(log_path)}"
            )


def connect_qmp(vm: VmProcess, deadline: float) -> socket.socket:
    # QEMU creates the QMP socket partway through its own startup, so every VM
    # spends the first stretch of its bring-up retrying a path that does not
    # exist yet. A flat 20 ms step meant each of the 64 VMs woke about 50 times
    # a second for that, on a host that is already oversubscribed, and it also
    # cost up to 20 ms of latency per VM at the moment the socket appeared.
    # Backing off from a millisecond finds it sooner for a fast starter and
    # costs a slow starter a fraction of the wakeups.
    last_error = "socket has not appeared"
    delay = QMP_POLL_INITIAL_S
    while True:
        if vm.process.poll() is not None:
            raise HarnessError(
                f"VM {vm.index}: runner exited with status {vm.process.returncode} "
                f"before QMP was ready; log tail:\n{read_log_tail(vm.workdir / 'runner.log')}"
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HarnessError(
                f"VM {vm.index}: QMP socket {vm.qmp_socket} was not ready before the "
                f"startup deadline ({last_error}); log tail:\n"
                f"{read_log_tail(vm.workdir / 'runner.log')}"
            )

        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(min(0.2, remaining))
        try:
            connection.connect(str(vm.qmp_socket))
            return connection
        except OSError as error:
            connection.close()
            time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
            delay = min(delay * 2, QMP_POLL_MAX_S)


def bring_up_vm(
    vm: VmProcess,
    reservation: PortReservation,
    deadline: float,
    snapshot: Snapshot | None,
) -> None:
    connection = connect_qmp(vm, deadline)
    try:
        qmp = QmpConnection(connection, vm.index)
        qmp.negotiate(deadline)

        # Keep the ports reserved until QEMU is ready to bind them. QMP errors
        # remain authoritative if another process wins the small close/bind race.
        reservation.close()
        commands = [f"hostfwd_add net0 tcp:127.0.0.1:{vm.ssh_port}-:{GUEST_SSH_PORT}"]
        if vm.health_forwarded:
            # A withheld health forward is a missing route to the guest's
            # /health, which is what the negative control denies one node: the
            # readiness check polls that node's port exactly as it polls every
            # other node's and finds nothing listening, so the failure is the
            # one a guest that never serves health produces, made without
            # anything in the readiness path being told which node it is.
            commands.append(
                f"hostfwd_add net0 tcp:127.0.0.1:{vm.http_port}-:{GUEST_HEALTH_PORT}"
            )
        for command in commands:
            result = qmp.execute(
                "human-monitor-command",
                {"command-line": command},
                deadline,
            )
            if result != "":
                raise HarnessError(
                    f"VM {vm.index}: QMP command {command!r} returned an error: {result!r}"
                )

        if snapshot is not None:
            # The capture was taken paused, so the loaded VM stays paused until cont.
            qmp.execute("migrate-set-capabilities", SNAPSHOT_CAPABILITIES, deadline)
            qmp.execute("migrate-incoming", {"uri": f"file:{snapshot.state}"}, deadline)
            wait_for_migration(qmp, vm.index, deadline)

        # The address this launch asked for, checked against the device that
        # holds it. The launch names it in the child's environment and the
        # runner substitutes it into the NIC's -device line as QEMU builds the
        # machine, so there is nothing to write here: a -device is realized
        # before the monitor is reachable and a realized device's properties
        # are read-only, which is why the value travels in the environment at
        # all. The read is after the restore and after cont, so it reports the
        # running device rather than what this launch asked for.
        nic_path = find_nic_qom_path(qmp, vm.index, deadline)
        qmp.execute("cont", None, deadline)

        # ESTABLISHED, and enforced here: the address this launch assigned is
        # the address QEMU holds for this VM's NIC. The launch's own value is
        # the only source of that address, so a device holding a different one
        # was never given it, and no guest-side reading can be told apart from
        # that: the same fleet-wide result follows from a device given the
        # wrong address and from a guest that read the right one wrongly.
        #
        # The guest's own view of the device is made to be this address, by
        # reprobe_fleet_nics before any node is asked what it is: the kernel
        # reads a NIC address when the driver probes the device and keeps it in
        # dev_addr from then on, and a restore carries the capture-time state
        # with it, so an unprobed guest names the address the capture booted
        # with. Only a re-probe reaches the device's config space, so that is
        # what the guest-side check rests on.
        vm.device_address = qmp.execute(
            "qom-get",
            {"path": nic_path, "property": QMP_NIC_MAC_PROPERTY},
            deadline,
        )
        assigned = guest_mac(vm.index)
        if vm.device_address != assigned:
            raise HarnessError(
                f"VM {vm.index}: QEMU holds address {vm.device_address!r} for this VM's "
                f"NIC, but this launch assigned it {assigned!r}"
            )
    finally:
        connection.close()



def qom_child_type(entry: Any) -> str | None:
    """The device type a qom-list entry names, or None if it names a property.

    A child is reported as child<TYPE> and a property as that property's own
    type, so the type alone tells the two apart. A name that mentions
    virtio-net is therefore not evidence of anything on its own: only a child
    can be a device, and only a device can hold a NIC address.
    """
    if not isinstance(entry, dict) or not entry.get("name"):
        return None
    reported = str(entry.get("type", ""))
    if not reported.startswith(QMP_CHILD_TYPE_PREFIX) or not reported.endswith(">"):
        return None
    return reported[len(QMP_CHILD_TYPE_PREFIX) : -1]


def find_nic_qom_path(qmp: QmpConnection, vm_index: int, deadline: float) -> str:
    """The QOM path of this VM's NIC, resolved from the machine as it stands.

    qom-get addresses a property by path and the runner does not name the NIC,
    so the peripheral is identified by device type rather than by an id this
    harness would have to guess. The runner's -device carries no id=, so the
    device is anonymous: QEMU parents it under one of the machine's own child
    containers and names it device[N] for the index it took there. Which
    container, and which index, belong to the launch and not to this harness,
    so both are read from the live tree on every call and nothing is carried
    across calls: a path remembered from one launch, or from one VM, is not a
    path of the next.

    Anything other than exactly one match is refused rather than resolved: a
    machine with no NIC has no address to report, and one with two has no
    single answer.
    """
    searched = [
        f"{QMP_MACHINE_PATH}/{entry['name']}"
        for entry in qmp.execute("qom-list", {"path": QMP_MACHINE_PATH}, deadline)
        if qom_child_type(entry) == QMP_CONTAINER_TYPE
    ]
    paths = [
        f"{container}/{entry['name']}"
        for container in searched
        for entry in qmp.execute("qom-list", {"path": container}, deadline)
        if (device_type := qom_child_type(entry)) is not None
        and QMP_NIC_TYPE in device_type
    ]
    if len(paths) != 1:
        raise HarnessError(
            f"VM {vm_index}: expected one virtio-net NIC under the machine's "
            f"child containers ({', '.join(searched)}), found {len(paths)}"
        )
    return paths[0]


def start_all_vms(
    vms: Sequence[VmProcess],
    reservations: Sequence[PortReservation],
    ssh_key: Path,
    deadline: float,
    snapshot: Snapshot | None,
    probe_command: str,
) -> float:
    """Bring every VM up and wait until it serves SSH and HTTP.

    Each VM polls readiness as soon as its own bring-up (and restore) ends, so
    one slow QEMU start does not hold back the rest of the fleet. Returns the
    latest bring-up completion, in seconds since the call.
    """
    started = time.monotonic()
    bring_up_done: list[float] = []

    def start(vm: VmProcess, reservation: PortReservation) -> None:
        bring_up_vm(vm, reservation, deadline, snapshot)
        bring_up_done.append(time.monotonic() - started)
        wait_for_vm_ready(vm, ssh_key, deadline, probe_command)

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(vms)) as executor:
        futures = [
            executor.submit(start, vm, reservation)
            for vm, reservation in zip(vms, reservations, strict=True)
        ]
        collect_parallel_failures("fleet startup", futures)
    return max(bring_up_done)


def wait_for_migration(qmp: QmpConnection, vm_index: int, deadline: float) -> None:
    """Block until the migration finishes, by polling query-migrate.

    QEMU's MIGRATION event would be the cheaper way to wait, and replacing this
    poll with it was tried and reverted. Tracing the raw QMP socket on both
    directions showed QEMU 11.1.1 delivering no MIGRATION event at all here: on
    an outgoing `migrate` the connection received a STOP event and then nothing
    while query-migrate reported completed, and on a restore the harness was
    served a query-migrate reply. So there was no event to wait on, only a
    deadline to block out.
    """
    while True:
        status = qmp.execute("query-migrate", None, deadline).get("status")
        if status == "completed":
            return
        if status in ("failed", "cancelled"):
            raise HarnessError(f"VM {vm_index}: snapshot migration {status}")
        time.sleep(MIGRATION_POLL_S)


def snapshot_dir(
    cache: Path, runner: Path, ram_mib: int, probe_command: str
) -> Path:
    # The runner is a Nix store path, so it pins the guest closure, kernel,
    # QEMU binary, and device model that the captured state depends on. The
    # harness's own capture parameters pin the rest, and they have to be in
    # the key: guest_ram_args tells QEMU to map ram_mib against the cached
    # file, and the capture guest's readiness probe writes READY_PROBE_FILE
    # into the golden overlay that every restore then shares. A cache hit
    # under different values restores a state that was never captured.
    #
    # The probe command is in the key for the same reason: it names a guest
    # store path, so swapping it for a different binary changes what the
    # capture guest ran against that shared overlay.
    material = "\0".join(
        (
            SNAPSHOT_FORMAT,
            str(runner),
            str(ram_mib),
            READY_PROBE_FILE,
            probe_command,
            GUEST_SNAPSHOT_PREP,
        )
    )
    key = hashlib.sha256(material.encode()).hexdigest()[:32]
    return cache / key


def touch_snapshot(directory: Path) -> None:
    """Mark a snapshot as in use now, for prune_stale_snapshots."""
    try:
        directory.touch()
    except OSError:
        pass


def prune_stale_snapshots(cache: Path, retention_s: float = SNAPSHOT_RETENTION_S) -> None:
    """Remove cached snapshots not used within retention_s.

    A snapshot is ~565 MB and every guest change produces a new runner, so the
    cache grows without bound. Only whole key directories are considered, and
    only those whose mtime is older than the window, so a directory another
    launcher is using is left alone: a hit refreshes its mtime.
    """
    cutoff = time.time() - retention_s
    try:
        entries = list(cache.iterdir())
    except OSError:
        return
    for entry in entries:
        if not entry.is_dir() or entry.name.startswith("capture-"):
            continue
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(entry)
        except OSError:
            continue


def ensure_snapshot(
    runner: Path,
    ssh_key: Path,
    cache: Path,
    ram_mib: int,
    probe_command: str,
) -> tuple[Snapshot, float | None]:
    """Return the cached snapshot for runner, capturing it first on a miss.

    Capture is a per-image artifact, like building the runner: it happens
    before the fleet's readiness clock starts and is reported separately.
    Returns the capture duration, or None on a cache hit.
    """
    directory = snapshot_dir(cache, runner, ram_mib, probe_command)
    snapshot = Snapshot(
        ram=directory / "ram",
        ram_mib=ram_mib,
        state=directory / "state",
        overlay=directory / OVERLAY_IMAGE_NAME,
    )
    if snapshot_complete(snapshot):
        # mtime is the only signal prune_stale_snapshots has that a directory
        # is still in use, and reading a directory does not update it.
        touch_snapshot(directory)
        # Prune here as well as after a capture. Calling it only on the capture
        # path means it never runs in steady state, where every run is a hit:
        # measured, a nine-day-old entry survived a run that used the cache.
        prune_stale_snapshots(cache)
        return snapshot, None

    started = time.monotonic()
    try:
        cache.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="capture-", dir=cache))
    except OSError as error:
        raise HarnessError(f"cannot create snapshot staging under {cache}: {error}") from error
    try:
        capture_snapshot(runner, ssh_key, staging, ram_mib, probe_command)
        try:
            (staging / "vm1" / OVERLAY_IMAGE_NAME).rename(staging / OVERLAY_IMAGE_NAME)
            shutil.rmtree(staging / "vm1")
            staging.rename(directory)
        except OSError as error:
            # A concurrent launcher may have published the same snapshot first.
            if not snapshot_complete(snapshot):
                raise HarnessError(f"cannot publish snapshot {directory}: {error}") from error
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    touch_snapshot(directory)
    prune_stale_snapshots(cache)
    return snapshot, time.monotonic() - started


def snapshot_complete(snapshot: Snapshot) -> bool:
    return all(path.is_file() for path in (snapshot.ram, snapshot.state, snapshot.overlay))


def capture_snapshot(
    runner: Path,
    ssh_key: Path,
    staging: Path,
    ram_mib: int,
    probe_command: str,
) -> None:
    reservations = reserve_ports(1)
    vms: list[VmProcess] = []
    try:
        deadline = time.monotonic() + SNAPSHOT_CAPTURE_TIMEOUT_S
        ram_args = guest_ram_args(staging / "ram", ram_mib, share=True)
        launch_vms(runner, staging, 1, vms, extra_args=ram_args)
        start_all_vms(vms, reservations, ssh_key, deadline, None, probe_command)
        run_checked(
            "snapshot guest preparation",
            host_ssh_command(ssh_key, vms[0].ssh_port, GUEST_SNAPSHOT_PREP),
            timeout=VERIFY_TIMEOUT_S,
        )
        connection = connect_qmp(vms[0], deadline)
        try:
            qmp = QmpConnection(connection, vms[0].index)
            qmp.negotiate(deadline)
            qmp.execute("stop", None, deadline)
            qmp.execute("migrate-set-capabilities", SNAPSHOT_CAPABILITIES, deadline)
            qmp.execute("migrate", {"uri": f"file:{staging / 'state'}"}, deadline)
            wait_for_migration(qmp, vms[0].index, deadline)
        finally:
            connection.close()
    finally:
        for reservation in reservations:
            reservation.close()
        cleanup_errors = terminate_vms(vms)
    if cleanup_errors:
        raise HarnessError("snapshot capture cleanup failed: " + "; ".join(cleanup_errors))


@functools.cache
def host_ssh_binary() -> str:
    # An absolute path lets subprocess take its posix_spawn path (see the
    # readiness probe); a bare name makes it fork() and search PATH.
    path = shutil.which("ssh")
    if path is None:
        raise HarnessError("ssh is not on PATH")
    return path


def host_ssh_command(
    ssh_key: Path,
    port: int,
    remote_command: str,
    connect_timeout_s: int = 1,
) -> list[str]:
    return [
        host_ssh_binary(),
        "-F",
        "/dev/null",
        "-i",
        str(ssh_key),
        "-p",
        str(port),
        "-oBatchMode=yes",
        f"-oConnectTimeout={connect_timeout_s}",
        "-oConnectionAttempts=1",
        "-oIdentitiesOnly=yes",
        "-oStrictHostKeyChecking=no",
        "-oUserKnownHostsFile=/dev/null",
        "-oLogLevel=ERROR",
        "root@127.0.0.1",
        remote_command,
    ]


def wait_for_vm_ready(
    vm: VmProcess,
    ssh_key: Path,
    deadline: float,
    probe_command: str,
) -> None:
    ssh_ready = False
    http_ready = False
    last_ssh_result = "not attempted"
    last_http_result = "not attempted"

    while not (ssh_ready and http_ready):
        if vm.process.poll() is not None:
            raise HarnessError(
                f"VM {vm.index}: runner exited with status {vm.process.returncode} while "
                f"waiting for readiness; log tail:\n"
                f"{read_log_tail(vm.workdir / 'runner.log')}"
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HarnessError(
                f"VM {vm.index}: readiness deadline expired; "
                f"last SSH result: {last_ssh_result}; last HTTP result: {last_http_result}"
            )

        if not ssh_ready:
            # SSH ping plus a data exchange: send a fresh nonce on stdin, have
            # the guest write it to a file and read that file back. The VM is
            # ready only if the exact bytes return, so a login that cannot
            # move data or touch its filesystem does not count.
            nonce = f"fanout-vm{vm.index}-{secrets.token_hex(16)}\n"
            try:
                result = subprocess.run(
                    host_ssh_command(
                        ssh_key,
                        vm.ssh_port,
                        probe_command,
                        READY_PROBE_CONNECT_TIMEOUT_S,
                    ),
                    check=False,
                    input=nonce,
                    capture_output=True,
                    text=True,
                    timeout=min(READY_PROBE_TIMEOUT_S, remaining),
                    # Probes run while other VMs are still in bring-up. A
                    # fork()ed child holds copies of every open fd until it
                    # execs, including port reservations those VMs are about
                    # to hand to QEMU, whose hostfwd bind then fails. With an
                    # absolute executable and close_fds=False, subprocess uses
                    # posix_spawn (macOS has no closefrom spawn action): no
                    # forked copy, and ~3x cheaper for 64 concurrent probes.
                    # The launcher's fds are non-inheritable (PEP 446).
                    close_fds=False,
                )
            except subprocess.TimeoutExpired:
                last_ssh_result = "check timed out"
            except OSError as error:
                last_ssh_result = str(error)
            else:
                if result.returncode != 0:
                    last_ssh_result = summarize_output(result.stdout, result.stderr)
                elif result.stdout != nonce:
                    last_ssh_result = (
                        f"data exchange mismatch: sent {nonce!r}, read back {result.stdout!r}"
                    )
                else:
                    ssh_ready = True
                    vm.probe_nonce = nonce
                    # What this leg last said, kept on the VM so the report of a
                    # run that stops here can be read without the run.
                    last_ssh_result = SSH_EXCHANGE_OK

        remaining = deadline - time.monotonic()
        if not http_ready and remaining > 0:
            http_ready, last_http_result = check_http_health(vm.http_port, remaining)

        vm.last_ssh_result = last_ssh_result
        vm.last_http_result = last_http_result

        if not (ssh_ready and http_ready):
            time.sleep(min(0.03, max(0.0, deadline - time.monotonic())))

    vm.ready = True


def check_http_health(port: int, remaining: float) -> tuple[bool, str]:
    connection = http.client.HTTPConnection(
        "127.0.0.1",
        port,
        timeout=max(0.01, min(READY_PROBE_TIMEOUT_S, remaining)),
    )
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        body = response.read(7)
    except (OSError, TimeoutError, http.client.HTTPException) as error:
        return False, str(error)
    finally:
        connection.close()

    if response.status != 200:
        return False, f"HTTP {response.status}, body={body!r}"
    if body != HEALTH_BODY:
        return False, f"expected {HEALTH_BODY!r}, got {body!r}"
    return True, HEALTH_OK


def collect_parallel_failures(
    phase: str,
    futures: Sequence[concurrent.futures.Future[Any]],
) -> None:
    failures: list[str] = []
    for future in concurrent.futures.as_completed(futures):
        try:
            future.result()
        except Exception as error:
            failures.append(str(error))
    if failures:
        failures.sort()
        shown = failures[:8]
        detail = "\n".join(f"  - {failure}" for failure in shown)
        omitted = len(failures) - len(shown)
        if omitted:
            detail += f"\n  - ... {omitted} additional failure(s) omitted"
        raise HarnessError(f"{phase} failed:\n{detail}")


def write_inventory(path: Path, count: int) -> str:
    lines = [f'seed = "root@10.0.2.2:{SSH_PORT_BASE + 1}"', "nodes = ["]
    lines.extend(
        f'  "root@10.0.2.2:{SSH_PORT_BASE + index}",'
        for index in range(2, count + 1)
    )
    lines.append("]")
    content = "\n".join(lines) + "\n"
    try:
        path.write_text(content, encoding="utf-8")
    except OSError as error:
        raise HarnessError(f"failed to write inventory {path}: {error}") from error
    return content


def cascade_command(store_path: Path, inventory: str, *, fanout: int) -> str:
    """The guest-side command that distributes one closure across the fleet.

    `--format jsonl` is what makes this checkable at all: cascade-copy runs
    with a non-TTY stdout over SSH, where it otherwise wires a NullSink and
    emits nothing, leaving only its exit status as evidence.
    """
    return shlex.join(
        [
            "cascade-copy",
            str(store_path),
            "--inventory",
            inventory,
            "--strategy",
            "log2-fanout",
            "--fanout",
            str(fanout),
            "--no-watch",
            "--format",
            "jsonl",
        ]
    )


def verify_cascade_relay(
    stream: str, *, count: int, fanout: int, verifier: str = "cast"
) -> dict[str, int]:
    """Assert the cascade really relayed, and describe the tree it built.

    `cast cascade verify` owns the stream grammar, the relay assertion, and
    the round rule; this hands it the run's whole event stream and translates
    its verdict. The CLI's exit status cannot tell a peer-to-peer cascade from
    a host that pushed to every guest in turn — both exit 0 — so the verdict
    comes from the verifier, and every way the verification can fail (binary
    absent, non-zero exit, nonsense output) fails the run with the cause
    named rather than reading as "relay fine".
    """
    with tempfile.NamedTemporaryFile(
        "w", suffix=".jsonl", prefix="cascade-trace-"
    ) as trace:
        trace.write(stream)
        trace.flush()
        try:
            completed = subprocess.run(
                [
                    verifier,
                    "cascade",
                    "verify",
                    trace.name,
                    "--fanout",
                    str(fanout),
                    "--nodes",
                    str(count),
                ],
                capture_output=True,
                text=True,
                timeout=VERIFY_TIMEOUT_S,
            )
        except FileNotFoundError as error:
            raise HarnessError(
                f"cascade verifier {verifier!r} is not on PATH, so the relay "
                "check cannot run"
            ) from error
        except subprocess.TimeoutExpired as error:
            raise HarnessError(
                f"cascade verifier {verifier!r} did not finish within "
                f"{VERIFY_TIMEOUT_S:.0f}s"
            ) from error
    if completed.returncode != 0:
        diagnostic = (
            completed.stderr.strip()
            or completed.stdout.strip()
            or "no diagnostic printed"
        )
        raise HarnessError(
            f"cascade verifier failed (exit {completed.returncode}): {diagnostic}"
        )
    try:
        summary = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise HarnessError(
            "cascade verifier exited 0 but printed unparseable output "
            f"({error.msg}): {completed.stdout.strip()[:200]!r}"
        ) from error
    for field in ("nodes", "rounds", "relay_depth", "relayed_nodes"):
        value = summary.get(field)
        if not isinstance(value, int) or isinstance(value, bool):
            raise HarnessError(
                f"cascade verifier reported {field} as {value!r}, not an integer"
            )
    return {
        "nodes": summary["nodes"],
        "rounds": summary["rounds"],
        "relay_depth": summary["relay_depth"],
        "relayed_nodes": summary["relayed_nodes"],
    }


def write_cascade_evidence(evidence_dir: Path | None, stream: str) -> None:
    """Record a cascade's event stream where it survives cleanup."""
    if evidence_dir is None:
        return
    try:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (evidence_dir / "cascade-events.jsonl").write_text(stream, encoding="utf-8")
    except OSError as error:
        raise HarnessError(f"cannot record the cascade event stream: {error}") from error

def run_cascade(
    ssh_key: Path,
    store_path: Path,
    inventory_content: str,
    *,
    count: int,
    fanout: int,
    evidence_dir: Path | None = None,
) -> dict[str, int]:
    """Distribute one closure across the fleet and prove the relay was used.

    Returns the topology summary. Raises if the cascade did not converge over
    the whole fleet, or converged without ever relaying - which is the case a
    green exit status cannot distinguish from a real fan-out.
    """
    run_checked(
        f"write cascade inventory to {INVENTORY_GUEST_PATH}",
        host_ssh_command(
            ssh_key,
            SSH_PORT_BASE + 1,
            f"umask 077; cat > {shlex.quote(INVENTORY_GUEST_PATH)}",
        ),
        timeout=VERIFY_TIMEOUT_S,
        input_text=inventory_content,
    )
    # The stream is evidence, and a cascade that fails is exactly when it is
    # needed, so take it whichever way the command ends. `run_checked` truncates
    # a failing command's output in the error it raises, and it raises *before*
    # returning, so a post-hoc write recorded nothing when it mattered most.
    def record(result: subprocess.CompletedProcess[str]) -> None:
        # Runs inside run_checked, before it raises. Recording after the call
        # would be dead code: a non-zero exit never returns.
        write_cascade_evidence(evidence_dir, result.stdout)
    completed = run_checked(
        "guest cascade-copy",
        host_ssh_command(
            ssh_key,
            SSH_PORT_BASE + 1,
            cascade_command(store_path, INVENTORY_GUEST_PATH, fanout=fanout),
        ),
        timeout=CASCADE_TIMEOUT_S,
        on_failure=record,
    )
    write_cascade_evidence(evidence_dir, completed.stdout)
    return verify_cascade_relay(completed.stdout, count=count, fanout=fanout)


def assert_probe_file_is_durable(vm: VmProcess, ssh_key: Path) -> None:
    """Fail the run if the readiness sync can no-op on a RAM filesystem.

    Checked once per run, on one VM and off the readiness clock: the probe's
    value is that the write reached the block device, and a tmpfs probe path
    would satisfy every other check while quietly proving nothing.
    """
    probe_directory = str(PurePosixPath(READY_PROBE_FILE).parent)
    result = run_checked(
        f"VM {vm.index} probe filesystem",
        host_ssh_command(
            ssh_key,
            vm.ssh_port,
            f"stat -f -c %T {shlex.quote(probe_directory)}",
        ),
        timeout=VERIFY_TIMEOUT_S,
    )
    filesystem = result.stdout.strip()
    if not filesystem or "tmpfs" in filesystem:
        raise HarnessError(
            f"VM {vm.index}: readiness probe file {READY_PROBE_FILE} is on "
            f"{filesystem or 'an unknown'} filesystem; sync would not reach the "
 "block device, so the data exchange proves nothing"
        )


def assert_fleet_state_is_independent(vms: Sequence[VmProcess], ssh_key: Path) -> None:
    """Every VM must still hold exactly its own readiness nonce.

    The 64 restores share one copy-on-write RAM image and one read-only
    golden overlay, so every speedup in the restore path rests on VM i's
    writes being invisible to VM j. Nothing else in the run would notice if
    that stopped being true: each VM's own round trip still succeeds. Re-read
    the probe file on every VM and require the unique bytes that VM wrote.
    """
    def check(vm: VmProcess) -> None:
        if vm.probe_nonce is None:
            raise HarnessError(f"VM {vm.index}: no readiness nonce was recorded")
        result = run_checked(
            f"VM {vm.index} probe file re-read",
            host_ssh_command(ssh_key, vm.ssh_port, f"cat {READY_PROBE_FILE}"),
            timeout=VERIFY_TIMEOUT_S,
        )
        if result.stdout != vm.probe_nonce:
            raise HarnessError(
                f"VM {vm.index}: restored guests are not independent; the readiness "
                f"probe file holds {result.stdout!r} but this VM wrote "
                f"{vm.probe_nonce!r}"
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(vms)) as executor:
        futures = [executor.submit(check, vm) for vm in vms]
        collect_parallel_failures("fleet state independence", futures)


def assert_fleet_entropy_is_independent(
    vms: Sequence[VmProcess], ssh_key: Path, ready_probe_binary: str
) -> None:
    """Two restored VMs must not draw from one captured RNG stream.

    Every restore maps the same captured RAM image copy-on-write, so a guest
    whose kernel RNG came from that image rather than from the host would hand
    every node the same bytes. virtio-rng pulls from the host, so restored
    nodes should differ - but the write-isolation canary cannot see this,
    because an identical entropy stream is not a cross-VM write leak. Assert it
    instead of assuming it, because a fleet test that generates keys or tokens
    per node would silently be drawing from shared state.

    Two nodes is enough to catch a replayed stream and keeps the cost at two
    SSH round trips out of 64.
    """
    if len(vms) < 2:
        raise HarnessError("entropy independence needs at least two VMs")

    def draw(vm: VmProcess) -> bytes:
        result = run_checked(
            f"VM {vm.index} entropy draw",
            host_ssh_command(
                ssh_key, vm.ssh_port, f"{ready_probe_binary} --entropy {ENTROPY_BYTES}"
            ),
            timeout=VERIFY_TIMEOUT_S,
        )
        return result.stdout

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        first, second = executor.map(draw, (vms[0], vms[1]))
    for index, draw in ((1, first), (2, second)):
        if len(draw) != ENTROPY_HEX_CHARS:
            raise HarnessError(
                f"VM {index}: entropy draw was {len(draw)} hex characters, "
                f"expected {ENTROPY_HEX_CHARS} ({ENTROPY_BYTES} bytes)"
            )
        if any(character not in "0123456789abcdef" for character in draw):
            raise HarnessError(f"VM {index}: entropy draw is not hex: {draw!r}")
    if first == second:
        raise HarnessError(
            "restored guests share one RNG stream: VM 1 and VM 2 produced "
            f"identical {ENTROPY_BYTES}-byte draws, so entropy is coming from the "
            "captured RAM image rather than the host"
        )


def parse_reported_address(reported: str, vm_index: int) -> str:
    """Normalize one reported NIC address, or refuse it.

    sysfs prints lower-case colon-delimited hex, and that is the only shape
    accepted: a value that is not an address cannot be compared against the one
    this launch assigned, and passing it on would turn a broken read into a
    mismatch reported against the wrong thing.
    """
    address = reported.strip().lower()
    octets = address.split(":")
    if len(octets) != 6 or any(
        len(octet) != 2 or octet.strip("0123456789abcdef") for octet in octets
    ):
        raise HarnessError(
            f"VM {vm_index}: reported address {reported!r} is not a MAC address"
        )
    return address


def fetch_guest_address(vm: VmProcess, ssh_key: Path, probe_binary: str) -> str:
    """Ask this VM what address its own NIC currently has.

    The probe binary is the one the readiness exchange already runs, in a mode
    that reads the address out of sysfs per invocation, so this costs one SSH
    round trip and depends on nothing computed when the guest image was built.
    """
    result = run_checked(
        f"VM {vm.index} reported address",
        host_ssh_command(
            ssh_key, vm.ssh_port, f"{probe_binary} --address {GUEST_NIC_NAME}"
        ),
        timeout=VERIFY_TIMEOUT_S,
    )
    return parse_reported_address(result.stdout, vm.index)

def guest_nic_reprobe_script(vm: VmProcess, report_port: int) -> str:
    """The shell one guest runs to re-probe its NIC and say where it landed.

    The order is the whole design. The device's name is read while the netdev is
    still there to read it from, because afterwards the path it comes from is
    gone with the netdev, and sysfs addresses an unbind and a bind by device
    name rather than by driver name. The address is reported only once the
    guest holds one again, because the report is what the host aims a forward
    with.

    The report goes to slirp's gateway, which is the host as the guest sees it,
    and that is the only way back for a node that has moved. A forward added
    without a guest address resolves to 10.0.2.15 and stays there for the life
    of the run, while the re-probed guest is re-leased a different address,
    because to slirp's DHCP server a new MAC is a new client. So the node the
    host can no longer reach is exactly the node that has to report.
    """
    read_address = (
        f"IP=$(ip -4 -o addr show {GUEST_NIC_NAME} 2>/dev/null | "
        "awk '{print $4}' | cut -d/ -f1 | head -1); "
    )
    return (
        "( VDEV=$(basename $(readlink -f " + GUEST_NIC_DEVICE_PATH + ")); "
        f"echo $VDEV > {VIRTIO_NET_DRIVER_PATH}/unbind; "
        f"echo $VDEV > {VIRTIO_NET_DRIVER_PATH}/bind; "
        f"IP=; i=0; while [ $i -lt {int(NIC_REPROBE_TIMEOUT_S)} ]; do "
        + read_address
        + '[ -n "$IP" ] && break; i=$(( i + 1 )); sleep 1; done; '
        f'echo "{vm.index} $IP" | busybox nc -w 5 {GUEST_REPORT_GATEWAY} {report_port} '
        ") </dev/null >/dev/null 2>&1 &"
    )


def issue_guest_nic_reprobe(vm: VmProcess, ssh_key: Path, report_port: int) -> None:
    """Start this guest's NIC re-probe, without waiting for it to finish.

    The re-probe tears the node's netdev down, so the session that asks for it is
    expected to die with the link and its exit status says nothing about whether
    the re-probe happened. The work is detached for the same reason, and the
    guest's own report is what says whether it landed.
    """
    try:
        subprocess.run(
            host_ssh_command(
                ssh_key, vm.ssh_port, guest_nic_reprobe_script(vm, report_port)
            ),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=VERIFY_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        # Losing the link mid-request is the expected outcome, not a fault to
        # report: the guest's report is the only thing that says it worked.
        pass


def collect_nic_reprobe_reports(
    vms: Sequence[VmProcess], listener: socket.socket, deadline: float
) -> dict[int, str]:
    """Every guest's report of the address it holds after its re-probe.

    A report is "<node> <address>", and both halves are refused rather than
    guessed: an unknown node would leave a forward aimed at nothing, and an
    address that is not one would be aimed by a forward that cannot use it.
    """
    reports: dict[int, str] = {}
    while len(reports) < len(vms):
        if time.monotonic() >= deadline:
            missing = ", ".join(str(vm.index) for vm in vms if vm.index not in reports)
            raise HarnessError(
                f"these nodes did not report the address they re-probed onto: {missing}"
            )
        listener.settimeout(max(0.05, min(1.0, deadline - time.monotonic())))
        try:
            connection, _ = listener.accept()
        except socket.timeout:
            continue
        except OSError as error:
            raise HarnessError(f"the NIC re-probe report listener failed: {error}") from error
        with connection:
            connection.settimeout(5.0)
            chunks: list[bytes] = []
            try:
                while True:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
            except OSError:
                pass
        reported = b"".join(chunks).decode("utf-8", "replace").strip()
        node, _, address = reported.partition(" ")
        if node.strip() not in {str(vm.index) for vm in vms} or not address.strip():
            raise HarnessError(f"unreadable NIC re-probe report: {reported!r}")
        reports[int(node)] = address.strip()
    return reports


def repoint_vm_forwards(vm: VmProcess, guest_address: str) -> None:
    """Re-add this node's host forwards, aimed at the address it now holds.

    Both forwards, and both removed first: the SSH one because the identity read
    needs it, the health one because a forward still aimed at 10.0.2.15 would
    report this node as never serving health. A node whose health forward was
    withheld has no health forward to move.
    """
    deadline = time.monotonic() + NIC_REPROBE_TIMEOUT_S
    connection = connect_qmp(vm, deadline)
    try:
        qmp = QmpConnection(connection, vm.index)
        qmp.negotiate(deadline)
        forwards = [(vm.ssh_port, GUEST_SSH_PORT)]
        if vm.health_forwarded:
            forwards.append((vm.http_port, GUEST_HEALTH_PORT))
        for host_port, guest_port in forwards:
            # The two commands report success differently, and both are checked:
            # hostfwd_remove names the rule it removed, hostfwd_add says nothing
            # at all when it worked, which is the contract bring_up_vm already
            # relies on for the add.
            remove = f"hostfwd_remove net0 tcp:127.0.0.1:{host_port}"
            removed = qmp.execute(
                "human-monitor-command", {"command-line": remove}, deadline
            )
            if "removed" not in removed:
                raise HarnessError(
                    f"VM {vm.index}: QMP command {remove!r} did not remove the existing "
                    f"forward: {removed!r}"
                )
            add = f"hostfwd_add net0 tcp:127.0.0.1:{host_port}-{guest_address}:{guest_port}"
            added = qmp.execute(
                "human-monitor-command", {"command-line": add}, deadline
            )
            if added != "":
                raise HarnessError(
                    f"VM {vm.index}: QMP command {add!r} returned an error: {added!r}"
                )
    finally:
        connection.close()


def await_guest_ssh(vm: VmProcess, ssh_key: Path) -> None:
    """Block until this guest answers over its re-aimed SSH forward."""
    deadline = time.monotonic() + NIC_REPROBE_TIMEOUT_S
    while True:
        result = subprocess.run(
            host_ssh_command(ssh_key, vm.ssh_port, "true", connect_timeout_s=5),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=VERIFY_TIMEOUT_S,
            check=False,
        )
        if result.returncode == 0:
            return
        if time.monotonic() >= deadline:
            raise HarnessError(
                f"VM {vm.index}: the guest did not answer over its re-aimed SSH forward "
                f"within {NIC_REPROBE_TIMEOUT_S:g}s: {summarize_output(result.stdout, result.stderr)}"
            )
        time.sleep(NIC_REPROBE_POLL_S)


def reprobe_fleet_nics(vms: Sequence[VmProcess], ssh_key: Path) -> None:
    """Make every node re-probe its NIC, and follow each one to where it lands.

    All of them at once because each re-probe is a second of its own node's time
    and the budget is the whole fleet's: asked one at a time, 64 nodes pay 64
    seconds where in parallel they pay one. Each node is then re-aimed at the
    address it reported and waited for, because a node that cannot be reached is
    a node the run cannot ask anything else.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(max(2, len(vms)))
        report_port = listener.getsockname()[1]
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(vms)) as executor:
            futures = [
                executor.submit(issue_guest_nic_reprobe, vm, ssh_key, report_port)
                for vm in vms
            ]
            collect_parallel_failures("guest NIC re-probe", futures)
        reports = collect_nic_reprobe_reports(
            vms, listener, time.monotonic() + NIC_REPROBE_TIMEOUT_S
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(vms)) as executor:
            futures = [
                executor.submit(repoint_vm_forwards, vm, reports[vm.index]) for vm in vms
            ]
            collect_parallel_failures("guest NIC re-probe forwards", futures)
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(vms)) as executor:
            futures = [executor.submit(await_guest_ssh, vm, ssh_key) for vm in vms]
            collect_parallel_failures("guest NIC re-probe readiness", futures)
    finally:
        listener.close()


def assert_fleet_node_identity_is_per_launch(
    vms: Sequence[VmProcess], ssh_key: Path, probe_binary: str
) -> None:
    """Every node must report the address its own launch gave it.

    The address is what tells 64 restored guests apart, and nothing else in
    the run can see whether it arrived: each VM's readiness exchange passes
    with one shared address for the whole fleet, as does the cascade, which
    addresses nodes by their forwarded port. So each node reads the address
    out of its own device and has to name the one this launch assigned it.

    Two different things are measured here, and both are established by the
    time this runs. That the per-launch address reached the device is enforced
    in bring_up_vm, which reads it back from QEMU, and that the guest's own view
    of the device is that address is established by reprobe_fleet_nics, which
    makes each guest's driver read the device again: without it the kernel
    caches a probed NIC address in dev_addr and a restore carries the
    capture-time state with it, so every node would name the address the
    capture booted with. What is left checked rather than assumed is the
    reading itself, so every node's answer is recorded before any failure is
    raised, and node_identity_report publishes each answer beside the address
    QEMU holds for the device it read. A node naming a different address fails
    the run, because a fleet answering to one address is the outcome this check
    exists to catch, and it fails with the measurement that explains it rather
    than without.

    The host's view of the same device is quoted in the failure so the side
    the disagreement is on is named: a guest reading the capture-time address
    while QEMU holds this VM's own is a different problem from a device that
    was never given one, which bring_up_vm has already refused.
    """
    def check(vm: VmProcess) -> None:
        reported = fetch_guest_address(vm, ssh_key, probe_binary)
        vm.guest_address = reported
        expected = guest_mac(vm.index)
        if reported != expected:
            held = vm.device_address
            raise HarnessError(
                f"VM {vm.index}: the guest reports address {reported!r}, but this "
                f"launch assigned it {expected!r}"
                + (
                    ""
                    if held is None
                    else f"; QEMU holds {held!r} for this VM's NIC"
                )
                + ", so the guest is not using the address its device holds "
                "and the nodes cannot be told apart by address"
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(vms)) as executor:
        futures = [executor.submit(check, vm) for vm in vms]
        collect_parallel_failures("per-launch node identity", futures)


def node_identity_status(vm: VmProcess) -> str:
    """Which side of this node's address the disagreement is on.

    Named rather than counted, because the three outcomes need different
    responses and a single boolean would hide which one happened: a device
    holding another launch's address is an injection that did not arrive (a
    real defect, refused in bring_up_vm), a guest naming an address its
    device does not hold is the kernel keeping the address it probed or the
    restore carrying the capture's state (reported, not yet attributed), and
    a node that never answered was never asked successfully.
    """
    if vm.device_address != guest_mac(vm.index):
        return "device_mismatch"
    if vm.guest_address is None:
        return "unanswered"
    if vm.guest_address == vm.device_address:
        return "agrees_with_device"
    return "diverges_from_device"


def node_identity_report(vms: Sequence[VmProcess]) -> dict[str, Any]:
    """Both sides of every node's address, for a passing run and a failed one.

    The addresses are recorded on the VMs as the run reads them, so the same
    measurement reaches the result of a run that passed and the report of one
    that did not: what each node said its address was, what QEMU holds for
    the device it read, and which side the disagreement falls on. That is
    the measurement a guest-side divergence needs to be understood, and a run
    which stops on one would otherwise discard it with the result it never
    finished building.
    """
    return {
        "device_addresses": {str(vm.index): vm.device_address for vm in vms},
        "guest_addresses": {str(vm.index): vm.guest_address for vm in vms},
        "node_statuses": {str(vm.index): node_identity_status(vm) for vm in vms},
    }


def readiness_report(vms: Sequence[VmProcess]) -> dict[str, Any]:
    """What each side of every node's readiness exchange last said.

    A readiness failure is a deadline, and a deadline alone cannot say which
    leg of the exchange stopped answering: a node that never serves health and
    a node that stopped answering both expire on the same clock. The last
    reading from each leg is what tells them apart, so it is recorded as it
    is taken and published by a run that failed on it.
    """
    return {
        "http_ready": [str(vm.index) for vm in vms if vm.last_http_result == HEALTH_OK],
        "ready": [str(vm.index) for vm in vms if vm.ready],
        "last_http_results": {str(vm.index): vm.last_http_result for vm in vms},
        "last_ssh_results": {str(vm.index): vm.last_ssh_result for vm in vms},
    }


def prove_guest_gateway_relay(ssh_key: Path) -> None:
    peer_command = shlex.join(
        [
            "ssh",
            "-p",
            str(SSH_PORT_BASE + 2),
            "-oBatchMode=yes",
            "-oConnectTimeout=3",
            "-oConnectionAttempts=1",
            "root@10.0.2.2",
            "true",
        ]
    )
    try:
        run_checked(
            "guest1-to-guest2 QEMU gateway SSH probe",
            host_ssh_command(ssh_key, SSH_PORT_BASE + 1, peer_command),
            timeout=PEER_PROBE_TIMEOUT_S,
        )
    except HarnessError as error:
        raise HarnessError(
            "guest gateway relay is unreachable: VM 1 could not SSH to "
            f"10.0.2.2:{SSH_PORT_BASE + 2} (VM 2). Host-only forwarding is not "
            f"a valid cascade fallback. {error}"
        ) from error


def nix_ssh_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["NIX_SSHOPTS"] = (
        "-F /dev/null -o BatchMode=yes -o IdentitiesOnly=yes "
        "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
    )
    return environment

def copy_private_key(source: Path, run_dir: Path) -> Path:
    destination = run_dir / "id_test"
    try:
        shutil.copyfile(source, destination)
        destination.chmod(0o600)
    except OSError as error:
        raise HarnessError(
            f"failed to make a mode-0600 per-run copy of test SSH key {source}: {error}"
        ) from error
    return destination


def assert_store_path_absent(
    vms: Sequence[VmProcess],
    ssh_key: Path,
    store_path: Path,
) -> None:
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(vms)) as executor:
        futures = [
            executor.submit(assert_vm_store_path_absent, vm, ssh_key, store_path)
            for vm in vms
        ]
        collect_parallel_failures("pre-deployment store isolation", futures)


def assert_vm_store_path_absent(
    vm: VmProcess,
    ssh_key: Path,
    store_path: Path,
) -> None:
    try:
        result = subprocess.run(
            host_ssh_command(
                ssh_key,
                vm.ssh_port,
                shlex.join(["nix", "path-info", str(store_path)]),
            ),
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=VERIFY_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as error:
        raise HarnessError(
            f"VM {vm.index}: pre-deployment nix path-info timed out"
        ) from error
    except OSError as error:
        raise HarnessError(
            f"VM {vm.index}: pre-deployment nix path-info could not start: {error}"
        ) from error
    if result.returncode == 0:
        raise HarnessError(
            f"VM {vm.index}: {store_path} already exists before deployment; "
            "the benchmark requires an independent writable guest store and refuses "
            "to report a cached transfer"
        )




def deploy_and_verify(
    vms: Sequence[VmProcess],
    ssh_key: Path,
    evidence_dir: Path | None,
    store_path: Path,
    inventory_content: str,
    binary_relative_path: PurePosixPath,
    expected_stdout: str,
) -> tuple[float, dict[str, int]]:
    deployment_started = time.monotonic()
    encoded_key = urllib.parse.quote(str(ssh_key), safe="/")
    seed_uri = (
        f"ssh-ng://root@127.0.0.1:{SSH_PORT_BASE + 1}?ssh-key={encoded_key}"
    )
    run_checked(
        "host-to-seed nix copy",
        [
            "nix",
            "copy",
            "--no-check-sigs",
            "--to",
            seed_uri,
            str(store_path),
        ],
        timeout=HOST_COPY_TIMEOUT_S,
        env=nix_ssh_environment(),
    )

    cascade = run_cascade(
        ssh_key,
        store_path,
        inventory_content,
        count=len(vms),
        fanout=2,
        evidence_dir=evidence_dir,
    )

    verify_all_store_paths(vms, ssh_key, store_path, binary_relative_path, expected_stdout)
    return time.monotonic() - deployment_started, cascade


def verify_all_store_paths(
    vms: Sequence[VmProcess],
    ssh_key: Path,
    store_path: Path,
    binary_relative_path: PurePosixPath,
    expected_stdout: str,
) -> None:
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(vms)) as executor:
        futures = [
            executor.submit(
                verify_store_path,
                vm,
                ssh_key,
                store_path,
                binary_relative_path,
                expected_stdout,
            )
            for vm in vms
        ]
        collect_parallel_failures("store-path verification", futures)


def verify_store_path(
    vm: VmProcess,
    ssh_key: Path,
    store_path: Path,
    binary_relative_path: PurePosixPath,
    expected_stdout: str,
) -> None:
    result = run_checked(
        f"VM {vm.index} nix path-info",
        host_ssh_command(
            ssh_key,
            vm.ssh_port,
            shlex.join(["nix", "path-info", str(store_path)]),
        ),
        timeout=VERIFY_TIMEOUT_S,
    )
    if str(store_path) not in result.stdout.splitlines():
        raise HarnessError(
            f"VM {vm.index}: nix path-info succeeded but did not report {store_path}; "
            f"output: {result.stdout.strip()!r}"
        )

    executable = PurePosixPath(str(store_path)) / binary_relative_path
    execution = run_checked(
        f"VM {vm.index} deployed executable",
        host_ssh_command(ssh_key, vm.ssh_port, shlex.join([str(executable)])),
        timeout=VERIFY_TIMEOUT_S,
    )
    if execution.stdout != expected_stdout:
        raise HarnessError(
            f"VM {vm.index}: {executable} returned unexpected output; "
            f"expected {expected_stdout!r}, got {execution.stdout!r}"
        )


def run_checked(
    description: str,
    command: Sequence[str],
    *,
    timeout: float,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    on_failure: Callable[[subprocess.CompletedProcess[str]], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            list(command),
            check=False,
            stdin=None if input_text is not None else subprocess.DEVNULL,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as error:
        raise HarnessError(f"{description} timed out after {timeout:g}s") from error
    except OSError as error:
        raise HarnessError(f"{description} could not start: {error}") from error
    if result.returncode != 0:
        if on_failure is not None:
            # Hand over the full output before raising. A caller that has to
            # explain a failure needs the whole thing, not the tail the error
            # message carries.
            on_failure(result)
        raise HarnessError(
            f"{description} exited with status {result.returncode}: "
            f"{summarize_output(result.stdout, result.stderr)}"
        )
    return result


def summarize_output(stdout: str | None, stderr: str | None) -> str:
    combined = "\n".join(part.strip() for part in (stderr, stdout) if part and part.strip())
    if not combined:
        return "no output"
    return combined[-2000:]


def read_log_tail(path: Path, maximum_bytes: int = 4000) -> str:
    try:
        with path.open("rb") as log:
            log.seek(0, os.SEEK_END)
            size = log.tell()
            log.seek(max(0, size - maximum_bytes))
            return log.read().decode("utf-8", errors="replace").strip() or "<empty>"
    except OSError as error:
        return f"<unable to read log: {error}>"


def process_group_alive(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_vms(vms: Sequence[VmProcess]) -> list[str]:
    errors: list[str] = []
    for vm in vms:
        if process_group_alive(vm.process_group):
            try:
                os.killpg(vm.process_group, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError as error:
                errors.append(f"VM {vm.index}: SIGTERM failed: {error}")

    deadline = time.monotonic() + TERMINATE_GRACE_S
    while time.monotonic() < deadline:
        for vm in vms:
            vm.process.poll()
        if not any(process_group_alive(vm.process_group) for vm in vms):
            break
        time.sleep(0.05)

    for vm in vms:
        if process_group_alive(vm.process_group):
            try:
                os.killpg(vm.process_group, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError as error:
                errors.append(f"VM {vm.index}: SIGKILL failed: {error}")
        try:
            vm.process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            errors.append(f"VM {vm.index}: process group remained after SIGKILL")
        finally:
            vm.log.close()
    return errors


def remove_run_dir(run_dir: Path, tmpdir: Path) -> list[str]:
    if run_dir.parent != tmpdir or not run_dir.name.startswith("fv-"):
        return [f"refusing to remove unsafe run directory: {run_dir}"]
    try:
        shutil.rmtree(run_dir)
    except FileNotFoundError:
        return []
    except OSError as error:
        return [f"failed to remove run directory {run_dir}: {error}"]
    return []


def store_path_bytes(path: Path) -> int:
    """Bytes `nix copy` of this store path will write into one guest.

    Symlinks are counted by lstat, so a payload that is mostly symlinks
    undercounts. That is acceptable for a capacity guard with per-guest
    headroom on top, and it never overcounts, so the check cannot be
    satisfied by a payload that is actually too large.
    """
    total = 0
    for root, _directories, names in os.walk(path):
        for name in names:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


def create_vm_scratch(needed_bytes: int) -> Path | None:
    """A RAM-backed directory for the fleet's -snapshot disk overlays, if any.

    Each restore creates two temporary qcow2 overlays at startup; 128 of them
    created at once on a disk filesystem serialize (on ext4, QMP readiness
    went from 0.12 s to 0.45 s at 64 VMs). They only ever hold a disposable
    VM's disk writes, so tmpfs is the natural home. Hosts without /dev/shm
    (macOS) keep them in each VM's workdir.

    Each overlay is a copy-on-write file over the guest's whole writable
    volume, and everything `nix copy` writes during the measured deployment
    phase lands in it, so the fleet can outgrow a small mount. Containers
    commonly cap /dev/shm at 64 MiB. Checking here keeps that from surfacing as
    an opaque nix copy I/O error inside deployment_s, which would silently turn
    the number into a measurement of tmpfs headroom.

    needed_bytes is what the measured window can actually write, not the
    volume size: a guest that filled its whole 1 GiB volume would need 70 GiB
    at 64 nodes, more than the 63 GiB /dev/shm on the Linux host, so budgeting
    the maximum would decline the scratch everywhere and give back the 0.33 s
    that placing the overlays on tmpfs won. A guest that overruns its budget
    still fails, loudly, as a copy error.
    """
    if not VM_SCRATCH_ROOT.is_dir():
        return None
    try:
        stat = os.statvfs(VM_SCRATCH_ROOT)
    except OSError as error:
        raise HarnessError(f"cannot stat {VM_SCRATCH_ROOT}: {error}") from error
    available = stat.f_bavail * stat.f_frsize
    if available < needed_bytes:
        # Fall back to each VM's workdir rather than fail the guests later.
        return None
    try:
        return Path(tempfile.mkdtemp(prefix="fv-scratch-", dir=VM_SCRATCH_ROOT))
    except OSError as error:
        raise HarnessError(f"cannot create VM scratch under {VM_SCRATCH_ROOT}: {error}") from error


def remove_vm_scratch(scratch: Path | None) -> list[str]:
    if scratch is None:
        return []
    try:
        shutil.rmtree(scratch)
    except FileNotFoundError:
        return []
    except OSError as error:
        return [f"failed to remove VM scratch {scratch}: {error}"]
    return []


def failed_result(
    vms: Sequence[VmProcess], failure: BaseException, cleanup_errors: Sequence[str]
) -> dict[str, Any]:
    """The run's result, as far as it got, published with the failure.

    Every phase records what it measured on the way through, so a run that
    stops at a check has the readings that explain why, and those readings
    are the answer to the question the failure message can only pose. The
    message is what main reports to the operator and is repeated here so the
    published document is readable on its own, and the identity report is
    the one that has to survive: the guest-side divergence it is written
    for is exactly the failure that would otherwise be the only evidence.
    """
    if isinstance(failure, (HarnessError, KeyboardInterrupt)):
        message = str(failure)
    else:
        message = f"unexpected harness failure: {failure}"
    if cleanup_errors:
        message = f"{message}; cleanup also failed: {'; '.join(cleanup_errors)}"
    return {
        "error": message,
        "node_identities": node_identity_report(vms),
        "readiness": readiness_report(vms),
        "status": "failed",
        "statuses": {
            "node_identity_verified": sum(
                1 for vm in vms if node_identity_status(vm) == "agrees_with_device"
            ),
        },
    }


def execute_benchmark(args: argparse.Namespace, run_dir: Path) -> dict[str, Any]:
    reservations: list[PortReservation] = []
    vms: list[VmProcess] = []
    failure: BaseException | None = None
    result: dict[str, Any] | None = None
    scratch: Path | None = None

    try:
        ssh_key = copy_private_key(args.ssh_key, run_dir)
        snapshot: Snapshot | None = None
        snapshot_capture_s: float | None = None
        if args.boot == "snapshot":
            # Before reserve_ports: capture boots one VM on VM 1's ports.
            snapshot, snapshot_capture_s = ensure_snapshot(
                args.runner,
                ssh_key,
                args.snapshot_cache,
                args.guest_mem_mib,
                args.ready_probe_command,
            )
        reservations = reserve_ports(args.count)
        # The measured window writes the readiness probe and one nix copy, not
        # a full volume, so budget that rather than the volume size. See
        # create_vm_scratch: budgeting the volume would need 70 GiB at 64
        # nodes, more than the Linux host's 63 GiB /dev/shm.
        scratch = create_vm_scratch(
            args.count
            * (store_path_bytes(args.store_path) * 2 + SCRATCH_PER_GUEST_MIB * 1024 * 1024)
        )
        readiness_started = time.monotonic()
        fleet_runner = args.runner if snapshot is None else args.restore_runner
        launch_vms(
            fleet_runner,
            run_dir,
            args.count,
            vms,
            snapshot,
            scratch=scratch,
            withheld_health=args.negative_control_vm,
        )
        launch_s = time.monotonic() - readiness_started
        readiness_deadline = readiness_started + args.startup_deadline
        bring_up_max_s = start_all_vms(
            vms, reservations, ssh_key, readiness_deadline, snapshot, args.ready_probe_command
        )
        # Part of bringing the fleet up rather than a step after it, and timed
        # as such: a node's network is down while its driver re-reads the
        # device, so it is not ready until it has been re-probed, and the
        # re-probed address is the whole reason the readiness exchange that
        # precedes it cannot tell 64 restored nodes apart on its own. Excluded
        # from readiness_s it would be a second of every node's time that the
        # reported number did not pay for.
        reprobe_fleet_nics(vms, ssh_key)
        readiness_s = time.monotonic() - readiness_started
        ready_within_target = readiness_s < READINESS_TARGET_S

        assert_fleet_node_identity_is_per_launch(
            vms, ssh_key, args.ready_probe_binary
        )
        assert_probe_file_is_durable(vms[0], ssh_key)
        assert_fleet_state_is_independent(vms, ssh_key)
        assert_fleet_entropy_is_independent(
            vms, ssh_key, args.ready_probe_binary
        )
        prove_guest_gateway_relay(ssh_key)
        assert_store_path_absent(vms, ssh_key, args.store_path)
        inventory_content = write_inventory(run_dir / "inventory.toml", args.count)
        deployment_s, cascade_topology = deploy_and_verify(
            vms,
            ssh_key,
            args.evidence_dir,
            args.store_path,
            inventory_content,
            args.binary_relative_path,
            args.expect_stdout,
        )
        result = {
            "boot": args.boot,
            "count": args.count,
            "deployment_s": round(deployment_s, 6),
            "host_arch": platform.machine(),
            "host_os": platform.system(),
            "snapshot_capture_s": (
                None if snapshot_capture_s is None else round(snapshot_capture_s, 6)
            ),
            # What each node said its own address was, beside what QEMU holds
            # for the device it read, so which node answered and whether the
            # two sides agree can be read off the run's output alone. The same
            # report is published by a run that fails on it.
            "node_identities": node_identity_report(vms),
            # What payload_executions_verified actually compared, so a reader
            # of that count can tell a real output check from an exit status.
            "payload": {
                "binary_relative_path": str(args.binary_relative_path),
                "expect_stdout": args.expect_stdout,
                "store_path": str(args.store_path),
            },
            "readiness_s": round(readiness_s, 6),
            # Both entries are durations, not offsets from the readiness clock:
            # spawning every runner, then the slowest QMP bring-up (forwards +
            # restore). The remainder, readiness_s - launch - bring_up_max, is
            # SSH/HTTP polling. launch used to be added into bring_up_max,
            # which double-counted the whole spawn phase against QMP bring-up.
            "cascade_topology": cascade_topology,
            "readiness_phases_s": {
                "launch": round(launch_s, 6),
                "bring_up_max": round(bring_up_max_s, 6),
            },
            "readiness_target_s": READINESS_TARGET_S,
            "ready_within_target": ready_within_target,
            "status": "ok" if ready_within_target else "performance_target_missed",
            "statuses": {
                # Not the CLI's exit status: the tree the run actually built.
                # A cascade that pushed host-to-each-guest exits 0 and would
                # otherwise be recorded identically to a real fan-out.
                "cascade_relay_verified": cascade_topology["relayed_nodes"],
                "guest_gateway_relay": "ok",
                "host_to_seed": "ok",
                "payload_executions_verified": args.count,
                "readiness_target": "ok" if ready_within_target else "missed",
                "http_ready": args.count,
                "ssh_ready": args.count,
                # Every VM passed an SSH write/read round trip of a unique nonce.
                "ssh_data_exchange_verified": args.count,
                # Every VM still held its own nonce after the whole fleet came up.
                "state_isolation_verified": args.count,
                "entropy_isolation_verified": args.count,
                # Every VM whose guest named the address its own launch
                # assigned it, which is what makes 64 restored nodes
                # distinguishable. Counted from the readings rather than
                # assumed, so the number means what a failed run reports too.
                "node_identity_verified": sum(
                    1 for vm in vms if node_identity_status(vm) == "agrees_with_device"
                ),
                "store_paths_verified": args.count,
            },
        }
    except BaseException as error:
        failure = error

    cleanup_errors: list[str] = []
    for reservation in reservations:
        reservation.close()
    cleanup_errors.extend(terminate_vms(vms))
    cleanup_errors.extend(remove_run_dir(run_dir, args.tmpdir))
    cleanup_errors.extend(remove_vm_scratch(scratch))

    if failure is not None:
        report = failed_result(vms, failure, cleanup_errors)
        # An interrupt the cleanup could honour is still an interrupt, and
        # main's exit code for it says so; anything else, including an
        # interrupt that left VMs behind, is a failure carrying its report.
        if isinstance(failure, KeyboardInterrupt) and not cleanup_errors:
            raise failure
        raise BenchmarkFailed(report["error"], report) from failure
    if cleanup_errors:
        raise HarnessError("benchmark succeeded but cleanup failed: " + "; ".join(cleanup_errors))
    if result is None:
        raise HarnessError("benchmark produced no result")
    return result


def create_run_dir(tmpdir: Path) -> Path:
    try:
        return Path(tempfile.mkdtemp(prefix="fv-", dir=tmpdir))
    except OSError as error:
        raise HarnessError(f"cannot create a fresh run directory under {tmpdir}: {error}") from error

def control_detected(report: dict[str, Any], *, index: int, count: int) -> bool:
    """Whether the run was rejected for this node's readiness and no other.

    A harness that is not looking and a harness that is cannot be told apart
    by the presence of a failure: any error would do, including one that
    failed for an unrelated reason or in a run where every node was healthy.
    What separates them is the whole shape at once: the denied node's health
    never answered, that node never finished its readiness exchange, every
    other node's did, and the run was still rejected. Any one of those on its
    own is satisfied by a harness that ignores readiness entirely.
    """
    readiness = report.get("readiness", {})
    completed = readiness.get("ready", [])
    faulted = readiness.get("last_http_results", {}).get(str(index))
    return (
        faulted is not None
        and faulted != HEALTH_OK
        and str(index) not in completed
        and len(completed) == count - 1
    )


def run_negative_control(args: argparse.Namespace, run_dir: Path) -> dict[str, Any]:
    """Deny one node its health endpoint and require the run to be rejected.

    The run is the ordinary one, unmodified, with one node's health forward
    withheld at launch: the readiness loop, the SSH data exchange and the HTTP
    check are the ones every other run uses, and the failure is theirs. The
    control's own verdict is whether that run was rejected for that node and
    no other, computed from the readings the run published rather than from
    the presence of an error message, so a run that failed for some other
    reason is a control that did not detect anything.
    """
    index = args.negative_control_vm
    try:
        execute_benchmark(args, run_dir)
    except BenchmarkFailed as failure:
        report = failure.report
    except HarnessError as error:
        # A run that failed outside the phases that publish a report is still
        # a run this control has a verdict about, and the record has to say so
        # rather than leave the operator with an exit status alone.
        report = {"error": str(error)}
    else:
        # A run that passed with a node denied readiness is a harness whose
        # checks are not looking, and it has to be reported as the control
        # failing rather than as a fast fleet.
        report = {}
    detected = bool(report) and control_detected(report, index=index, count=args.count)
    readiness = report.get("readiness", {})
    return {
        "detected": detected,
        # The fault, in the terms a reader can check against the launch that
        # installed it: one node's health forward withheld, every other node
        # forwarded and answering.
        "fault": "health_forward_withheld",
        "negative_control_vm": index,
        "observation": {
            "error": report.get("error", "the run was accepted"),
            "last_http_results": readiness.get("last_http_results", {}),
            "last_ssh_results": readiness.get("last_ssh_results", {}),
            "ready": readiness.get("ready", []),
        },
        "status": "detected" if detected else "not_detected",
        "statuses": {
            "nodes_health_ready": len(readiness.get("http_ready", [])),
            "nodes_ran": args.count,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        run_dir = create_run_dir(args.tmpdir)
        if args.negative_control_vm is None:
            result = execute_benchmark(args, run_dir)
        else:
            control = run_negative_control(args, run_dir)
    except BenchmarkFailed as error:
        # On stdout, where the result of a passing run is printed and where
        # the result file is read from: a run that measured the divergence it
        # is failing on has to publish it, or the measurement is recoverable
        # only by repeating the whole run.
        sys.stdout.write(json.dumps(error.report, sort_keys=True) + "\n")
        sys.stderr.write(f"fanout benchmark failed: {error}\n")
        return 1
    except HarnessError as error:
        sys.stderr.write(f"fanout benchmark failed: {error}\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("fanout benchmark interrupted; child VMs were cleaned up\n")
        return 130

    if args.negative_control_vm is not None:
        sys.stdout.write(json.dumps(control, sort_keys=True) + "\n")
        sys.stderr.write(
            f"fanout negative control: readiness of VM {args.negative_control_vm} was "
            f"{control['status']}\n"
        )
        return 0 if control["detected"] else 1

    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0 if result["ready_within_target"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
