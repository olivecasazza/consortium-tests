# Test-only SSH keypair for the fanout64 fleet

`id_fanout` / `id_fanout.pub` are a **private/public keypair committed on
purpose**. The public key is baked into every `fanout64` microVM as root's
*only* authorized key, so the benchmark can SSH into the fleet with zero
per-operator setup.

This key is deliberately **separate from `nix/vms/keys/id_test`** in the
`consortium` repo, which belongs to the six-node tap fleet on the isolated
`10.99.0.0/24` subnet. The `fanout64` guests sit on QEMU user-mode networking
with per-node host port forwards -- a different trust boundary -- so sharing
one key across both fleets would widen its blast radius for no benefit.

Rules:

- **Never** use this keypair outside the `fanout64` test fleet.
- **Never** add `id_fanout.pub` to any real machine, service, CI secret, or
  account's `authorized_keys`.
- Anyone with repo access can log in as **root** to any VM built from
  `nix/fanout-vms/`. That is acceptable *only* because those VMs are
  ephemeral test guests with no secrets.

Regenerate (only if rotating the fleet key deliberately):

```sh
ssh-keygen -t ed25519 -f nix/fanout-vms/keys/id_fanout -N '' \
  -C 'consortium-fanout64-root-TEST-ONLY'
```
