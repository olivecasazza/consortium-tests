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
| Host systems | `x86_64-linux` (verified); `aarch64-darwin` evaluates but is unbuilt |
| Readiness | SSH *and* HTTP `/health` answering on every node |
| Payload | a store path copied host -> seed, then relayed log2-fanout to the rest |

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
- Run the VMs on a **Linux + KVM** host. The `aarch64-darwin` package
  evaluates but has never been built or booted; treat it as unproven.
- The `| tail -1` matters: the flake devShell prints a banner, so the raw
  `--raw` output has trailing noise.
- `--startup-deadline` (default 120 s) is a hard safety timeout, **not** the
  performance target. Raising it will not make the target pass.
- Exit status is 1 for a performance miss as well as for a real failure.
  Read the JSON: `status: "performance_target_missed"` with `ready_within_target:
  false` is a working harness that missed the target, not a broken one.
- Ports are `22201`-`22264` (SSH) and `28201`-`28264` (HTTP). A collision
  names the exact port.

**Measured results** on a 128-core / 125 GB Linux KVM host, with the
optimized guest (2 vCPU, no serial console, `console.enable = false`):

| Nodes | Baseline | Optimized | Deployment | 10 s target |
|---|---|---|---|---|
| 2 | 6.52 s | 4.78 s | 0.68 s | met |
| 32 | 9.9-11.1 s | 7.97 s | 1.64 s | met |
| 64 | 15.1-17.2 s | 13.2-14.0 s | 2.2-2.4 s | **missed** |

Readiness (launch until every node answers SSH and HTTP) and deployment
(host -> seed copy, then log2 relay rounds, then verifying the payload executed
on every node) are reported separately, because a fast payload distribution
over a slow fleet boot is still a slow test.

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

