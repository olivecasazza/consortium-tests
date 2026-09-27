{
  lib,
  pkgs,
  consortiumCli,
  vmHostPackages,
  ...
}:

let
  guestMac = "02:00:00:00:00:01";
  busyboxHttpd = pkgs.busybox.override {
    extraConfig = ''
      CONFIG_HTTPD y
    '';
  };
  healthRoot = pkgs.writeTextDir "health" ''
    ready
  '';
  peerSshConfig = pkgs.writeText "fanout-peer-ssh-config" ''
    Host 10.0.2.2
      BatchMode yes
      IdentityFile /root/.ssh/id_ed25519
      IdentitiesOnly yes
      StrictHostKeyChecking no
      UserKnownHostsFile /dev/null
      LogLevel ERROR
  '';
in
{
  microvm = {
    hypervisor = "qemu";
    vmHostPackages = vmHostPackages;
    vcpu = 2;
    mem = 512;

    # The immutable guest closure is a disk, not a host-store share. Each
    # runner cwd gets its own persistent, sparse writable overlay image.
    storeOnDisk = true;
    writableStoreOverlay = "/nix/.rw-store";
    volumes = [
      {
        image = "overlay.img";
        label = "fanout-overlay";
        mountPoint = "/nix/.rw-store";
        size = 1024;
        fsType = "ext4";
      }
    ];
    shares = [ ];

    socket = "fanout.qmp";
    interfaces = [
      {
        type = "user";
        id = "net0";
        mac = guestMac;
      }
    ];
    forwardPorts = [ ];

    # Omit the SMBIOS UUID. Machined registration is disabled; its unused
    # fallback UUID remains deterministic from the shared hostname.
    machineId = null;
    registerWithMachined = false;

    # dev-ttyS0.device is the largest systemd-analyze blame entry. The
    # harness tails runner.log on the host, not the guest console, so the
    # emulated 16550 is pure boot latency.
    qemu.serialConsole = false;
  };

  networking = {
    hostName = "fanout64";
    useDHCP = false;
    useNetworkd = true;
    firewall.allowedTCPPorts = [
      22
      8080
    ];
  };

  systemd.network.networks."10-user" = {
    matchConfig.MACAddress = guestMac;
    networkConfig = {
      DHCP = "ipv4";
      IPv6AcceptRA = false;
    };
  };

  services.openssh = {
    enable = true;
    # Each guest generates its own fast host key; avoid 64 RSA-4096 keygens.
    hostKeys = [
      {
        path = "/etc/ssh/ssh_host_ed25519_key";
        type = "ed25519";
      }
    ];
    settings = {
      KbdInteractiveAuthentication = false;
      PasswordAuthentication = false;
      PermitRootLogin = "prohibit-password";
    };
  };
  users.users.root.openssh.authorizedKeys.keyFiles = [ ./keys/id_fanout.pub ];

  # TEST-ONLY fixture credentials for guest-to-guest SSH through the QEMU
  # gateway forwards. These keys must never be used outside this fleet.
  systemd.tmpfiles.rules = [
    "d /root/.ssh 0700 root root -"
    "C /root/.ssh/id_ed25519 0600 root root - ${./keys/id_fanout}"
    "C /root/.ssh/config 0600 root root - ${peerSshConfig}"
  ];

  nix = {
    enable = true;
    channel.enable = false;
    settings.experimental-features = [ "nix-command" ];
  };

  environment = {
    defaultPackages = lib.mkForce [ ];
    systemPackages = [
      consortiumCli
      busyboxHttpd
      pkgs.openssh
    ];
  };
  documentation.enable = false;

  # The guest is headless (SSH + HTTP only) and microvm.qemu.serialConsole
  # is false, so console/keymap setup is dead weight: it drags the whole
  # kbd closure (the second-largest package in the initrd by file count)
  # plus systemd-vconsole-setup into every one of 64 initrd extractions.
  console.enable = false;

  # REVERTED: disabling lvm + tpm2 shrank the initrd (2029 -> 1311 files,
  # 27.6 -> 20.4 MB) but made 64-node readiness WORSE: 13.15-14.00 s
  # before vs 15.06-16.20 s after, three runs each. A smaller initrd did
  # not translate into faster boots, so neither switch earns its keep.

  # 64 guests booting at once each log kernel+systemd to a serial chardev
  # that the harness redirects into a per-VM runner.log on the host disk.
  # Silencing the console removes that host-side write amplification; the
  # harness still surfaces failures via its own error messages and log tail.
  boot.consoleLogLevel = 0;
  boot.initrd.verbose = false;

  systemd.services.fanout-health = {
    description = "Fanout microVM health endpoint";
    wantedBy = [ "multi-user.target" ];
    after = [ "network.target" ];
    serviceConfig = {
      DynamicUser = true;
      ExecStart = "${busyboxHttpd}/bin/busybox httpd -f -p 8080 -h ${healthRoot}";
      Restart = "on-failure";
      RestartSec = "100ms";
    };
  };

  system.stateVersion = "25.05";
}
