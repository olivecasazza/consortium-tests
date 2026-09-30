{
  lib,
  pkgs,
  consortiumCli,
  probe,
  vmHostPackages,
  ...
}:

let
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
    inherit vmHostPackages;
    # Restored guests never boot, so a second vCPU buys no boot parallelism;
    # it only doubles the host threads 64 VMs contend for.
    vcpu = 1;
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
        # microvm.nix declares this option with no default and destructures it
        # when it builds the -device line, so an interface without one fails
        # evaluation. The value is the capture-time address, which is VM 1's:
        # the capture guest boots from this image. It is also the runner's
        # fallback, because each launch substitutes its own address into this
        # very mac= as QEMU builds the machine (see default.nix). An explicit
        # mac= here outranks a -global virtio-net-pci.mac= passed at launch,
        # and a -device is realized before the monitor is reachable, so the
        # address cannot be written onto the device afterwards over QMP.
        mac = "02:00:00:00:00:01";
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

    # 64 QEMU processes start inside the readiness window, and dynamic
    # loading dominates each start: the stock build maps 67 Nix dylibs
    # (spice, gstreamer, curl, iSCSI, smartcard, ...) none of which a
    # headless slirp microVM uses. `minimal` would also drop libslirp, so
    # switch features off individually.
    qemu.package =
      (vmHostPackages.qemu.override {
        hostCpuOnly = true;
        guestAgentSupport = false;
        spiceSupport = false;
        smartcardSupport = false;
        vncSupport = false;
        ncursesSupport = false;
        libiscsiSupport = false;
        capstoneSupport = false;
        tpmSupport = false;
        enableDocs = false;
      }).overrideAttrs
        (old: {
          buildInputs = lib.filter (
            input:
            !lib.elem (lib.getName input) [
              "curl"
              "vde2"
              "lzo"
              "snappy"
            ]
          ) old.buildInputs;
          configureFlags = old.configureFlags ++ [
            "--disable-curl"
            "--disable-vde"
            "--disable-lzo"
            "--disable-snappy"
          ];
        });
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

  # The address is per launch (see microvm.interfaces), so it cannot be a
  # stable match: a .network keyed on a MAC would stop matching the moment
  # bench.py hands this VM its own, and the guest would lose DHCP. The name is
  # pinned here instead of left to the kernel, which derives it from PCI slot
  # order, so the name is the same in the captured guest and in all 64
  # restores. The match is on device type and on ID_BUS, a udev property the
  # kernel emits when the device is registered, because this is a stage-1
  # rename: the driver need not be bound yet, and the address, the one
  # property that is certain, differs per launch. Property= is the [Match] key
  # that reads a udev property; there is no Bus= key to use instead. There is
  # exactly one NIC.
  systemd.network.links."10-fanout" = {
    matchConfig = {
      Type = "ether";
      Property = "ID_BUS=pci";
    };
    linkConfig.Name = "net0";
  };

  systemd.network.networks."10-user" = {
    matchConfig.Name = "net0";
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
  # Every readiness probe and relay hop is a root SSH login. Registering each
  # with logind (session scope + cgroup setup and teardown) is guest CPU that
  # 64 contending VMs pay on the critical path; the fleet has no interactive
  # users. The openssh module enables it with a plain value, hence mkForce.
  security.pam.services.sshd.startSession = lib.mkForce false;
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
      # The readiness probe, replacing the cat/sync/cat pipeline: one exec
      # instead of four per VM, and the probe tail is guest-vCPU bound.
      probe
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

  # The fleet restores from a snapshot captured after boot (bench.py). Zeroing
  # pages on free keeps the pre-capture cache drop from leaving stale data in
  # the captured RAM image.
  boot.kernelParams = [ "init_on_free=1" ];

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
