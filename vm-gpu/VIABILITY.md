# GPU-hosted VM: measured viability

Measured on pdx-nxst-001, Quadro RTX 6000 (CC 7.5, driver 595.84) via Vulkan.
Stack VM in WGSL (one VM per GPU invocation), CPU scalar reference, identical
opcodes and identical instruction accounting. `work_match=yes` on every run:
the GPU and CPU executed exactly the same number of VM instructions.

## Results

| mode | VMs | CPU Ginstr/s | GPU Ginstr/s | speedup |
|---|---|---|---|---|
| straight-line | 1,024 | 0.166 | 0.364 | 2.20x |
| straight-line | 16,384 | 0.164 | 2.421 | 14.75x |
| straight-line | 65,536 | 0.172 | 3.386 | 19.72x |
| straight-line | 262,144 | 0.170 | 3.932 | **23.18x** |
| branching (loop) | 1,024 | 0.176 | 0.090 | **0.51x** |
| branching (loop) | 16,384 | 0.194 | 0.634 | 3.27x |
| branching (loop) | 65,536 | 0.177 | 0.885 | 4.99x |
| branching (loop) | 262,144 | 0.186 | 1.001 | 5.37x |

1,048,576 VMs does not run: the VM-state buffer is 360,710,144 bytes and
exceeds wgpu's 268,435,456-byte maximum buffer size. That is a wgpu
implementation limit, not a GPU limit, so **there is no measurement at
1M** and the trend past 262,144 is unknown. The measured increments are
shrinking (+5.0x then +3.5x from 16k to 262k), so an asymptote is as
plausible as continued growth; do not extrapolate.

## Verdict

**Viable for throughput, not for latency, and only for data-parallel guests.**

* Straight-line guests: 23x at 262k VMs, still scaling at the limit. A GPU
  wins because each warp runs 32 independent VMs with no divergence.
* Looping guests: 5.4x at scale, but only 0.51x at 1,024 VMs. A short
  loop does not have enough work per VM to cover the launch cost.
* The gap between 23x and 5.4x is warp divergence: branches desynchronise
  the lanes that share a warp.

This is the same shape the Glasgow paper reports (arXiv:2608.16387):
19x parallel-vs-sequential on Stencil, but a sequential VM 50-100x slower than
the CPU and never beating it. The paper's best GPU was a 22-CU laptop part;
this is a 24 GB datacenter card, and the result is the same shape with a
higher ceiling.

## What this implies for a hypervisor

A general guest does not look like the straight-line case. Linux boots are
branchy and exit-heavy, which is the 5.4x case at best and the 0.51x case
per-VM at small N. A GPU-hosted hypervisor would win throughput on
data-parallel batches and lose on per-VM boot latency.

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
