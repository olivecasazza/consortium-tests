{
  inputs,
  hostSystem,
  consortiumCli,
}:

let
  lib = inputs.nixpkgs.lib;
  guestSystems = {
    aarch64-darwin = "aarch64-linux";
    x86_64-linux = "x86_64-linux";
  };
  guestSystem =
    guestSystems.${hostSystem}
      or (throw "fanout64 microVMs are unsupported on host system ${hostSystem}");

  vmHostPackages = import inputs.nixpkgs { system = hostSystem; };
  guestPkgs = import inputs.nixpkgs { system = guestSystem; };

  configuration = lib.nixosSystem {
    system = guestSystem;
    specialArgs = {
      inherit consortiumCli vmHostPackages;
    };
    modules = [
      inputs.microvm-nix.nixosModules.microvm
      ./guest.nix
    ];
  };
in
{
  inherit configuration guestSystem;
  runner = configuration.config.microvm.runner.qemu;
  defaultPayload = guestPkgs.hello;
}
