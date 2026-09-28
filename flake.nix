{
  description = "consortium-tests — test infrastructure for consortium";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";

    # Declarative NixOS microVMs for the 64-node fanout64 benchmark. This repo
    # owns the fleet; `consortium` supplies the CLI that runs inside each guest
    # and the Rust library that parses the cascade.
    microvm-nix = {
      url = "github:astro/microvm.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    # The guest image embeds `consortium-cli` (for `cascade-copy`) and
    # exercises `consortium-nix` SSH-port parsing, so it needs the package
    # built for the GUEST system, not a path dependency.
    consortium = {
      url = "git+ssh://git@github.com/olivecasazza/consortium";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    inputs@{
      nixpkgs,
      microvm-nix,
      consortium,
      ...
    }:
    let
      lib = nixpkgs.lib;
      supportedSystems = [
        "aarch64-darwin"
        "x86_64-linux"
      ];

      perSystem =
        system:
        let
          pkgs = import nixpkgs { inherit system; };
          python = pkgs.python3;

          # The guest is aarch64-linux on an aarch64-darwin host (HVF) and
          # x86_64-linux otherwise. The CLI must match the GUEST system.
          guestSystem = if system == "aarch64-darwin" then "aarch64-linux" else "x86_64-linux";

          consortiumCli = consortium.packages.${guestSystem}.consortium-cli;

          fanout = import ./nix/fanout-vms {
            inherit inputs;
            hostSystem = system;
            inherit consortiumCli;
          };

          fanout64 = pkgs.writeShellApplication {
            name = "fanout64";
            runtimeInputs = [
              pkgs.nix
              pkgs.openssh
              python
              fanout.runner
              fanout.defaultPayload
            ];
            meta.description = "Launch N independent microVMs and benchmark log2 Nix closure distribution";
            text = ''
              exec ${python}/bin/python3 ${./nix/fanout-vms/bench.py} \
                --runner ${lib.getExe fanout.runner} \
                --restore-runner ${lib.getExe' fanout.runner "microvm-restore"} \
                --guest-mem-mib ${toString fanout.guestMemMiB} \
                --ssh-key ${./nix/fanout-vms/keys/id_fanout} \
                --store-path ${fanout.defaultPayload} \
                --expect-stdout 'Hello, world!\n' \
                "$@"
            '';
          };
        in
        {

          packages = {
            inherit fanout64;
            fanout64-guest = fanout.runner;
          };

          apps.fanout64 = {
            type = "app";
            program = lib.getExe fanout64;
            meta.description = "Launch N independent microVMs and benchmark log2 Nix closure distribution";
          };

          # Launcher safety tests. The benchmark itself needs KVM, but its
          # port-reservation and cleanup logic is pure Python and must not
          # regress unnoticed — a TIME_WAIT regression once made a second
          # consecutive 64-node run fail to bind its HTTP host ports.
          #
          # openssh is not optional here: the tests build real ssh command
          # lines, and host_ssh_command resolves the binary eagerly via
          # shutil.which. Without it the sandbox raises "ssh is not on PATH"
          # and three tests fail on the environment rather than on anything
          # they assert.
          checks.fanout-bench-test =
            pkgs.runCommand "consortium-fanout-bench-test"
              {
                nativeBuildInputs = [
                  python
                  pkgs.openssh
                ];
              }
              ''
                export PYTHONDONTWRITEBYTECODE=1
                cp -r ${./nix/fanout-vms} fanout-vms
                chmod -R u+w fanout-vms
                python -m unittest discover -s fanout-vms -p 'test_*.py' -v
                touch $out
              '';
        };

      forEach = attr: lib.genAttrs supportedSystems (system: (perSystem system).${attr});
    in
    {
      checks = forEach "checks";
      packages = forEach "packages";
      apps = forEach "apps";
    };
}
