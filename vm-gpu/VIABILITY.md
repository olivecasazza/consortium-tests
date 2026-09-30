# GPU-hosted VM: measured viability

Measured on pdx-nxst-001, Quadro RTX 6000 (CC 7.5, driver 595.84) via Vulkan.
Stack VM in WGSL (one VM per GPU invocation), CPU scalar reference, identical
opcodes and identical instruction accounting. `work_match=yes` on every run:
the GPU and CPU executed exactly the same number of VM instructions.
## Results

| workload | 1k | 16k | 65k | 262k |
|---|---|---|---|---|
| branch-free | 2.20x | 14.75x | 19.72x | 23.18x |
| looping, uniform trip count | 0.51x | 3.27x | 4.99x | 5.37x |
| **fan-out/fan-in over staged data** | 2.01x | 3.73x | 3.73x | 3.82x |
| **per-VM trip count (divergent)** | 11.88x | 21.16x | 22.36x | 22.51x |

All runs report `work_match=yes` (equal VM instruction counts on both sides)
and `results_match=yes` (equal per-VM output values), so the speedups compare
equal work, not merely equal time.

## Read the small-N numbers carefully

The GPU has a fixed dispatch cost that does not shrink with VM count.
Measured on the divergent workload:

| VMs | CPU | GPU | ratio |
|---|---|---|---|
| 4 | 4.0 us | 98.0 us | 0.04x |
| 256 | 434 us | 89 us | 4.88x |
| 1,024 | 1,824 us | 154 us | 11.88x |
| 8,192 | 15,122 us | 747 us | 20.24x |

GPU time is nearly flat from 4 to 8,192 VMs: a ~90 us fixed cost plus ~79 ns
per VM, against ~1,846 ns per VM on the CPU. The marginal cost of a VM is ~23x
cheaper on the GPU, but **at small N the ratio is pure launch overhead** and
says nothing about throughput. A 0.04x at N=4 is not a slow GPU; it is a GPU
that has not been given work to hide its pipeline behind.

## Verdict

**Viable for throughput, not for latency, and only for data-parallel guests.**

- Identical work: 23x at 262k, climbing slowly.
- Per-VM divergent work: 22.5x at 262k. Divergence costs less than expected
  when each lane still has bulk work.
- Fan-in (each VM consumes staged input and produces its own result): 3.8x,
  flat from 16k to 262k.
- Short uniform loops: 5.4x.

The pattern is **work per VM**, not control flow alone. Divergence is cheap when
lanes still have bulk work; it is expensive when each VM does only a handful of
operations. The fan-in row (3.8x) is the most representative number for a real
fleet, because 16 ops per VM is closer to real work than 1000 straight-line ops.

This matches the Glasgow paper's shape (arXiv:2608.16387): 19x
parallel-vs-sequential on Stencil, a sequential VM 50-100x slower than the CPU,
and never beating it. Their best GPU was a 22-CU laptop part; this is a 24 GB
datacenter card, so the ceiling is higher, but the shape is the same.

## What this implies for fan-out -> fan-in on a real fleet

A compute shader has no syscalls, no MMU and no traps. "I/O" is only: stage
input into a per-VM buffer before dispatch, read results out after. That is a
batch interface, not a service interface - you cannot run `sshd` and answer
requests in these VMs. For the 64-node `fanout64` harness, whose nodes are
heterogeneous services each with its own Nix store answering SSH and HTTP, the
honest projection is the fan-in row (~3.8x), not the 22x rows.

## Environment notes

* Use the 64-bit ICD: `nvidia-x11-595.84/share/vulkan/icd.d/nvidia_icd.json`
  (a sibling `lib32` package also ships a libGLX_nvidia; it is ELFCLASS32
  and cannot be used on x86-64).
* lavapipe (`lvp_icd`) also works and is a useful control: it is a CPU
  rasterizer, so it should NOT show a large speedup over the CPU reference.
  Observed 0.01x, which is the expected control result.

## Reproducing

The GPU is reached through the 64-bit NVIDIA ICD. A sibling package also
ships a `libGLX_nvidia`, but that one is ELFCLASS32 and cannot be used on
x86-64:

```sh
N=/nix/store/gkjg210fnmiiqvlmrwwjhksn9xykflkm-nvidia-x11-595.84
export LD_LIBRARY_PATH=$N/lib:/nix/store/y5a34h6vgvsc1pmfmvcqfcvxy2grg3hx-vulkan-loader-1.4.341.0/lib
export VK_ICD_FILENAMES=$N/share/vulkan/icd.d/nvidia_icd.json

nix build ./vm-gpu            # or: cd vm-gpu && nix build
$(nix build ./vm-gpu --no-link --print-out-paths)/bin/vm-gpu 262144 straight
$(nix build ./vm-gpu --no-link --print-out-paths)/bin/vm-gpu 262144 branch
```

Confirm the adapter line reads `Quadro RTX 6000` / `Vulkan` before trusting
any number. As a control, Mesa's `lvp_icd` (lavapipe) is a **CPU** rasterizer
and must NOT show a speedup; it reports 0.01x, which is the expected result.
