#!/usr/bin/env python3
"""Launch and benchmark the test-only fanout microVM fleet."""

from __future__ import annotations

import argparse
import concurrent.futures
import http.client
import json
import os
import platform
import shlex
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
from typing import IO, Any, Sequence

SSH_PORT_BASE = 22200
HTTP_PORT_BASE = 28200
MAX_VM_COUNT = 64
QMP_SOCKET_NAME = "fanout.qmp"
# Darwin's sockaddr_un.sun_path is 104 bytes including the trailing NUL.
MAX_QMP_SOCKET_PATH_BYTES = 103
READINESS_TARGET_S = 10.0
HOST_COPY_TIMEOUT_S = 600.0
CASCADE_TIMEOUT_S = 900.0
VERIFY_TIMEOUT_S = 30.0
PEER_PROBE_TIMEOUT_S = 15.0
TERMINATE_GRACE_S = 5.0
INVENTORY_GUEST_PATH = "/tmp/consortium-fanout-inventory.toml"



class HarnessError(RuntimeError):
    """An actionable benchmark failure."""


@dataclass
class PortReservation:
    ssh: socket.socket
    http: socket.socket

    def close(self) -> None:
        self.ssh.close()
        self.http.close()


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
        arguments: dict[str, str] | None,
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
            "the separately reported performance target remains 10 seconds"
        ),
    )
    parser.add_argument(
        "--binary-relative-path",
        default="bin/hello",
        help=(
            "executable path relative to the copied store path "
            "(default: bin/hello; its output must be exactly 'Hello, world!')"
        ),
    )
    args = parser.parse_args(argv)

    if not 2 <= args.count <= MAX_VM_COUNT:
        parser.error(f"--count must be between 2 and {MAX_VM_COUNT}")
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

    try:
        args.runner = args.runner.resolve(strict=True)
    except OSError as error:
        parser.error(f"--runner does not resolve to an existing path: {error}")
    if not args.runner.is_file() or not os.access(args.runner, os.X_OK):
        parser.error(f"--runner must be an executable file: {args.runner}")

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
) -> None:
    for index in range(1, count + 1):
        workdir = run_dir / f"vm{index}"
        workdir.mkdir(mode=0o700)
        qmp_socket = workdir / QMP_SOCKET_NAME
        if len(os.fsencode(qmp_socket)) > MAX_QMP_SOCKET_PATH_BYTES:
            raise HarnessError(
                f"QMP socket path is too long ({qmp_socket}); set TMPDIR to a shorter "
                "caller-owned path"
            )

        log_path = workdir / "runner.log"
        try:
            log = log_path.open("wb")
        except OSError as error:
            raise HarnessError(f"VM {index}: cannot open log {log_path}: {error}") from error

        child_env = os.environ.copy()
        child_env["TMPDIR"] = str(workdir)
        try:
            process = subprocess.Popen(
                [str(runner)],
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
            )
        )

        if process.poll() is not None:
            raise HarnessError(
                f"VM {index}: runner exited immediately with status {process.returncode}; "
                f"log tail:\n{read_log_tail(log_path)}"
            )


def connect_qmp(vm: VmProcess, deadline: float) -> socket.socket:
    last_error = "socket has not appeared"
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
            last_error = str(error)
            connection.close()
            time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))


def configure_forwarding(
    vm: VmProcess,
    reservation: PortReservation,
    deadline: float,
) -> None:
    connection = connect_qmp(vm, deadline)
    try:
        qmp = QmpConnection(connection, vm.index)
        qmp.negotiate(deadline)

        # Keep the ports reserved until QEMU is ready to bind them. QMP errors
        # remain authoritative if another process wins the small close/bind race.
        reservation.close()
        commands = (
            f"hostfwd_add net0 tcp:127.0.0.1:{vm.ssh_port}-:22",
            f"hostfwd_add net0 tcp:127.0.0.1:{vm.http_port}-:8080",
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
    finally:
        connection.close()


def configure_all_forwarding(
    vms: Sequence[VmProcess],
    reservations: Sequence[PortReservation],
    deadline: float,
) -> None:
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(vms)) as executor:
        futures = [
            executor.submit(configure_forwarding, vm, reservation, deadline)
            for vm, reservation in zip(vms, reservations, strict=True)
        ]
        collect_parallel_failures("QMP host forwarding", futures)


def host_ssh_command(ssh_key: Path, port: int, remote_command: str) -> list[str]:
    return [
        "ssh",
        "-F",
        "/dev/null",
        "-i",
        str(ssh_key),
        "-p",
        str(port),
        "-oBatchMode=yes",
        "-oConnectTimeout=1",
        "-oConnectionAttempts=1",
        "-oIdentitiesOnly=yes",
        "-oStrictHostKeyChecking=no",
        "-oUserKnownHostsFile=/dev/null",
        "-oLogLevel=ERROR",
        "root@127.0.0.1",
        remote_command,
    ]


def wait_for_vm_ready(vm: VmProcess, ssh_key: Path, deadline: float) -> None:
    ssh_ready = False
    http_ready = False
    last_ssh_error = "not attempted"
    last_http_error = "not attempted"

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
                f"last SSH result: {last_ssh_error}; last HTTP result: {last_http_error}"
            )

        if not ssh_ready:
            try:
                result = subprocess.run(
                    host_ssh_command(ssh_key, vm.ssh_port, "true"),
                    check=False,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=min(1.5, remaining),
                )
            except subprocess.TimeoutExpired:
                last_ssh_error = "check timed out"
            except OSError as error:
                last_ssh_error = str(error)
            else:
                ssh_ready = result.returncode == 0
                if not ssh_ready:
                    last_ssh_error = summarize_output(result.stdout, result.stderr)

        remaining = deadline - time.monotonic()
        if not http_ready and remaining > 0:
            http_ready, last_http_error = check_http_health(vm.http_port, remaining)

        if not (ssh_ready and http_ready):
            time.sleep(min(0.03, max(0.0, deadline - time.monotonic())))


def check_http_health(port: int, remaining: float) -> tuple[bool, str]:
    connection = http.client.HTTPConnection(
        "127.0.0.1",
        port,
        timeout=max(0.01, min(1.0, remaining)),
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
    if body != b"ready\n":
        return False, f"expected b'ready\\n', got {body!r}"
    return True, "ready"


def wait_for_all_ready(
    vms: Sequence[VmProcess],
    ssh_key: Path,
    deadline: float,
) -> None:
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(vms)) as executor:
        futures = [executor.submit(wait_for_vm_ready, vm, ssh_key, deadline) for vm in vms]
        collect_parallel_failures("fleet readiness", futures)


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
    store_path: Path,
    inventory_content: str,
    binary_relative_path: PurePosixPath,
) -> float:
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

    run_checked(
        "inventory upload to seed",
        host_ssh_command(
            ssh_key,
            SSH_PORT_BASE + 1,
            f"umask 077; cat > {shlex.quote(INVENTORY_GUEST_PATH)}",
        ),
        timeout=VERIFY_TIMEOUT_S,
        input_text=inventory_content,
    )

    cascade_command = shlex.join(
        [
            "cascade-copy",
            str(store_path),
            "--inventory",
            INVENTORY_GUEST_PATH,
            "--strategy",
            "log2-fanout",
            "--fanout",
            "2",
            "--no-watch",
        ]
    )
    run_checked(
        "guest cascade-copy",
        host_ssh_command(ssh_key, SSH_PORT_BASE + 1, cascade_command),
        timeout=CASCADE_TIMEOUT_S,
    )

    verify_all_store_paths(vms, ssh_key, store_path, binary_relative_path)
    return time.monotonic() - deployment_started


def verify_all_store_paths(
    vms: Sequence[VmProcess],
    ssh_key: Path,
    store_path: Path,
    binary_relative_path: PurePosixPath,
) -> None:
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(vms)) as executor:
        futures = [
            executor.submit(
                verify_store_path,
                vm,
                ssh_key,
                store_path,
                binary_relative_path,
            )
            for vm in vms
        ]
        collect_parallel_failures("store-path verification", futures)


def verify_store_path(
    vm: VmProcess,
    ssh_key: Path,
    store_path: Path,
    binary_relative_path: PurePosixPath,
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
    if binary_relative_path == PurePosixPath("bin/hello"):
        expected = "Hello, world!\n"
        if execution.stdout != expected:
            raise HarnessError(
                f"VM {vm.index}: {executable} returned unexpected output; "
                f"expected {expected!r}, got {execution.stdout!r}"
            )


def run_checked(
    description: str,
    command: Sequence[str],
    *,
    timeout: float,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
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


def execute_benchmark(args: argparse.Namespace, run_dir: Path) -> dict[str, Any]:
    reservations: list[PortReservation] = []
    vms: list[VmProcess] = []
    failure: BaseException | None = None
    result: dict[str, Any] | None = None

    try:
        reservations = reserve_ports(args.count)
        ssh_key = copy_private_key(args.ssh_key, run_dir)
        readiness_started = time.monotonic()
        launch_vms(args.runner, run_dir, args.count, vms)
        readiness_deadline = readiness_started + args.startup_deadline
        configure_all_forwarding(vms, reservations, readiness_deadline)
        wait_for_all_ready(vms, ssh_key, readiness_deadline)
        readiness_s = time.monotonic() - readiness_started
        ready_within_target = readiness_s < READINESS_TARGET_S

        prove_guest_gateway_relay(ssh_key)
        assert_store_path_absent(vms, ssh_key, args.store_path)
        inventory_content = write_inventory(run_dir / "inventory.toml", args.count)
        deployment_s = deploy_and_verify(
            vms,
            ssh_key,
            args.store_path,
            inventory_content,
            args.binary_relative_path,
        )
        result = {
            "count": args.count,
            "deployment_s": round(deployment_s, 6),
            "host_arch": platform.machine(),
            "host_os": platform.system(),
            "readiness_s": round(readiness_s, 6),
            "readiness_target_s": READINESS_TARGET_S,
            "ready_within_target": ready_within_target,
            "status": "ok" if ready_within_target else "performance_target_missed",
            "statuses": {
                "cascade_copy": "ok",
                "guest_gateway_relay": "ok",
                "host_to_seed": "ok",
                "payload_executions_verified": args.count,
                "readiness_target": "ok" if ready_within_target else "missed",
                "http_ready": args.count,
                "ssh_ready": args.count,
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

    if failure is not None:
        if cleanup_errors:
            cleanup_detail = "; ".join(cleanup_errors)
            raise HarnessError(f"{failure}; cleanup also failed: {cleanup_detail}") from failure
        if isinstance(failure, (HarnessError, KeyboardInterrupt)):
            raise failure
        raise HarnessError(f"unexpected harness failure: {failure}") from failure
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


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        run_dir = create_run_dir(args.tmpdir)
        result = execute_benchmark(args, run_dir)
    except HarnessError as error:
        sys.stderr.write(f"fanout benchmark failed: {error}\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("fanout benchmark interrupted; child VMs were cleaned up\n")
        return 130

    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0 if result["ready_within_target"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
