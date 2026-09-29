# aarch64-linux builder VM for the Apple half of the fanout64 benchmark.
#
# The darwin fanout64 package boots aarch64-linux guests, so it needs an
# aarch64-linux builder. nixpkgs' stock darwin.linux-builder is 1 core / 2 GB,
# too small for the ~470-derivation guest closure; this is the same
# nix-builder-vm profile sized for a benchmark host. Run with
#   nix run -f bench/fanout64/linux-builder.nix
# (serves on localhost:31022 with /etc/nix/builder_ed25519, like the stock one).
{
  nixpkgs ? <nixpkgs>,
  cores ? 12,
  memoryMiB ? 24 * 1024,
  diskMiB ? 80 * 1024,
}:
let
  darwinPkgs = import nixpkgs { system = "aarch64-darwin"; };
  nixos = import "${nixpkgs}/nixos" {
    system = "aarch64-linux";
    configuration = {
      imports = [ "${nixpkgs}/nixos/modules/profiles/nix-builder-vm.nix" ];
      virtualisation.host.pkgs = darwinPkgs;
      virtualisation.cores = cores;
      virtualisation.darwin-builder.memorySize = memoryMiB;
      virtualisation.darwin-builder.diskSize = diskMiB;
      nix.settings.max-jobs = cores;
    };
  };
in
nixos.config.system.build.macos-builder-installer
