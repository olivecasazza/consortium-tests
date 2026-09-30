{
  description = "POC: stack VM on GPU via wgpu, vs arXiv:2608.16387";
  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" ];
      forAll = f: nixpkgs.lib.genAttrs systems (s: f nixpkgs.legacyPackages.${s});
    in {
      packages = forAll (pkgs:
        let
          p = pkgs.rustPlatform.buildRustPackage {
            pname = "vm-gpu"; version = "0.1.0";
            src = ./.;
            cargoLock.lockFile = ./Cargo.lock;
            buildAndTestSubdir = "src";
            doCheck = false;  # cargo test needs a real GPU; the nix sandbox has none
          };
        in { default = p; vm-gpu = p; });
    };
}
