#!/bin/bash
# Builds the macOS arm64 node bundle from python-build-standalone, the repo's locked requirements, the pinned components, Homebrew bottles and the spaCy model.
# Usage: build_bundle.sh <components-checkout> <work-dir>. Output is <work-dir>/bundle plus a tar.zst and the lock files, and nothing is written inside the repository.
set -euo pipefail

COMPONENTS_SRC=${1:?components checkout path}
WORK=${2:?work directory}
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../../.." && pwd)"
PBS_TAG=20261110
PBS_FILE=cpython-3.11.18+${PBS_TAG}-aarch64-apple-darwin-install_only.tar.gz
PBS_SHA256=73f1ed14486aa278d331e95f7d6d452f5cfb69d90b418e26e62fd4846d53557d
RG_VERSION=15.2.0
RG_SHA256=3750b2e93f37e0c692657da574d7019a101c0084da05a790c83fd335bad973e4
MODEL_VERSION=3.8.0
MODEL_URL=https://github.com/explosion/spacy-models/releases/download/en_core_web_lg-${MODEL_VERSION}/en_core_web_lg-${MODEL_VERSION}-py3-none-any.whl
MODEL_SHA256=293e9547a655b25499198ab15a525b05b9407a75f10255e405e8c3854329ab63
export HOMEBREW_NO_ENV_HINTS=1 HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_CACHE="$WORK/brew-cache"

TOOLS_PATH="$PATH"
BUNDLE="$WORK/bundle"
DL="$WORK/dl"
SRC="$WORK/root"
KERNEL_TARGET="$WORK/kernel-target"
rm -rf "$BUNDLE" "$SRC" "$WORK/wheels" "$WORK/kernel-stage"
mkdir -p "$BUNDLE" "$DL" "$SRC/config" "$SRC/scripts" "$WORK/kernel-stage"
stamp() { date +%s; }
START=$(stamp)
step() { echo "[$(( $(stamp) - START ))s] $*" >&2; }

check_sha() { [ "$(shasum -a 256 "$1" | cut -d' ' -f1)" = "$2" ] || { echo "digest mismatch for $1" >&2; exit 1; }; }

step "python-build-standalone ${PBS_TAG}"
[ -f "$DL/pbs.tgz" ] || curl -fsSL -o "$DL/pbs.tgz" "https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/${PBS_FILE//+/%2B}"
check_sha "$DL/pbs.tgz" "$PBS_SHA256"
tar xzf "$DL/pbs.tgz" -C "$BUNDLE"
PY="$BUNDLE/python/bin/python3"
export PATH="/usr/bin:/bin"
export PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1

step "component sources at the composition pins"
cp "$REPO/pyproject.toml" "$REPO/.gitmodules" "$SRC/"
cp "$REPO/config/astral-composition.json" "$SRC/config/"
cp "$REPO/scripts/install_local_components.py" "$SRC/scripts/"
"$PY" - "$SRC/config/astral-composition.json" <<'PYEOF' | while read -r path commit; do
import json, sys
for entry in json.load(open(sys.argv[1]))["components"].values():
    print(entry["path"], entry["commit"])
PYEOF
  mkdir -p "$SRC/$path"
  git -C "$COMPONENTS_SRC/$(basename "$path")" archive "$commit" | tar x -C "$SRC/$path"
done

step "build tools and locked requirements"
"$PY" -m pip install setuptools==83.0.0 wheel==0.45.1 hatchling==1.27.0 uv_build==0.13.6
"$PY" -m pip install --report "$WORK/pip-report.json" \
  -r "$REPO/backend/requirements.txt" -r "$REPO/backend/requirements-document-worker.txt"

step "components"
"$PY" "$SRC/scripts/install_local_components.py" build --root "$SRC" --wheel-dir "$WORK/wheels" --lock "$WORK/wheels/astral-component-wheels.lock.json"
"$PY" "$SRC/scripts/install_local_components.py" install --root "$SRC" --lock "$WORK/wheels/astral-component-wheels.lock.json"
"$PY" "$SRC/scripts/install_local_components.py" verify --root "$SRC" --lock "$WORK/wheels/astral-component-wheels.lock.json"

step "spaCy model"
[ -f "$DL/model.whl" ] || curl -fsSL -o "$DL/model.whl" "$MODEL_URL"
check_sha "$DL/model.whl" "$MODEL_SHA256"
cp "$DL/model.whl" "$DL/en_core_web_lg-${MODEL_VERSION}-py3-none-any.whl"
"$PY" -m pip install --no-deps "$DL/en_core_web_lg-${MODEL_VERSION}-py3-none-any.whl"

step "kernel wheel and mailbox for macOS arm64"
KERNEL="$SRC/components/AstralKernel"
(
  cd "$KERNEL"
  export PATH="$TOOLS_PATH" CARGO_TARGET_DIR="$KERNEL_TARGET"
  export RUSTFLAGS="--remap-path-prefix=$HOME/.cargo=/cargo --remap-path-prefix=$KERNEL=/kernel --remap-path-prefix=$KERNEL_TARGET=/target"
  cargo build --release --locked --offline --target aarch64-apple-darwin -p astral-kernel-ffi -p uniffi-bindgen -p astral-mailbox
  RELEASE="$KERNEL_TARGET/aarch64-apple-darwin/release"
  "$RELEASE/uniffi-bindgen" generate --library "$RELEASE/libastral_kernel_ffi.dylib" --language python --out-dir "$WORK/kernel-stage"
  cp "$RELEASE/libastral_kernel_ffi.dylib" "$WORK/kernel-stage/"
  "$PY" scripts/package_wheel.py --stage "$WORK/kernel-stage" --out "$WORK/wheels" --platform-tag macosx_14_0_arm64 --library-name libastral_kernel_ffi.dylib
)
"$PY" -m pip install --no-deps "$WORK"/wheels/astral_kernel-*.whl
"$PY" -m pip uninstall -y hatchling uv_build trove-classifiers wheel
"$PY" -m pip check

step "native binaries and libraries"
PATH="$TOOLS_PATH" "$PY" "$HERE/collect_native.py" --out "$BUNDLE/native" --staging "$WORK/brew-stage" --manifest "$WORK/native-manifest.json"
cp "$KERNEL_TARGET/aarch64-apple-darwin/release/astral-mailbox" "$BUNDLE/native/bin/astral-mailbox"
codesign --force --sign - "$BUNDLE/native/bin/astral-mailbox"
[ -f "$DL/rg.tgz" ] || curl -fsSL -o "$DL/rg.tgz" "https://github.com/BurntSushi/ripgrep/releases/download/${RG_VERSION}/ripgrep-${RG_VERSION}-aarch64-apple-darwin.tar.gz"
check_sha "$DL/rg.tgz" "$RG_SHA256"
tar xzf "$DL/rg.tgz" -C "$WORK" --strip-components=1 "ripgrep-${RG_VERSION}-aarch64-apple-darwin/rg"
install -m 755 "$WORK/rg" "$BUNDLE/native/bin/rg"

step "node source and shim"
mkdir -p "$BUNDLE/app"
rsync -a --exclude tests --exclude __pycache__ --exclude /data --exclude /tmp "$REPO/backend/" "$BUNDLE/app/backend/"
cp "$REPO/config/astral-kernel-binding.json" "$BUNDLE/app/astral-kernel-binding.json"
SITE="$("$PY" -c 'import sysconfig;print(sysconfig.get_paths()["purelib"])')"
cp "$HERE/astral_bundle_paths.py" "$SITE/astral_bundle_paths.py"
echo "import astral_bundle_paths" > "$SITE/astral_bundle_paths.pth"
"$PY" -m compileall -q -j 4 "$BUNDLE/python/lib" "$BUNDLE/app" >/dev/null || echo "compileall skipped files that do not compile" >&2
find "$BUNDLE/python/bin" -type f -exec grep -l "^#!.*python" {} + | xargs rm -f

step "locks"
"$PY" - "$WORK/pip-report.json" "$WORK/locked-requirements.macos-arm64.txt" <<'PY'
import json, sys
report = json.load(open(sys.argv[1]))
lines = []
for item in sorted(report["install"], key=lambda entry: entry["metadata"]["name"].lower()):
    info = item["download_info"]["archive_info"]
    digest = info.get("hashes", {}).get("sha256") or info.get("hash", "").removeprefix("sha256=")
    lines.append(f'{item["metadata"]["name"]}=={item["metadata"]["version"]} --hash=sha256:{digest}')
open(sys.argv[2], "w").write("\n".join(lines) + "\n")
PY
step "bundle ready"

step "archive"
(cd "$WORK" && tar -cf - bundle | PATH="$TOOLS_PATH" zstd -q -19 -T0 -o astral-node-bundle-macos-arm64.tar.zst -f)
step "done"
