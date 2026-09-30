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
    # built for the GUEST system, not a path dependency. Pinned to 850247da
    # ("accept SSH ports in cascade source addresses"), not yet on master:
    # relay sources are addressed as root@10.0.2.2:<port>, and without it
    # every guest-sourced hop fails (only seed -> 2 children land).
    consortium = {
      # HTTPS, not SSH: Nix Checks runs on a GitHub-hosted runner that holds no
      # key for this host, and the repo is public, so the fetch must not need
      # one. The ref/rev pin is what matters; the transport is incidental.
      url = "git+https://github.com/olivecasazza/consortium?ref=feat/cascade-copy-jsonl&rev=98b99542536accb73b5234cca1f631d73eebd554";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    # Claude Code plugin marketplace of hand-crafted agent skills, vendored at
    # an exact rev: the catalog is part of the dev environment, so it has to be
    # reproducible rather than "whatever main happens to be today". HTTPS, not
    # SSH, for the same reason as `consortium` — Nix Checks runs on a
    # GitHub-hosted runner that holds no key for this host. This repo has no
    # flake.nix of its own, so the input contributes a source tree only and has
    # no nixpkgs input to follow.
    context-engineering-kit = {
      url = "git+https://github.com/NeoLabHQ/context-engineering-kit?rev=23e2428e809d77717f8acc9659c374a3a1fcb93e";
      # No flake.nix upstream: the catalog is consumed as a plain source tree.
      flake = false;
    };

  };

  outputs =
    inputs@{
      nixpkgs,
      consortium,
      context-engineering-kit,
      ...
    }:
    let
      inherit (nixpkgs) lib;
      supportedSystems = [
        "aarch64-darwin"
        "x86_64-linux"
      ];

      perSystem =
        system:
        let
          pkgs = import nixpkgs { inherit system; };
          python = pkgs.python3;

          # Interpreter for the dev shell: the parity suite's pytest
          # dependencies, which the repo's broken `.venv` no longer provides.
          pythonEnv = pkgs.python3.withPackages (ps: [
            ps.pytest
            ps.pytest-timeout
            ps.pyyaml
          ]);

          # The guest is aarch64-linux on an aarch64-darwin host (HVF) and
          # x86_64-linux otherwise. The CLI must match the GUEST system.
          guestSystem = if system == "aarch64-darwin" then "aarch64-linux" else "x86_64-linux";

          # consortium's own suite runs in its CI; its checkPhase has a
          # BrokenPipe race (molt_tests::test_010_axis_edge_cases) that
          # otherwise fails guest builds nondeterministically.
          consortiumCli = consortium.packages.${guestSystem}.consortium-cli.overrideAttrs {
            doCheck = false;
          };

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
                --ready-probe-binary ${fanout.probeBinary} \
                --cascade-tree-module ${./nix/fanout-vms/cascade_tree.py} \
                "$@"
            '';
          };
          # Skill catalog harvested from the pinned context-engineering-kit.
          # `$out` is a directory of skill directories whose names equal their
          # SKILL.md `name:` frontmatter value (never the source directory
          # name), each holding SKILL.md plus its `agents assets examples
          # references scripts tests` resource dirs — the same layout
          # consortium's `packages.skills` produces, so a consumer merging the
          # two catalogs sees one shape. The `test -n` guard fails the build
          # rather than silently shipping a skill under the literal name "".
          skills = pkgs.runCommand "context-engineering-skills" { nativeBuildInputs = [ pkgs.findutils ]; } ''
            mkdir -p $out
            for skill_file in ${context-engineering-kit}/plugins/*/skills/*/SKILL.md; do
              source_dir="$(dirname "$skill_file")"
              skill_name="$(sed -n 's/^name:[[:space:]]*//p' "$skill_file" | head -n1)"
              test -n "$skill_name"
              destination="$out/$skill_name"
              mkdir -p "$destination"
              find "$source_dir" -maxdepth 1 -type f -exec cp {} "$destination/" \;
              for resource in agents assets examples references scripts tests; do
                if test -d "$source_dir/$resource"; then
                  cp -rL "$source_dir/$resource" "$destination/$resource"
                fi
              done
            done
          '';

        in
        {

          packages = {
            inherit fanout64 skills;
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
          devShells.default = pkgs.mkShell {
            # The parity suite is pytest against the vendored oracle in `lib/`,
            # and the integration layer is cargo against the sibling consortium
            # checkout. This repo's own `.venv` is not usable as a baseline
            # (it points at an interpreter that no longer exists), so the
            # shellHook below puts a working interpreter on PATH.
            packages = [
              pkgs.cargo
              pkgs.nix
            ];

            shellHook = ''
              # The Python environment is put on PATH rather than passed in
              # `packages`: a PythonEnvironment is a spliced package, and this
              # nixpkgs' nativeBuildInputs dependency check rejects it outright
              # ("Dependency is not of a valid type"). `inputsFrom` takes it
              # but never puts its bin/ on PATH, so pytest stays unimportable.
              export PATH="${pythonEnv}/bin:$PATH"

              # The vendored catalog is consumed straight out of the store:
              # every $out/<name> is symlinked into this repo's own agent skills
              # directory, .agents/skills, which is where the committed
              # upstream-sync-watch skill already lives — so an agent working
              # here discovers them without a second, global catalog. Links,
              # not copies: the pinned rev stays the single source of truth and
              # re-entering the shell re-points them after a rebuild. A real
              # directory squatting on a skill name is left alone, not clobbered.
              if test -f "$PWD/pyproject.toml" && test -d "$PWD/tests"; then
                skill_dir="$PWD/.agents/skills"
                mkdir -p "$skill_dir"
                linked=0
                for skill in ${skills}/*; do
                  test -d "$skill" || continue
                  name="$(basename "$skill")"
                  target="$skill_dir/$name"
                  if test -L "$target"; then
                    ln -sfn "$skill" "$target"
                  elif test -e "$target"; then
                    echo "  $name: $target exists and is not a symlink, left alone" >&2
                    continue
                  else
                    ln -s "$skill" "$target"
                  fi
                  linked=$((linked + 1))
                done

                echo ""
                echo "  consortium-tests dev shell"
                echo "  ─────────────────────────────────────────"
                echo "  $linked vendored skills linked into .agents/skills"
                echo "  python -m pytest tests/  — upstream parity suite (oracle: PYTHONPATH=lib)"
                echo "  cargo test -p consortium-integration-tests --features docker-tests"
                echo "                            — Docker integration layer"
                echo ""
              else
                echo "  nix develop: not in the consortium-tests checkout, skill links skipped" >&2
              fi
            '';
          };

        };

      forEach = attr: lib.genAttrs supportedSystems (system: (perSystem system).${attr});
    in
    {
      checks = forEach "checks";
      packages = forEach "packages";
      devShells = forEach "devShells";
      apps = forEach "apps";
    };
}
