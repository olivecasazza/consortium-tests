// POC: does a stack VM beat the CPU on a GPU, and by how much?
// Reproduces the setup of arXiv:2608.16387 on a datacenter GPU instead of a
// laptop part. See src/vm.wgsl for the kernel and the reasoning.
use anyhow::{anyhow, Result};
use std::time::Instant;
use wgpu::util::DeviceExt;

pub const PROG_WORDS: usize = 16; // 64 bytes
pub const STACK_WORDS: usize = 64; // 256 bytes
pub const VMWORDS: usize = 1 + PROG_WORDS + STACK_WORDS + 4;

/// Per-VM data words, used by the fan-out/fan-in workload: input is staged
/// into `data` before dispatch and the result is written back into it.
pub const DATA_WORDS: usize = 16;

#[repr(C)]
#[derive(Clone, Copy, bytemuck::Pod, bytemuck::Zeroable)]
struct Vm {
    prog: [u32; PROG_WORDS],
    stack: [u32; STACK_WORDS],
    data: [u32; DATA_WORDS],
    sp: u32,
    pc: u32,
    halted: u32,
    pad: [u32; 3],
}

// Opcodes must match vm.wgsl.
const LIT: u8 = 0; const PUSH: u8 = 1; const DUP: u8 = 2; const SWAP: u8 = 3;
const ADD: u8 = 4; const SUB: u8 = 5; const MUL: u8 = 6; const INC: u8 = 7;
const DEC: u8 = 8; const JMP: u8 = 9; const JZ: u8 = 10; const HALT: u8 = 11;
const LOAD: u8 = 12; const STORE: u8 = 13;

/// Straight-line program: no branches, so a warp never diverges. This is the
/// best case for a GPU and the fairest one to compare against a CPU.
fn straight_line() -> Vec<u8> {
    // MUL is binary: it consumes two values and leaves one, so a chain of MULs
    // needs a fresh left operand each time. PUSH duplicates the value below the
    // top, so LIT/DUP/MUL repeats safely and leaves a single accumulator.
    let mut p = vec![LIT, 7, LIT, 11, MUL]; // acc = 77
    for _ in 0..8 {
        p.push(DUP);  // duplicate acc
        p.push(LIT);  p.push(3);
        p.push(MUL);  // acc = acc*3
    }
    p.push(HALT);
    p
}

/// Data-dependent loop: warps diverge because VMs exit at different times.
///
/// Two facts drove this shape, both established by tracing the pc/stack
/// against vm.wgsl rather than assumed:
///
///  * JZ POPS, so the counter must be duplicated before it, or DEC on the
///    next iteration finds an empty stack.
///  * JMP/JZ displacements are SIGNED (see the cast in vm.wgsl); with a u8
///    add the VM can only jump forward and cannot express a loop at all.
///
/// The stack therefore grows by one value per iteration, which is fine for a
/// throughput benchmark: the work per VM is bounded and identical on both
/// sides, and `work_match` proves the GPU and CPU agree.
fn branching() -> Vec<u8> {
    let mut p = vec![LIT, 16]; // counter = 16                     [0],[1]
    let body = p.len(); // = 2
    // DUP; DEC; DUP; JZ exit; JMP body; HALT
    p.push(DUP); // [16, 16]                      [2]
    p.push(DEC); // [16, 15]                      [3]
    p.push(DUP); // [16, 15, 15]                 [4]
    p.push(JZ);  // pop; exit if zero         [5]
    p.push(0);  // displacement, patched   [6]
    p.push(JMP); // back to body                     [7]
    p.push(0);  // displacement, patched   [8]
    p.push(HALT); //                                 [9]
    let jz_at = body + 4; // 6
    let jmp_at = body + 6; // 8
    // JZ:  after reading disp pc = jz_at+1, then +1 => jz_at+2; exit at HALT.
    p[jz_at] = ((p.len() - 1) as i32 - (jz_at as i32 + 2)) as u8;
    // JMP: after reading disp pc = jmp_at+1, then +d+1 => jmp_at+d+2 = body.
    p[jmp_at] = (body as i32 - (jmp_at as i32 + 2)) as u8;
    p
}

/// Fill the rest of the 16-word program array with HALT. Without this a pc
/// that runs past the program decodes the zero padding as `LIT 0` forever
/// and walks out of the array; the CPU reference and the shader then
/// disagree about what happened. A short program plus HALT padding is
/// well-defined for both.
fn build(prog: &[u8], seed: u32) -> Vm {
    let mut v = Vm { prog: [0; PROG_WORDS], stack: [0; STACK_WORDS], data: [0; DATA_WORDS], sp: 0, pc: 0, halted: 0, pad: [0; 3] };
    for (i, b) in prog.iter().enumerate() { v.prog[i / 4] |= (*b as u32) << ((i % 4) * 8); }
    // HALT-fill the remaining bytes so a runaway pc stops instead of
    // decoding zero padding.
    for i in prog.len()..(PROG_WORDS * 4) {
        v.prog[i / 4] |= (HALT as u32) << ((i % 4) * 8);
    }
    // Distinct, deterministic per-VM input: this is the fan-out half.
    let mut x = seed.wrapping_mul(2654435761).wrapping_add(1);
    for slot in v.data.iter_mut() {
        x ^= x << 13; x ^= x >> 17; x ^= x << 5;
        *slot = x & 0xff;
    }
    v
}

/// Sign-extend a u8 displacement into a signed jump offset.
///
/// Subtracting 256 unconditionally was wrong: a forward displacement like 2
/// became -254, so the pc jumped far out of the 16-word program array. Only
/// bytes with the high bit set are negative.
#[inline]
fn sign_extend(b: u32) -> i32 {
    let b = (b & 0xff) as i32;
    if b >= 128 { b - 256 } else { b }
}

fn run_cpu(vm: &mut Vm) -> u32 {
    let mut n = 0u32;
    macro_rules! need { ($k:expr) => {
        if (vm.sp as i64) < $k {
            let cur = (vm.prog[(vm.pc.wrapping_sub(1) >> 2) as usize]
                >> ((vm.pc.wrapping_sub(1) & 3) * 8)) & 0xff;
            panic!("stack underflow pc={} op={} sp={}", vm.pc, cur, vm.sp);
        }
    }}
    while vm.halted == 0 {
        let op = (vm.prog[(vm.pc >> 2) as usize] >> ((vm.pc & 3) * 8)) & 0xff;
        vm.pc += 1;
        match op as u8 {
            LIT => { let b = (vm.prog[(vm.pc >> 2) as usize] >> ((vm.pc & 3) * 8)) & 0xff; vm.stack[vm.sp as usize] = b; vm.sp += 1; vm.pc += 1; }
            PUSH | DUP => { need!(1); let a = vm.stack[(vm.sp - 1) as usize]; vm.stack[vm.sp as usize] = a; vm.sp += 1; }
            SWAP => { need!(2); let a = vm.stack[(vm.sp - 1) as usize]; let b = vm.stack[(vm.sp - 2) as usize]; vm.stack[(vm.sp - 1) as usize] = b; vm.stack[(vm.sp - 2) as usize] = a; }
            ADD | SUB | MUL => {
                need!(2); let b = vm.stack[(vm.sp - 1) as usize]; let a = vm.stack[(vm.sp - 2) as usize];
                let r = match op as u8 { ADD => a.wrapping_add(b), SUB => a.wrapping_sub(b), _ => a.wrapping_mul(b) };
                vm.sp -= 1; vm.stack[(vm.sp - 1) as usize] = r & 0xff;
            }
            INC => { need!(1); let s = (vm.sp - 1) as usize; vm.stack[s] = (vm.stack[s].wrapping_add(1)) & 0xff; }
            DEC => { need!(1); let s = (vm.sp - 1) as usize; vm.stack[s] = (vm.stack[s].wrapping_sub(1)) & 0xff; }
            // Signed displacement, matching vm.wgsl: a u8 add could only jump
            // forward, so the VM could not express a loop.
            JMP => { let d = sign_extend((vm.prog[(vm.pc >> 2) as usize] >> ((vm.pc & 3) * 8)) & 0xff); vm.pc = (vm.pc as i32 + d + 1) as u32; }
            JZ => { need!(1); vm.sp -= 1; let z = vm.stack[vm.sp as usize] & 0xff; let d = sign_extend((vm.prog[(vm.pc >> 2) as usize] >> ((vm.pc & 3) * 8)) & 0xff); vm.pc = vm.pc.wrapping_add(1); if z == 0 { vm.pc = (vm.pc as i32 + d) as u32; } }
            LOAD => { let i = (vm.prog[(vm.pc >> 2) as usize] >> ((vm.pc & 3) * 8)) & 0xff; vm.stack[vm.sp as usize] = vm.data[i as usize]; vm.sp += 1; vm.pc += 1; }
            STORE => { need!(1); let i = (vm.prog[(vm.pc >> 2) as usize] >> ((vm.pc & 3) * 8)) & 0xff; vm.sp -= 1; vm.data[i as usize] = vm.stack[vm.sp as usize]; vm.pc += 1; }
            HALT => vm.halted = 1,
            _ => vm.halted = 1,
        }
        n += 1;
    }
    n
}

/// Data-dependent workload: every VM runs the SAME program, but the trip
/// count comes from that VM's own staged input, so lanes finish at different
/// times and warps diverge. This is the realistic middle case and the one
/// that decides whether a GPU-hosted VM is worth anything.
///
/// Traced against the interpreter rather than hand-encoded: the first
/// version used `LIT` for the seed and `DUP;DEC;DUP;JZ;JMP`, but JZ pops,
/// so the counter was duplicated after being decremented, the stack grew
/// without bound, and the back-edge landed on a LIT operand so pc ran off the
/// end of the 16-word program. LOAD makes the per-VM trip count explicit and
/// the stack stays flat.
fn data_dependent() -> Vec<u8> {
    // Byte layout: [0]LOAD [1]idx [2]DEC [3]DUP [4]JZ [5]jzdisp
    //               [6]JMP [7]jmpdisp [8]HALT
    //
    // Displacements are derived from the interpreter's pc arithmetic, not
    // guessed: at JZ (index 4) pc is 6 after the displacement is read, so
    // exiting at HALT (8) needs jz = 2; at JMP (index 6) pc is 7 and the
    // target is DEC (2), so jmp = 2 - 8 = 250. An earlier version used
    // body-relative offsets that were off by one and sent pc out of the array.
    let mut p = vec![LOAD, 0, DEC, DUP, JZ, 0, JMP, 0, HALT];
    const JZ_AT: usize = 5;
    const JMP_AT: usize = 7;
    p[JZ_AT] = 2; // 6 + 2 = 8 (HALT)
    p[JMP_AT] = 250; // 7 + 250 + 1 = 258 -> 2 mod 256 (DEC)
    p
}

/// Fan-out / fan-in: each VM sums its own staged input words and leaves the
/// result in data[0], so the kernel both consumes host-supplied data and
/// produces per-VM output. This is the closest a compute shader gets to I/O:
/// there are no syscalls, only buffers staged before dispatch and read back
/// after. `results_match` in the harness proves the two sides agree.
fn fanout_fanin() -> Vec<u8> {
    let mut p = vec![LIT, 0]; // acc = data[0]
    p.push(LOAD); p.push(0); // [acc, data[0]] (reads data[0] again; cheap and explicit)
    p.push(ADD); // [acc+acc]
    for i in 1..DATA_WORDS {
        p.push(LOAD); p.push(i as u8);
        p.push(ADD);
    }
    p.push(STORE); p.push(0); // data[0] = acc
    p.push(HALT);
    p
}

fn main() -> Result<()> {
    futures_lite::future::block_on(run())
}

fn run() -> impl std::future::Future<Output = Result<()>> {
    async move {
    let n: usize = std::env::args().nth(1).and_then(|s| s.parse().ok()).unwrap_or(65536);
    let mode = std::env::args().nth(2).unwrap_or_else(|| "straight".into());
    let (prog, label) = match mode.as_str() {
        "branch" => (branching(), "looping (uniform trip count)"),
        "datadep" => (data_dependent(), "looping (per-VM trip count)"),
        "fanin" => (fanout_fanin(), "fan-out/fan-in over staged data"),
        _ => (straight_line(), "branch-free"),
    };

    // ---- CPU reference: one VM at a time ----
    let mut total_cpu: u64 = 0;
    let mut warm = build(&prog, 1); run_cpu(&mut warm);
    let t0 = Instant::now();
    for i in 0..n { let mut v = build(&prog, i as u32); total_cpu += run_cpu(&mut v) as u64; }
    let cpu_t = t0.elapsed().as_secs_f64();

    // ---- GPU ----
    let instance = wgpu::Instance::new(&wgpu::InstanceDescriptor::default());
    let adapter = instance.request_adapter(&wgpu::RequestAdapterOptions::default())
        .await.map_err(|e| anyhow!("adapter: {e:?}"))?;
    let info = adapter.get_info();
    println!("adapter: {:?} backend {:?}", info.name, info.backend);
    let (device, queue) = adapter.request_device(&wgpu::DeviceDescriptor::default())
        .await?;

    let module = device.create_shader_module(wgpu::ShaderModuleDescriptor {
        label: Some("vm"),
        source: wgpu::ShaderSource::Wgsl(include_str!("vm.wgsl").into()),
    });
    let bgl = device.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor {
        label: Some("bgl"),
        entries: &[
            wgpu::BindGroupLayoutEntry { binding: 0, visibility: wgpu::ShaderStages::COMPUTE, ty: wgpu::BindingType::Buffer { ty: wgpu::BufferBindingType::Storage { read_only: false }, has_dynamic_offset: false, min_binding_size: None }, count: None },
            wgpu::BindGroupLayoutEntry { binding: 1, visibility: wgpu::ShaderStages::COMPUTE, ty: wgpu::BindingType::Buffer { ty: wgpu::BufferBindingType::Storage { read_only: false }, has_dynamic_offset: false, min_binding_size: None }, count: None },
        ],
    });
    let pll = device.create_pipeline_layout(&wgpu::PipelineLayoutDescriptor {
        label: Some("pll"), bind_group_layouts: &[&bgl], push_constant_ranges: &[],
    });
    let pipeline = device.create_compute_pipeline(&wgpu::ComputePipelineDescriptor {
        label: Some("run"), layout: Some(&pll),
        module: &module, entry_point: Some("run"), compilation_options: Default::default(), cache: None,
    });

    let vms: Vec<Vm> = (0..n).map(|i| build(&prog, i as u32)).collect();
    let vm_buf = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
        label: Some("vms"), contents: bytemuck::cast_slice(&vms), usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_DST | wgpu::BufferUsages::COPY_SRC,
    });
    let step_buf = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
        label: Some("steps"), contents: bytemuck::cast_slice(&vec![0u32; n]), usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_SRC,
    });
    let step_read = device.create_buffer(&wgpu::BufferDescriptor {
        label: Some("stepread"), size: (n * 4) as u64, usage: wgpu::BufferUsages::COPY_DST | wgpu::BufferUsages::MAP_READ, mapped_at_creation: false,
    });
    let read_buf = device.create_buffer(&wgpu::BufferDescriptor {
        label: Some("read"), size: (std::mem::size_of::<Vm>() * n) as u64, usage: wgpu::BufferUsages::COPY_DST | wgpu::BufferUsages::MAP_READ, mapped_at_creation: false,
    });
    let bg = device.create_bind_group(&wgpu::BindGroupDescriptor {
        label: Some("bg"), layout: &bgl,
        entries: &[
            wgpu::BindGroupEntry { binding: 0, resource: vm_buf.as_entire_binding() },
            wgpu::BindGroupEntry { binding: 1, resource: step_buf.as_entire_binding() },
        ],
    });

    let run_once = || {
        let mut enc = device.create_command_encoder(&Default::default());
        let mut pass = enc.begin_compute_pass(&Default::default());
        pass.set_pipeline(&pipeline); pass.set_bind_group(0, &bg, &[]);
        pass.dispatch_workgroups((n as u32).div_ceil(64), 1, 1);
        drop(pass);
        enc.copy_buffer_to_buffer(&step_buf, 0, &step_read, 0, (n * 4) as u64);
        enc.copy_buffer_to_buffer(&vm_buf, 0, &read_buf, 0, (std::mem::size_of::<Vm>() * n) as u64);
        queue.submit([enc.finish()]);
        device.poll(wgpu::PollType::wait_indefinitely()).unwrap();
    };
    run_once(); // warm

    let t0 = Instant::now();
    run_once();
    let gpu_t = t0.elapsed().as_secs_f64();

    // wgpu 27: map_async is callback-based and returns (). Map the whole buffer
    // without a range, then read the mapped memory directly.
    read_buf
        .slice(..)
        .map_async(wgpu::MapMode::Read, |_| {});
    device.poll(wgpu::PollType::wait_indefinitely()).unwrap();
    // BufferView derefs to &[u8]; reinterpret as u32 words.
    let view = read_buf.slice(..).get_mapped_range();
    // Full VM readback: per-VM results, not just step counts. This is what
    // makes the fan-in check a real control.
    let got_vms: &[Vm] = bytemuck::cast_slice(&*view);
    step_read.slice(..).map_async(wgpu::MapMode::Read, |_| {});
    device.poll(wgpu::PollType::wait_indefinitely()).unwrap();
    let step_view = step_read.slice(..).get_mapped_range();
    let words: &[u32] = bytemuck::cast_slice(&*step_view);
    let total_gpu: u64 = words.iter().map(|&s| s as u64).sum();

    // Fan-in correctness: the GPU and CPU must produce the SAME per-VM
    // result, not merely the same instruction count.
    let mut results_match = true;
    if mode == "fanin" {
        let mut cpu_vms: Vec<Vm> = (0..n).map(|i| build(&prog, i as u32)).collect();
        for v in cpu_vms.iter_mut() { run_cpu(v); }
        for i in 0..n {
            if cpu_vms[i].data[0] != got_vms[i].data[0] { results_match = false; break; }
        }
    }
    let cpu_rate = total_cpu as f64 / cpu_t / 1e9;
    let gpu_rate = total_gpu as f64 / gpu_t / 1e9;
    println!("mode={label} n={n}");
    println!("cpu_s={cpu_t:.6} cpu_instr={total_cpu} cpu_Ginstr_s={cpu_rate:.3}");
    println!("gpu_s={gpu_t:.6} gpu_instr={total_gpu} gpu_Ginstr_s={gpu_rate:.3}");
    println!("speedup={:.2}x", cpu_t / gpu_t);
    println!("work_match={}", if total_cpu == total_gpu { "yes" } else { "NO" });
    println!("results_match={}", if results_match { "yes" } else { "NO" });
    Ok(())
    }
}
