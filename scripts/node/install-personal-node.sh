#!/usr/bin/env bash
# Prepares a personal AstralDeep node: a private data directory, the node signing key, and a personal-profile env file outside the repository.
# Safe to run again. It keeps every existing value, adds only what is missing, and never prints a secret.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: install-personal-node.sh [options]

  --home DIR           Node home (default: $ASTRAL_NODE_HOME or ~/.astral-node)
  --public-url URL     Origin your devices use to reach the node
  --database-url URL   Database URL (default: SQLite in the node data directory)
  -h, --help           Show this help

The node needs no sign-in. Its owner is the mesh you pair it with.
USAGE
}

removed_option() {
  case "$1" in
    --issuer|--issuer=*|--client-id|--client-id=*)
      echo "${1%%=*} was removed. A personal node needs no Keycloak issuer or client id. Pair it with your mesh instead (see the next steps this script prints)." >&2 ;;
    --owner-subject|--owner-subject=*)
      echo "${1%%=*} was removed. A personal node takes its owner from the mesh you pair it with, not from a sign-in subject." >&2 ;;
    *) return 1 ;;
  esac
}

public_url="" database_url=""
home="${ASTRAL_NODE_HOME:-$HOME/.astral-node}"
while [ $# -gt 0 ]; do
  case "$1" in
    --home) home="${2:-}"; shift 2 ;;
    --public-url) public_url="${2:-}"; shift 2 ;;
    --database-url) database_url="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) if removed_option "$1"; then exit 2; fi
       echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

umask 077
data_dir="$home/data"
env_file="$home/node.env"
key_file="$data_dir/node-ed25519.pem"
mkdir -p "$data_dir"
chmod 700 "$home" "$data_dir"

random_hex() { openssl rand -hex 32; }
random_fernet() { openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n'; }

has_key() { [ -f "$env_file" ] && grep -q "^$1=" "$env_file"; }
add_value() {
  if ! has_key "$1"; then
    printf '%s=%s\n' "$1" "$2" >>"$env_file"
    added+=("$1")
  fi
}

added=()
legacy_owner=""
if [ -f "$env_file" ]; then
  owner_line="$(grep '^ASTRAL_NODE_OWNER=' "$env_file" | head -n 1 || true)"
  case "${owner_line#ASTRAL_NODE_OWNER=}" in
    "" | sha256:????????????????????????????????????????????????????????????????) ;;
    mesh.????????????????????????????????????????????????????????????????) ;;
    *) legacy_owner=1 ;;
  esac
fi
touch "$env_file"
chmod 600 "$env_file"
add_value ASTRAL_NODE_PROFILE personal
add_value ASTRAL_NODE_DATA_DIR "$data_dir"
add_value WEB_SESSION_ENC_KEY "$(random_fernet)"
add_value CREDENTIAL_ENCRYPTION_KEY "$(random_fernet)"
add_value AUDIT_HMAC_SECRET "$(random_hex)"
add_value AGENT_API_KEY "$(random_hex)"
[ -n "$public_url" ] && add_value PUBLIC_BASE_URL "${public_url%/}"
[ -n "$database_url" ] && add_value DATABASE_URL "$database_url"
key_state="kept"
if [ ! -f "$key_file" ]; then
  if openssl genpkey -algorithm ED25519 -out "$key_file" 2>/dev/null; then
    chmod 600 "$key_file"
    key_state="generated"
  else
    rm -f "$key_file"
    key_state="deferred (the node creates it on first start)"
  fi
fi

echo "Personal node home: $home"
echo "Env file:           $env_file (mode 600)"
echo "Signing key:        $key_state"
if [ "${#added[@]}" -gt 0 ]; then
  echo "Added settings:     ${added[*]}"
else
  echo "Added settings:     none, everything was already present"
fi
if [ -n "$legacy_owner" ]; then
  echo "Legacy owner:       $env_file holds an ASTRAL_NODE_OWNER from the sign-in era. It is left as it is. The node reports it as a legacy owner and keeps your data."
fi
echo
echo "Next steps"
if ! has_key DATABASE_URL; then
  echo "  1. The node stores its data in SQLite at $data_dir/plane.sqlite3. Add DATABASE_URL to $env_file to use PostgreSQL instead."
else
  echo "  1. DATABASE_URL is set."
fi
echo "  2. Start the orchestrator with that file, for example: docker run --env-file $env_file -v $data_dir:$data_dir -p 8001:8001 <image>"
echo "  3. Pair it from the same machine. Read the secret in $data_dir/pairing-secret and call POST /api/node/mesh/offer on 127.0.0.1:8001 with it in the Astral-Pairing-Secret header."
echo "  4. Scan the code or payload it returns with your Mac or iPhone app and approve. The mesh you pair becomes the node's owner."
echo "  5. Keep $data_dir and $env_file private and back them up. The signing key is the node's identity."
