#!/usr/bin/env bash
# fanout64 benchmark: 64 independent Nix microVMs, closure spread by log2 relay.
# Target: 64 nodes ready (SSH + HTTP health) in < 8 s on BOTH Linux (KVM) and
# Apple Silicon (HVF). This script lives beside the harness it runs, in this
# repository: nix/fanout-vms/ is the fleet, doc/fanout64.md describes it.
#
# Metrics (median of FANOUT_REPS runs per platform):
#   worst_readiness_s   max(linux, apple) readiness -- PRIMARY
#   <platform>_readiness_s / _deployment_s / _total_s
set -euo pipefail

HARNESS_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
REPO_ROOT=$(cd "$HARNESS_DIR/../.." && pwd -P)
# Default to the checkout this script was run from, so the driver and the
# harness it archives are the same tree unless told otherwise.
BASE_REPO=${FANOUT_BASE_REPO:-$REPO_ROOT}
# Pinned to a commit rather than to master: a local clone's master is whatever
# was last fetched, so a moving ref would silently archive an older tree, and
# a commit cannot drift.
#
# This pin and the entropy gate in the ok predicate below have to move
# together - the gate would fail against a harness that does not report the
# field.
BASE_REV=${FANOUT_BASE_REV:-d457226}
COUNT=${FANOUT_COUNT:-64}
# Safety cap only; readiness is timed independently of it. bench.py's 120 s
# default aborts oversubscribed hosts (Apple, 64 nodes) before any number.
DEADLINE=${FANOUT_STARTUP_DEADLINE:-600}
REPS=${FANOUT_REPS:-3}
PLATFORMS=${FANOUT_PLATFORMS:-linux apple}
LINUX_HOST=${FANOUT_LINUX_HOST:-root@pdx-nxst-001.schrodinger.com}
WORK=${FANOUT_WORK:-$HOME/.cache/fanout64-harness}
# One cold-boot rep per platform, outside the median, as the anchor the warm
# headline is read against. Set FANOUT_COLD_CONTROL=0 to skip it: cold boot is
# far slower than a restore, and it is reported, never gated.
COLD_CONTROL=${FANOUT_COLD_CONTROL:-1}
MAX_AMBIENT_LOAD=${FANOUT_MAX_AMBIENT_LOAD:-25}
# aarch64-linux builder for the Apple guests (nix/fanout-vms/linux-builder.nix).
BUILDER_KEY=/etc/nix/builder_ed25519
BUILDER_HOSTKEY_FILE=$HOME/.cache/linux-builder/hostkey
BUILDER_CORES=${FANOUT_BUILDER_CORES:-12}

log() { printf '[fanout64] %s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# Record the host's 1-minute load average for one Apple rep, appended to the
# series. Sampled per rep, not once before the fleet: measured on this host, a
# single pre-fleet sample of 24.4 (just under the ceiling) preceded a 1.35 s
# spread, because the conditions a rep is taken under are not the conditions
# the first one was. The aggregator reports min/median/max of the series.
sample_apple_load() {
  local load
  # vm.loadavg prints "{ 82.18 92.91 78.53 }" on Darwin: space separated, and
  # three numbers. Take the first field only, or the value is nonsense.
  load=$(sysctl -n vm.loadavg | tr -d '{}' | awk '{print $1}')
  printf '{"loadavg_1min": %s, "cores": %s}\n' "${load:-0}" "$(sysctl -n hw.ncpu)" \
    >>"$results/apple-load.jsonl"
  log "apple: ambient load ${load:-unknown} (cores $(sysctl -n hw.ncpu), ceiling $MAX_AMBIENT_LOAD)"
}

stage=$WORK/src
rm -rf "$stage"
mkdir -p "$stage" "$WORK/results"
# Resolve through symlinks before handing the path to nix. nix refuses a
# flake path containing one ("path '//tmp' is a symlink"), and /tmp is a
# symlink on Darwin, so the obvious FANOUT_WORK=/tmp/fanout fails only after
# the whole stage is built. pwd -P also gives the driver a stable path when
# WORK is reached through a symlinked home.
stage=$(cd "$stage" && pwd -P)
# A clean archive of the pinned commit. There is no overlay to lay over it:
# harness changes belong in consortium-tests, not in a config repo.
git -C "$BASE_REPO" archive "$BASE_REV" | tar -x -C "$stage"
log "staged $BASE_REV at $stage"

# Launcher safety tests (pure Python, cheap): a harness regression must not
# masquerade as a performance result.
python3 -m unittest discover -s "$stage/nix/fanout-vms" -p test_bench.py >"$WORK/unittest.log" 2>&1 ||
  { cat "$WORK/unittest.log" >&2; die "launcher unit tests failed"; }

results=$WORK/results/run-$(date +%s)
mkdir -p "$results"

run_linux() {
  log "linux: building on ${LINUX_HOST#*@}"
  local out
  out=$(nix build --no-link --print-out-paths --eval-store auto \
    --store "ssh-ng://$LINUX_HOST" "path:$stage#packages.x86_64-linux.fanout64" 2>"$results/linux-build.log" | tail -1) ||
    { tail -20 "$results/linux-build.log" >&2; die "linux build failed"; }
  [[ -n $out ]] || die "linux build produced no output path"
  for i in $(seq 1 "$REPS"); do
    log "linux: run $i/$REPS ($out)"
    ssh -o BatchMode=yes "$LINUX_HOST" "TMPDIR=/var/tmp $out/bin/fanout64 --count $COUNT --startup-deadline $DEADLINE" \
      >"$results/linux-$i.json" 2>"$results/linux-$i.err" || true
  done
  if [[ $COLD_CONTROL == 1 ]]; then
    log "linux: cold-boot control ($out)"
    ssh -o BatchMode=yes "$LINUX_HOST" "TMPDIR=/var/tmp $out/bin/fanout64 --count $COUNT --startup-deadline $DEADLINE --boot cold" \
      >"$results/linux-cold.json" 2>"$results/linux-cold.err" || true
  fi
}

run_apple() {
  [[ $(uname -sm) == "Darwin arm64" ]] || die "apple platform must run on an Apple Silicon host"
  /usr/bin/nc -z -G 3 127.0.0.1 31022 >/dev/null 2>&1 ||
    die "aarch64-linux builder not listening on :31022 (nix run -f $HARNESS_DIR/linux-builder.nix)"
  local hostkey builders out
  hostkey=$(base64 <"$BUILDER_HOSTKEY_FILE" | tr -d '\n')
  builders="ssh-ng://builder@localhost:31022 aarch64-linux $BUILDER_KEY $BUILDER_CORES 1 kvm,big-parallel - $hostkey"
  log "apple: building (aarch64-linux guests via local builder)"
  out=$(nix build --no-link --print-out-paths --builders "$builders" \
    "path:$stage#packages.aarch64-darwin.fanout64" 2>"$results/apple-build.log" | tail -1) ||
    { tail -20 "$results/apple-build.log" >&2; die "apple build failed"; }
  [[ -n $out ]] || die "apple build produced no output path"
  mkdir -p "$WORK/tmp"
  for i in $(seq 1 "$REPS"); do
    # Sampled per rep, never immediately after the build: the Apple leg
    # cross-builds the aarch64-linux guest closure through a 12-core builder,
    # and a 1-minute average taken then reports our own build rather than the
    # host. Measured on an otherwise-idle machine here: 9.32 at rest, 69.62
    # right after the build, which would flag every Apple batch degraded.
    sample_apple_load
    log "apple: run $i/$REPS ($out)"
    TMPDIR=$WORK/tmp "$out/bin/fanout64" --count "$COUNT" --startup-deadline "$DEADLINE" \
      >"$results/apple-$i.json" 2>"$results/apple-$i.err" || true
  done
  if [[ $COLD_CONTROL == 1 ]]; then
    log "apple: cold-boot control ($out)"
    TMPDIR=$WORK/tmp "$out/bin/fanout64" --count "$COUNT" --startup-deadline "$DEADLINE" --boot cold \
      >"$results/apple-cold.json" 2>"$results/apple-cold.err" || true
  fi
}

for p in $PLATFORMS; do
  case $p in
    linux) run_linux ;;
    apple) run_apple ;;
    *) die "unknown platform $p" ;;
  esac
done

python3 - "$results" "$COUNT" "$REPS" "$MAX_AMBIENT_LOAD" $PLATFORMS <<'PY'
import json, statistics, sys
from pathlib import Path

results, count, reps = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
max_load, platforms = float(sys.argv[4]), sys.argv[5:]
failed, worst, degraded = [], 0.0, []
for p in platforms:
    rows = []
    for i in range(1, reps + 1):
        f = results / f"{p}-{i}.json"
        lines = [l for l in f.read_text().splitlines() if l.startswith("{")] if f.exists() else []
        r = json.loads(lines[-1]) if lines else None
        s = (r or {}).get("statuses", {})
        # The goal is 64 nodes ready in under READINESS_TARGET_S on both
        # platforms. Every per-run invocation above ends in `|| true` so one
        # missed rep still lets the rest run, which means bench.py's exit code
        # is discarded and this predicate is the only gate. Without
        # ready_within_target a 45 s readiness run scored as a clean pass and
        # printed worst_readiness_s=45.000. state_isolation_verified is the
        # canary that every restore's copy-on-write isolation rests on, and
        # entropy_isolation_verified is the one it cannot see: a guest drawing
        # the same bytes as its neighbours is not a cross-node write leak, so
        # every other field would stay green.
        ok = (r is not None and r.get("count") == count
              and r.get("ready_within_target") is True
              and s.get("ssh_ready") == count and s.get("http_ready") == count
              and s.get("ssh_data_exchange_verified") == count
              and s.get("state_isolation_verified") == count
              and s.get("entropy_isolation_verified") == count
              and s.get("store_paths_verified") == count
              and s.get("payload_executions_verified") == count)
        if not ok:
            err = (results / f"{p}-{i}.err")
            tail = err.read_text().strip().splitlines()[-3:] if err.exists() else []
            failed.append(f"{p} run {i}: " + (" | ".join(tail) or "no result"))
            continue
        rows.append(r)
    if len(rows) != reps:
        continue
    ready = statistics.median(r["readiness_s"] for r in rows)
    # The median of 3 under a contended host is dominated by whichever reps the
    # machine was busy for. The min is the steadiest estimate of what the
    # harness can do, and is reported next to the median rather than instead.
    best = min(r["readiness_s"] for r in rows)
    deploy = statistics.median(r["deployment_s"] for r in rows)
    total = statistics.median(r["readiness_s"] + r["deployment_s"] for r in rows)
    spread = max(r["readiness_s"] for r in rows) - min(r["readiness_s"] for r in rows)
    worst = max(worst, ready)
    print(f"METRIC {p}_readiness_s={ready:.3f}")
    print(f"METRIC {p}_readiness_min_s={best:.3f}")
    print(f"METRIC {p}_deployment_s={deploy:.3f}")
    print(f"METRIC {p}_total_s={total:.3f}")
    print(f"METRIC {p}_readiness_spread_s={spread:.3f}")
    # The conditions the numbers above were taken under. A shared Mac whose
    # ambient load is far above its core count cannot resolve a sub-second
    # change, so say so rather than let the median be read as a capability.
    if p == "apple":
        loads = []
        f = results / "apple-load.jsonl"
        if f.exists():
            for line in f.read_text().splitlines():
                try:
                    loads.append(json.loads(line)["loadavg_1min"])
                except (json.JSONDecodeError, KeyError):
                    pass
        if not loads:
            print(f"METRIC {p}_ambient_load=unknown")
        else:
            # The series, and its worst, because one pre-fleet sample does not
            # describe the conditions every rep was taken under: measured here,
            # a single sample of 24.4 (just under the ceiling) preceded a 1.35 s
            # spread across three reps.
            print(f"METRIC {p}_ambient_load={statistics.median(loads):.2f}")
            print(f"METRIC {p}_ambient_load_max={max(loads):.2f}")
            print(f"METRIC {p}_ambient_load_samples={len(loads)}")
            if max(loads) > max_load:
                degraded.append(
                    f"{p} reps taken up to ambient load {max(loads):.1f} over a "
                    f"{max_load:.0f} ceiling; not comparable to a quiet host"
                )
    # The headline above is warm-cache by construction: with the default
    # FANOUT_REPS=3 only rep 1 pays a snapshot capture, and ensure_snapshot
    # returns None on a hit, so reps 2-3 report snapshot_capture_s null and
    # never boot a kernel. Disclose that, so the number cannot be read as a
    # boot time, and report the median capture of the reps that did pay one.
    print(f"METRIC {p}_boot={rows[0]['boot']}")
    print(f"METRIC {p}_warm_cache_reps={sum(1 for r in rows if r.get('snapshot_capture_s') is None)}")
    captures = [r["snapshot_capture_s"] for r in rows if r.get("snapshot_capture_s") is not None]
    if captures:
        print(f"METRIC {p}_snapshot_capture_s={statistics.median(captures):.3f}")

# Cold-boot control: one rep per platform, outside the median, in its own
# results file. It is the anchor the warm number is read against -- without it
# a regression that adds seconds to guest boot (a new unit, a slower initrd, a
# changed volume) cannot move any metric at all. Deliberately not gated on
# ready_within_target: cold boot is a different thing from the restore path the
# 8 s goal is about, so it is reported, not passed or failed.
for p in platforms:
    f = results / f"{p}-cold.json"
    lines = [l for l in f.read_text().splitlines() if l.startswith("{")] if f.exists() else []
    r = json.loads(lines[-1]) if lines else None
    if r is None:
        print(f"METRIC {p}_cold_control=missing")
        continue
    s = r.get("statuses", {})
    complete = (r.get("count") == count
                and s.get("ssh_data_exchange_verified") == count
                and s.get("state_isolation_verified") == count
                and s.get("entropy_isolation_verified") == count
                and s.get("payload_executions_verified") == count)
    print(f"METRIC {p}_cold_control={'ok' if complete else 'incomplete'}")
    print(f"METRIC {p}_cold_boot_readiness_s={r['readiness_s']:.3f}")
for note in degraded:
    print(f"[fanout64] DEGRADED {note}", file=sys.stderr)
if failed:
    for f in failed:
        print(f"[fanout64] FAILED {f}", file=sys.stderr)
    sys.exit(1)
print(f"METRIC worst_readiness_s={worst:.3f}")
PY
