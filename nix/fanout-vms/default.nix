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

  # microvm-run execs QEMU with a fixed argument list. Forward extra arguments
  # so the launcher can capture and restore snapshots (-snapshot, -incoming);
  # with no arguments the VM boots exactly as microvm.nix defines it.
  runner = vmHostPackages.runCommand "fanout64-microvm-run" { meta.mainProgram = "microvm-run"; } ''
    mkdir -p $out/bin
    sed 's/\''${runtime_args:-}[[:space:]]*$/''${runtime_args:-} "$@"/' \
      ${lib.getExe' configuration.config.microvm.runner.qemu "microvm-run"} > $out/bin/microvm-run
    grep -q 'runtime_args:-} "\$@"$' $out/bin/microvm-run
    chmod +x $out/bin/microvm-run
  '';
in
{
  inherit configuration guestSystem runner;
  defaultPayload = guestPkgs.hello;
}
