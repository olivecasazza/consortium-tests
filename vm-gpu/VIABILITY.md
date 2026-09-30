# GPU-hosted VM: measured viability

Measured on pdx-nxst-001, Quadro RTX 6000 (CC 7.5, driver 595.84) via Vulkan.
Stack VM in WGSL (one VM per GPU invocation), CPU scalar reference, identical
opcodes and identical instruction accounting. `work_match=yes` on every run:
the GPU and CPU executed exactly the same number of VM instructions.
## Results

Measured on one build, with the readback timed separately from the kernel
(Quadro RTX 6000, driver 595.84, host load ~4/128 cores). Every row reports
`work_match=yes` and `results_match=yes`: equal VM instruction counts on both
sides, and equal per-VM output values.

`compute` is dispatch + execute. `total` adds the per-run result readback a
batch harness actually pays. Both are shown because they answer different
questions, and reporting one while the reader assumes the other is what made
an earlier 23x figure irreproducible.

| workload | n | CPU | GPU compute | GPU total | vs CPU | compute-only |
|---|---:|---:|---:|---:|---:|---:|
| branch-free | 65536 | 15.1 ms | 0.55 ms | 5.26 ms | 2.87x | 26.9x |
| branch-free | 262144 | 60.3 ms | 1.86 ms | 20.9 ms | 2.89x | 32.5x |
| looping, uniform | 65536 | 6.56 ms | 0.58 ms | 5.31 ms | 1.24x | 11.9x |
| looping, uniform | 262144 | 25.8 ms | 1.85 ms | 20.9 ms | 1.24x | 16.2x |
| fan-out/fan-in | 65536 | 19.4 ms | 0.58 ms | 5.31 ms | 3.65x | 40.7x |
| fan-out/fan-in | 262144 | 78.2 ms | 1.89 ms | 20.8 ms | 3.76x | 49.1x |
| per-VM divergent | 65536 | 115 ms | 0.50 ms | 5.19 ms | 22.2x | 205x |
| per-VM divergent | 262144 | 459 ms | 1.66 ms | 20.6 ms | 22.2x | 248x |

## The readback is 92% of the GPU's time

The VM array is 408 bytes; at 262144 VMs the readback moves 107 MB. Its cost
is essentially identical across four workloads that differ by 70x in
instruction count:

| workload | xfer_s | share of GPU time |
|---|---:|---:|
| branch-free | 0.020851 | 91.8% |
| looping, uniform | 0.020881 | 91.9% |
| fan-out/fan-in | 0.020795 | 91.7% |
| per-VM divergent | 0.020637 | 92.6% |

A 1.2% spread against a 14.4% spread in compute time. The transfer is a fixed
cost that the kernel's work does not change, and it runs at 5.1 GB/s — about
33% of what the Gen3 x16 link allows.

So the two speedup columns are not competing claims. **Compute-only is the
kernel's throughput. Total is what a harness that reads results back pays
today.** A design that keeps VM state resident on the device and reads back
only a summary would land near the compute-only column; one that ships every
VM's state to the host lands near the total column, and the transfer is then
the thing to optimise.

The highest-value change available is host-side: one mapped staging buffer and
a larger copy per submission, rather than restructuring the kernels.

## Read the small-N numbers carefully

The GPU has a fixed dispatch cost that does not shrink with VM count. Measured
on the divergent workload, with compute and transfer separated:

| VMs | CPU | GPU compute | GPU total | ratio |
|---|---|---|---|---|
| 4 | 4.0 us | 73 us | 67 us | 0.06x |
| 256 | 415 us | 81 us | 89 us | 4.65x |
| 1,024 | 1,767 us | 78 us | 155 us | 11.37x |
| 8,192 | 14,380 us | 132 us | 729 us | 19.73x |

GPU compute time is nearly flat from 4 to 8,192 VMs - 73 us to 132 us - so
**at small N the ratio is pure launch overhead** and says nothing about
throughput. A 0.06x at N=4 is not a slow GPU; it is a GPU that has not been
given work to hide its pipeline behind. The crossover sits near 256 VMs.

## Verdict

**Viable for throughput, not for latency, and only for data-parallel guests.**

- Branch-free: 2.9x as a batch harness measures it, 32x on compute alone.
- Per-VM divergent: 22x total, 248x compute-only.
- Fan-in: 3.8x total, 49x compute-only. Flat from 16k to 262k.
- Short uniform loops: 1.2x total, 16x compute-only.

The pattern is **work per VM and how much you move**, not control flow alone.
Divergence is cheap when lanes still have bulk work; it is expensive when each
VM does only a handful of operations. But at these sizes the readback dominates
every row, so the total column mostly measures bytes, not instructions. The
fan-in row (3.8x) remains the most representative for a batch fleet, because a
few ops per VM is closer to real work than a thousand straight-line ops.

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
