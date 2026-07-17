#!/usr/bin/env bash
# sync_upstream_tests.sh — fetch tests + lib from upstream ClusterShell at a pinned ref.
#
# Usage:
#   ./harness/sync_upstream_tests.sh              # uses ref from UPSTREAM_REF
#   ./harness/sync_upstream_tests.sh v1.9.3       # explicit ref
#   ./harness/sync_upstream_tests.sh HEAD          # latest master
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UPSTREAM_REPO="cea-hpc/clustershell"

# Determine ref
if [[ $# -ge 1 ]]; then
    REF="$1"
else
    REF="$(cat "$REPO_ROOT/UPSTREAM_REF" 2>/dev/null || echo "v1.9.3")"
fi

echo "==> Syncing upstream tests from $UPSTREAM_REPO @ $REF"

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

# Download tarball
TARBALL_URL="https://github.com/$UPSTREAM_REPO/archive/refs/tags/$REF.tar.gz"
# Fall back to commit URL if tag download fails
if ! curl -fsSL "$TARBALL_URL" -o "$TMPDIR/upstream.tar.gz" 2>/dev/null; then
    TARBALL_URL="https://github.com/$UPSTREAM_REPO/archive/$REF.tar.gz"
    curl -fsSL "$TARBALL_URL" -o "$TMPDIR/upstream.tar.gz"
fi

echo "==> Downloaded tarball"

# Extract
tar -xzf "$TMPDIR/upstream.tar.gz" -C "$TMPDIR"
EXTRACTED_DIR="$(ls -d "$TMPDIR"/clustershell-* | head -1)"

# Sync tests/
echo "==> Syncing tests/"
rsync -a --delete \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    "$EXTRACTED_DIR/tests/" "$REPO_ROOT/tests/"

# Sync lib/ClusterShell/ (the original Python implementation — our oracle)
echo "==> Syncing lib/ClusterShell/"
rsync -a --delete \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    "$EXTRACTED_DIR/lib/ClusterShell/" "$REPO_ROOT/lib/ClusterShell/"

# Update pinned ref
echo "$REF" > "$REPO_ROOT/UPSTREAM_REF"

# Report
NTEST_FILES=$(find "$REPO_ROOT/tests" -name '*Test.py' | wc -l | tr -d ' ')
NTEST_METHODS=$(grep -rh 'def test' "$REPO_ROOT/tests/"*Test.py 2>/dev/null | wc -l | tr -d ' ')
echo "==> Synced $NTEST_FILES test files with $NTEST_METHODS test methods from $UPSTREAM_REPO @ $REF"
