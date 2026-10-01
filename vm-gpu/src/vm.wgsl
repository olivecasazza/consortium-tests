// GPU stack-VM interpreter: one VM per invocation, N VMs per fleet.
//
// Modelled on arXiv:2608.16387 (Uxn on GPU, Glasgow). That paper's decisive
// finding, which this kernel is built to reproduce on better hardware:
// a SEQUENTIAL (1 VM) interpreter is 50-100x slower on GPU than on CPU,
// while a PARALLEL (N VMs) one reaches ~parity only for dependency-free work.
// Their best GPU was a 22-CU laptop part; this targets a datacenter card.
//
// Layout mirrors a Uxn-like machine: 256-byte stack, 64-byte program, and the
// same 12 opcodes. Kept deliberately small so interpreter overhead dominates,
// which is the regime that decides the question.

struct VM {
  prog : array<u32, 16>,   // 64 bytes of bytecode
  stack: array<u32, 64>,   // 256-byte stack
  // Per-VM data words. The host stages input here before dispatch; the VM
  // writes its result back here, and the host reads it out after. This is
  // the only "I/O" a compute shader has: no syscalls, no MMU, no traps.
  data : array<u32, 16>,
  sp    : u32,
  pc    : u32,
  halted: u32,
  pad0  : u32,
  pad1  : u32,
  pad2  : u32,
};

const OP_LIT  : u32 = 0u;
const OP_PUSH : u32 = 1u;
const OP_DUP  : u32 = 2u;
const OP_SWAP : u32 = 3u;
const OP_ADD  : u32 = 4u;
const OP_SUB  : u32 = 5u;
const OP_MUL  : u32 = 6u;
const OP_INC  : u32 = 7u;
const OP_DEC  : u32 = 8u;
const OP_JMP  : u32 = 9u;
const OP_JZ   : u32 = 10u;
const OP_HALT : u32 = 11u;
// Load/store against the per-VM data array. 12 = LOAD (push data[i]),
// 13 = STORE (data[i] = top of stack).
const OP_LOAD : u32 = 12u;
const OP_STORE: u32 = 13u;

@group(0) @binding(0) var<storage, read_write> vms : array<VM>;
@group(0) @binding(1) var<storage, read_write> steps : array<atomic<u32>>;

// Sign-extend a jump displacement. Only bytes >= 128 are negative; an
// unconditional -256 turned a forward jump of 2 into -254 and sent the pc
// out of the 16-word program array.
fn jmp_disp(v: VM) -> i32 {
  let raw = (v.prog[v.pc >> 2u] >> ((v.pc & 3u) * 8u)) & 0xffu;
  return select(i32(raw), i32(raw) - 256, raw >= 128u);
}

@compute @workgroup_size(64)
fn run(@builtin(global_invocation_id) gid: vec3<u32>) {
  let i = gid.x;
  if (i >= arrayLength(&vms)) { return; }
  var v = vms[i];
  var n: u32 = 0u;
  loop {
    if (v.halted != 0u) { break; }
    let op = (v.prog[v.pc >> 2u] >> ((v.pc & 3u) * 8u)) & 0xffu;
    v.pc = v.pc + 1u;
    switch (op) {
      case 0u: { // LIT
        let b = (v.prog[v.pc >> 2u] >> ((v.pc & 3u) * 8u)) & 0xffu;
        v.stack[v.sp] = b; v.sp = v.sp + 1u; v.pc = v.pc + 1u;
      }
      case 1u, 2u: { // PUSH / DUP
        let a = v.stack[v.sp - 1u];
        v.stack[v.sp] = a; v.sp = v.sp + 1u;
      }
      case 3u: { // SWAP
        let a = v.stack[v.sp - 1u]; let b = v.stack[v.sp - 2u];
        v.stack[v.sp - 1u] = b; v.stack[v.sp - 2u] = a;
      }
      case 4u, 5u, 6u: { // ADD / SUB / MUL
        let b = v.stack[v.sp - 1u]; let a = v.stack[v.sp - 2u];
        var r = a + b;
        if (op == 5u) { r = a - b; }
        if (op == 6u) { r = a * b; }
        v.sp = v.sp - 1u; v.stack[v.sp - 1u] = r & 0xffu;
      }
      case 7u: { v.stack[v.sp - 1u] = (v.stack[v.sp - 1u] + 1u) & 0xffu; }
      case 8u: { v.stack[v.sp - 1u] = (v.stack[v.sp - 1u] + 0xffu) & 0xffu; }
      case 9u: { // JMP
        // Sign-extend the displacement: a u8 add can only move forward, so
        // without this the VM cannot express a loop at all.
        let d = jmp_disp(v);
        v.pc = u32(i32(v.pc) + d + 1);
      }
      case 10u: { // JZ
        v.sp = v.sp - 1u;
        let z = v.stack[v.sp] & 0xffu;
        let d = jmp_disp(v);
        v.pc = v.pc + 1u;
        if (z == 0u) { v.pc = u32(i32(v.pc) + d); }
      }
      case 12u: { // LOAD data[i] -> stack
        let i = (v.prog[v.pc >> 2u] >> ((v.pc & 3u) * 8u)) & 0xffu;
        v.stack[v.sp] = v.data[i]; v.sp = v.sp + 1u; v.pc = v.pc + 1u;
      }
      case 13u: { // STORE data[i] <- stack
        let i = (v.prog[v.pc >> 2u] >> ((v.pc & 3u) * 8u)) & 0xffu;
        v.sp = v.sp - 1u;
        v.data[i] = v.stack[v.sp]; v.pc = v.pc + 1u;
      }
      case 11u: { v.halted = 1u; }
      default: { v.halted = 1u; }
    }
    n = n + 1u;
  }
  vms[i] = v;
  atomicAdd(&steps[i], n);
}
