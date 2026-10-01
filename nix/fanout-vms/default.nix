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
    # The identity mapping, unlike aarch64-darwin above: an aarch64-linux
    # host runs an aarch64-linux guest on its own architecture, with no
    # emulation and no cross builder. The pdx-nxmm hosts are this case.
    aarch64-linux = "aarch64-linux";
  };
  guestSystem =
    guestSystems.${hostSystem}
      or (throw "fanout64 microVMs are unsupported on host system ${hostSystem}");

  vmHostPackages = import inputs.nixpkgs { system = hostSystem; };
  guestPkgs = import inputs.nixpkgs { system = guestSystem; };

  configuration = lib.nixosSystem {
    system = guestSystem;
    specialArgs = {
      inherit consortiumCli vmHostPackages probe;
    };
    modules = [
      inputs.microvm-nix.nixosModules.microvm
      ./guest.nix
    ];
  };

  # The readiness probe, as one binary instead of a cat/sync/cat pipeline: see
  # probe.c for why the execs are on the critical path.
  probe = guestPkgs.runCommandCC "fanout64-readiness-probe" { } ''
    mkdir -p $out/bin
    ${guestPkgs.stdenv.cc}/bin/cc \
      -O2 -Wall -Wextra -Werror -std=c11 \
      -o $out/bin/fanout-probe ${./probe.c}
  '';

  # microvm-run execs QEMU with a fixed argument list. Forward extra arguments
  # so the launcher can capture and restore snapshots (-snapshot, -incoming);
  # with no arguments the VM boots exactly as microvm.nix defines it.
  #
  # The NIC's address is also made per-launch, and has to be: QEMU realizes a
  # -device while it builds the machine, before the monitor is reachable, and
  # a realized device's properties are read-only, so a qom-set of the address
  # over QMP is refused. microvm.nix writes the image's mac straight into the
  # -device line, so the launch's own value is substituted in its place, from
  # the environment the launcher sets per VM. The default is the image's
  # address, so a launch that sets nothing still boots on it.
  #
  # microvm-restore is the same machine without -kernel/-initrd/-append. A
  # restore never boots, yet QEMU would read the kernel and initrd into
  # per-VM ROM blobs (~90 MB each) that only a guest reset uses.
  runner = vmHostPackages.runCommand "fanout64-microvm-run" { meta.mainProgram = "microvm-run"; } ''
    mkdir -p $out/bin
    # The per-launch MAC is expanded by the runner at exec time, so the device
    # token has to be double-quoted or the variable reaches QEMU as literal
    # text. Only the aarch64-darwin shape was being re-quoted: x86_64 emits
    # `virtio-net-device,netdev=net0,mac=<addr>` and ends the argument there,
    # so it kept its single quotes and QEMU rejected the address. Re-quote that
    # shape too. The mac substitution anchors on the key rather than on a
    # trailing comma, since only aarch64-darwin continues with `,romfile=`.
    sed -e 's/''${runtime_args:-}[[:space:]]*$/''${runtime_args:-} "$@"/' \
        -e "s|'virtio-net-pci,\(.*\),romfile='|\"virtio-net-pci,\\1,romfile=\"|" \
        -e "s|'virtio-net-device,\(.*\)'|\"virtio-net-device,\\1\"|" \
        -e 's|,mac=\([0-9a-fA-F:]*\)|,mac=''${FANOUT_NIC_MAC:-\1}|g' \
        ${lib.getExe' configuration.config.microvm.runner.qemu "microvm-run"} > $out/bin/microvm-run
    grep -q 'runtime_args:-} "\$@"$' $out/bin/microvm-run
    grep -q 'mac=''${FANOUT_NIC_MAC:-02:00:00:00:00:01}' $out/bin/microvm-run
    sed -E "s/ -kernel [^ ]+//; s/ -initrd [^ ]+//; s/ -append '[^']*'//" \
      $out/bin/microvm-run > $out/bin/microvm-restore
    if grep -qE -- ' -(kernel|initrd|append) ' $out/bin/microvm-restore; then
      echo "microvm-restore still boots a kernel" >&2
      exit 1
    fi
    grep -q 'runtime_args:-} "\$@"$' $out/bin/microvm-restore
    grep -q 'mac=''${FANOUT_NIC_MAC:-02:00:00:00:00:01}' $out/bin/microvm-restore
    chmod +x $out/bin/microvm-run $out/bin/microvm-restore
  '';
in
{
  inherit configuration guestSystem runner;
  probeBinary = "${probe}/bin/fanout-probe";
  guestMemMiB = configuration.config.microvm.mem;
  defaultPayload = guestPkgs.hello;
}
