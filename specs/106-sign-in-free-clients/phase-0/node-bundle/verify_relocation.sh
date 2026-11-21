#!/bin/bash
# Copies a built bundle to a new path with a space in it, moves the original away and runs relocation_check.py with a PATH that holds only vmmap.
# Usage: verify_relocation.sh <work-dir>. It restores the original bundle directory afterwards.
set -euo pipefail

WORK=${1:?work directory}
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$WORK/relocated copy"
rm -rf "$TARGET"
mkdir -p "$TARGET"
cp -Rp "$WORK/bundle" "$TARGET/bundle"
mv "$WORK/bundle" "$WORK/bundle.moved"
TOOLS="$(mktemp -d)"
ln -s /usr/bin/vmmap "$TOOLS/vmmap"
trap 'mv "$WORK/bundle.moved" "$WORK/bundle"; rm -rf "$TOOLS"' EXIT
env -i HOME="$TARGET" PATH="$TOOLS" \
  "$TARGET/bundle/python/bin/python3" -I -B "$HERE/relocation_check.py"
