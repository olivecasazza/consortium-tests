# fanout64 — 64-node microVM closure-distribution benchmark

`nix/fanout-vms/` is a second, independent fleet: 64 single-purpose microVMs,
each with its **own** writable Nix store on its own disk image, a `sshd`, and a
busybox `httpd` health endpoint. It exists to measure one thing — how fast a
Nix closure can be spread across many nodes by relaying peer-to-peer rather
than pulling every node from one source.

Unlike the tap fleet it needs no tap interfaces, no static IPs and no host
preparation: each guest gets a QEMU user-mode network and per-node host port
forwards applied over QMP at launch.

| Property | Value |
|---|---|
| Count | 2-64 (default 64), `--count N` |
| Guest | 1 vCPU, 512 MiB, immutable erofs store + writable ext4 overlay |
| Host systems | `x86_64-linux` (KVM) and `aarch64-darwin` (HVF), both built and measured |
| Readiness | SSH *and* HTTP `/health` answering on every node |
| Payload | a store path copied host -> seed, then relayed log2-fanout to the rest |

**What "ready" means.** Both checks, on every node, and a node that fails
either is not counted:

1. `GET /health` over the node's host forward must return status 200 with a
   body of exactly `ready\n`.
2. A full root SSH login must write a fresh random per-node nonce, `fsync`
   it, and read the bytes back byte-for-byte. The probe is
   `nix/fanout-vms/probe.c`, a single binary: the shell pipeline it replaced
   (`umask 077 && cat > f && sync f && cat f`) cost three execs per node on
   top of the login shell, 64 times at once, on the guest-vCPU-bound part of
   the run.

The nonce file is `/nix/.rw-store/fanout-ready-probe`, on the **block-backed
ext4 volume**. It cannot move to the initrd tmpfs: `sync(2)` on tmpfs is a
no-op, so the round trip would pass without the write ever reaching virtio-blk
or the host. `assert_probe_file_is_durable` fails the run if that path is ever
a RAM filesystem, because the failure it prevents is a green run that proves
nothing.

**Two required inputs** keep the verification from degrading silently:

- `--expect-stdout` is the exact output the deployed payload must produce
  (`\n` decodes to a newline). Without it the payload check was an exit-status
  test that still reported `payload_executions_verified: 64`.
- `--ready-probe-binary` is the guest store path of that probe binary. The
  harness supplies the file path and the byte-for-byte comparison itself; only
  the store path comes from the caller, so a wrong binary fails loudly rather
  than passing quietly.

Every run also proves two independence properties, because every restore maps
the same captured RAM image copy-on-write and nothing else would notice if that
stopped being true:

- `state_isolation_verified` re-reads every node's probe file and requires the
  unique bytes that node wrote, so VM i's writes must be invisible to VM j.
- `entropy_isolation_verified` draws from `/dev/urandom` on two nodes and
  requires them to differ. The write canary cannot see this: a shared entropy
  stream is not a cross-node write leak, so every other check would stay green
  while two nodes drew identical bytes. virtio-rng pulls from the host, so
  restored nodes should differ -- and a fleet test that generated keys or
  tokens per node would otherwise be drawing from captured state.

```bash
# The cheap tests: launcher safety + the SSH-port parsing in the cascade.
nix develop --command python3 -m unittest discover -s nix/fanout-vms -p 'test_*.py' -v
nix develop --command cargo test -p consortium-nix

# Build, ship to a Linux KVM host, and run.
nix develop --command nix build --no-link .#packages.x86_64-linux.fanout64
APP=$(nix develop --command nix eval --raw .#packages.x86_64-linux.fanout64.outPath | tail -1)
nix develop --command nix copy --to ssh-ng://root@pdx-nxst-001.schrodinger.com \
  .#packages.x86_64-linux.fanout64
ssh root@pdx-nxst-001.schrodinger.com "TMPDIR=/var/tmp $APP/bin/fanout64 --count 2"
ssh root@pdx-nxst-001.schrodinger.com "TMPDIR=/var/tmp $APP/bin/fanout64 --count 64"
```

Notes:
- Both host systems are built and run: `x86_64-linux` on a 128-core KVM host
  (pdx-nxst-001) and `aarch64-darwin` on Apple Silicon under HVF. Apple needs
  the `aarch64-linux` guest closure cross-built, so it needs that builder
  reachable; the Linux leg builds entirely on the target host.
- The `| tail -1` matters: the flake devShell prints a banner, so the raw
  `--raw` output has trailing noise.
- `--startup-deadline` (default 120 s) is a hard safety timeout, **not** the
  performance target. Raising it will not make the target pass.
- Exit status is 1 for a performance miss as well as for a real failure.
  Read the JSON: `status: "performance_target_missed"` with `ready_within_target:
  false` is a working harness that missed the target, not a broken one.
- Ports are `22201`-`22264` (SSH) and `28201`-`28264` (HTTP). A collision
  names the exact port.

**How to run it.** The driver is `nix/fanout-vms/run.sh`, in this repo beside
the harness, and it archives a pinned commit of this repo into a scratch
directory and runs that. Run it from a checkout:

```sh
nix/fanout-vms/run.sh                              # both platforms
FANOUT_PLATFORMS=linux nix/fanout-vms/run.sh       # remote build, leaves the Mac idle
FANOUT_REPS=6 nix/fanout-vms/run.sh                # more samples; the median of 3
                                                   # cannot resolve a sub-second
                                                   # change on a busy Apple host
```

`FANOUT_BASE_REPO` and `FANOUT_BASE_REV` point it at a different checkout or
commit; the defaults are this checkout and `BASE_REV` below. The Apple leg
needs the aarch64-linux builder, `nix/fanout-vms/linux-builder.nix`, which the
driver names if it is not listening on `:31022`.

`FANOUT_LINUX_HOST` and `FANOUT_LINUX_SYSTEM` name the target of the Linux leg,
and the fleet spans two architectures: `pdx-nxst-001`/`-003` are `x86_64-linux`
and `pdx-nxmm-01`/`-02`/`-03` are `aarch64-linux`. A system the flake does not
build for is refused before the build rather than built for and pushed to a
host that cannot run it, so the two are set together:

```sh
FANOUT_LINUX_HOST=root@pdx-nxmm-01.schrodinger.com \
FANOUT_LINUX_SYSTEM=aarch64-linux \
FANOUT_PLATFORMS=linux nix/fanout-vms/run.sh
```

There is no copy of the harness in any other repo, and there should not be:
harness and driver both live here.

**Two pins in `flake.nix` that are not stylistic:**

- `consortium` is pinned to `850247da` ("accept SSH ports in cascade source
  addresses"), which is not on master. Relay sources are addressed as
  `root@10.0.2.2:<port>`, and on master every guest-sourced hop fails, so only
  seed -> 2 children land. The fleet still passes its per-node checks, which
  is exactly why this is worth stating: a green run does not prove the relay
  tree was used.
- `consortium-cli` is built with `doCheck = false`. Its own suite passes in its
  CI, but its `checkPhase` has a BrokenPipe race that fails guest builds
  nondeterministically.

**Measured results**, 64 nodes, on both host systems. `--boot snapshot` is the
default and is what the table reports; `--boot cold` boots every guest from
the kernel and is reported separately.

| Host | Runs | Readiness (min / median / max) | Deployment (median) | 8 s target |
|---|---|---|---|---|
| `x86_64-linux` (KVM, 128 cores) | 30 | 0.418 / 0.466 / 0.973 s | 2.45 s | met |
| `aarch64-darwin` (HVF) | 18 | 0.871 / 1.368 / 3.265 s | 3.02 s | met |

Counted runs are only those reporting the full verification set
(`ssh_data_exchange_verified`, `state_isolation_verified`,
`payload_executions_verified` and `store_paths_verified` all at 64). That is a
deliberately small sample: the isolation canary landed partway through, so runs
before it cannot be counted as evidence of anything, and runs judged against
the pre-8 s target bar are not mixed in.

The Apple spread is the *host*, not the harness. The 18-core Mac that runs the
aarch64 leg is shared, and its load average was observed ranging from 6.3 to
135 over a single day, with readiness spread inside one 3-rep batch ranging
from 0.42 s to 1.97 s at the high end. A quiet Apple host is the 0.87 s end of
that column. The driver samples load before the fleet starts, prints it as
`apple_ambient_load`, and warns above a ceiling precisely so a batch taken
under load cannot be read as a harness result.

The guest is `vcpu = 1`. The cold-boot figures further down were taken at
`vcpu = 2` and are kept as history: that analysis is about the boot path, and
the default path restores a snapshot instead of booting, where a second vCPU
only doubled the host threads 64 VMs contend for. Those rows are not
reproducible against today's guest.

**Earlier measurements, kept as the record they are** — taken at `vcpu = 2`,
before the probe binary and the isolation canary existed, in a quieter window
than the table above:

| Mode | Nodes | Readiness | Deployment | 8 s target |
|---|---|---|---|---|
| snapshot | 2 | 0.235 s | 11.26 s (2-node cascade, cold cache) | met |
| snapshot | 64 | **0.44-0.50 s** | 2.37-2.40 s | met, ~16x headroom |
| snapshot | 64 | 0.68 s (host at load 104) | 2.49 s | met |
| `--boot cold` | 64 | 20.80 s / 27.74 s | 2.42 s | **missed** |

The phase breakdown at 64 nodes was `launch 0.055 s` then `bring_up_max
0.16 s`. The 0.68 s run was taken while an unrelated vLLM inference job held
the host at load 104, so a quieter machine gives more headroom, not less.

**Capture is a one-time cost.** With an empty snapshot cache the run reports
`snapshot_capture_s: 12.38` and lands at 0.42 s readiness once capture
finishes. Subsequent runs read the cache under `~/.cache/fanout64-snapshots`
and skip both the boot and the capture. First run end to end is therefore
~12.8 s; every run after that is ~2.8 s including the full 64-node closure
cascade.

Readiness (launch until every node answers SSH and HTTP) and deployment
(host -> seed copy, then log2 relay rounds, then verifying the payload
executed on every node) are reported separately, because a fast payload
distribution over a slow fleet boot is still a slow test. Cold boot is
40-60x slower than snapshot at 64 nodes, so the two modes are reported
alongside each other rather than one replacing the other: the cold path is
still what proves a guest boots from nothing.

Every run also reports `state_isolation_verified` and
`ssh_data_exchange_verified` at 64, so a green run means the restored
guests are genuinely independent and exchanging real data - not merely that
64 sockets accepted a connection.

**Snapshot restore, not boot tuning, is what made this pass.** Everything below
documents the cold-boot path: it is what produced the 20.8 s figure and the
13-14 s figures quoted in the vCPU tables, all under `--boot cold`. With the
default `--boot snapshot` those same 64 nodes come up in 0.44 s, so the boot
tuning below moves a number that no longer gates the target. It is kept
because the cold path still has to work, and because the measurements are
what justify the guest config as it stands.

**What the measurements ruled out.** The host is not the bottleneck: during a
64-node boot it sits at 95.8% idle, load 18 of 128 cores, I/O wait 0.1%, no
steal. Not page cache - three consecutive runs took 15.36 s cold, 15.14 s and
15.19 s warm. Not stragglers: first node ready at 13.62 s, p50 15.68 s, last
17.20 s, a 3.6 s spread, so the whole fleet slows uniformly. Not overlay
creation: 64 x 1 GiB `truncate` + `mkfs.ext4` costs 0.19 s in total. Silencing
kernel and initrd console output changed nothing (16.30 s).

**What helped.** Three changes, measured together:

| Change | Single node | 64 nodes | Effect |
|---|---|---|---|
| 2 vCPU per guest | 4.78 s vs 5.47 s | 13.2-14.0 s vs 15.2-18.6 s | **the dominant win, ~2-5 s at 64 nodes** |
| `microvm.qemu.serialConsole = false` | (bundled above) | | kills `dev-ttyS0.device`, largest `blame` entry (~4.0 s) |
| `console.enable = false` | | | drops `kbd`: initrd 2,029 -> 1,468 files, 27.6 -> 21.9 MB |

The vCPU bump was measured by A/B, not assumed: an otherwise identical build
with `vcpu = 1` gave 5.47 s single-node and 15.22 / 16.17 / 18.56 s at 64 nodes,
against 4.78 s and 13.15-14.00 s with `vcpu = 2`. It is worth roughly 2-5 s at
64 nodes - more than the other two changes combined. Giving each guest a second
vCPU matters because the boot is serial and decompress-bound, not because the
host is short of cores: at 95.8% idle the capacity was free.

**The vCPU curve, measured at each point.** A third build with `vcpu = 4`
completed the curve, and it is not monotonic - 2 is the optimum, not "more is
better":

| vCPU per guest | Host threads at 64 nodes | Single node | 64 nodes |
|---|---|---|---|
| 1 | 64 | 5.47 s | 15.22-18.56 s |
| **2** | **128** | **4.78 s** | **13.15-14.00 s** |
| 4 | 256 (2x oversubscribed) | 4.88 s | 20.22-22.13 s |

Four vCPUs is ~7 s *worse* at 64 nodes while being no better on a single node,
so the second vCPU is buying genuine parallelism during a serial, decompress-bound
boot and the third and fourth are pure oversubscription. 64 x 2 = 128 threads
exactly saturates the host's 128 cores, which is why 2 is the sweet spot.

The initrd is 57% of a boot (3.45 s of 6.08 s) and is re-extracted in every
one of 64 VMs, so shrinking it is the highest-leverage change available.
Still present and still unused: `tpm2-tss` (83 files, no TPM device on this
QEMU config) and `lvm2` (50 files, the volumes are plain ext4).

**What did not.** Even with a 28% smaller initrd and a 26% faster single-node
boot, 64 nodes only reaches 13.2-14.0 s. Per-node boot got much cheaper and the
fleet-wide boot barely moved, so the penalty is in the 64-way concurrent boot
itself, not in any one guest's boot path. Getting 64 nodes under 10 s needs a
structural change - a pre-seeded or snapshot-backed disk so the initrd is not
re-extracted per VM - not more guest tuning.

Deployment - the thing this fleet actually benchmarks - scales cleanly and
stays under 2.4 s at every size tried, which is the useful result.

