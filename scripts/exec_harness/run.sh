#!/usr/bin/env bash
# Linux test harness for the execution backends: runs pytest as uid 1000 in a throwaway container, never a product path.
# Usage: run.sh [--repo DIR] [--exec DIR] [--pip FILE] [--cov FILE] -- PYTEST_ARGS (test paths are relative to the repo root)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
exec_dir=""
pip_file=""
cov_file=""

while [ $# -gt 0 ]; do
  case "$1" in
    --repo) repo="$(cd "$2" && pwd)"; shift 2 ;;
    --exec) exec_dir="$(cd "$2" && pwd)"; shift 2 ;;
    --pip) pip_file="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"; shift 2 ;;
    --cov) cov_file="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"; shift 2 ;;
    --) shift; break ;;
    *) echo "run.sh: unknown option $1" >&2; exit 2 ;;
  esac
done
[ $# -gt 0 ] || { echo "run.sh: name the pytest arguments after --" >&2; exit 2; }

tag="$(shasum -a 256 "$here/Dockerfile" | cut -c1-12)"
image="astral-exec-harness:$tag"
if ! docker image inspect "$image" >/dev/null 2>&1; then
  docker build --quiet --tag "$image" "$here" >/dev/null
fi

name="astral-exec-harness-$$"
trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT INT TERM

args=(run --rm --name "$name" --user 1000:1000
  --security-opt seccomp=unconfined --security-opt apparmor=unconfined --security-opt systempaths=unconfined
  -v "$repo:/src:ro" -e PYTHONDONTWRITEBYTECODE=1 -e ASTRAL_EXEC_HARNESS=1 -w /src)

path="/src/backend"
source_dir="/src/backend/astral_exec"
if [ -n "$exec_dir" ]; then
  args+=(-v "$exec_dir:/exec/astral_exec:ro")
  path="/exec:$path"
  source_dir="/exec/astral_exec"
fi
if [ -n "$pip_file" ]; then
  args+=(-v "$pip_file:/harness/requirements.txt:ro")
  path="/tmp/pipdeps:$path"
fi
runner=(python -m pytest)
if [ -n "$cov_file" ]; then
  args+=(-v "$(dirname "$cov_file"):/cov")
  runner=(python -m coverage run --rcfile=/dev/null "--data-file=/cov/$(basename "$cov_file")" "--source=$source_dir" -m pytest)
fi
args+=(-e "PYTHONPATH=$path")

script='if [ -f /harness/requirements.txt ]; then pip install --quiet --target /tmp/pipdeps -r /harness/requirements.txt; fi; exec "$@"'
docker "${args[@]}" "$image" sh -c "$script" sh "${runner[@]}" --noconftest -c /dev/null -p no:cacheprovider "$@"
