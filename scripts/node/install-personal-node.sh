#!/usr/bin/env bash
# Prepares a personal AstralDeep node: a private data directory, the node signing key, and a personal-profile env file outside the repository.
# Safe to run again. It keeps every existing value, adds only what is missing, and never prints a secret.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: install-personal-node.sh --issuer URL --client-id ID [options]

  --issuer URL         Keycloak realm URL the node trusts (required)
  --client-id ID       OIDC client id your Astral apps sign in with (required)
  --home DIR           Node home (default: $ASTRAL_NODE_HOME or ~/.astral-node)
  --owner-subject SUB  Bind the node to this Keycloak subject now instead of at first pairing
  --public-url URL     Origin your devices use to reach the node
  --database-url DSN   PostgreSQL connection string for the node
  -h, --help           Show this help
USAGE
}

issuer="" client_id="" owner_subject="" public_url="" database_url=""
home="${ASTRAL_NODE_HOME:-$HOME/.astral-node}"
while [ $# -gt 0 ]; do
  case "$1" in
    --issuer) issuer="${2:-}"; shift 2 ;;
    --client-id) client_id="${2:-}"; shift 2 ;;
    --home) home="${2:-}"; shift 2 ;;
    --owner-subject) owner_subject="${2:-}"; shift 2 ;;
    --public-url) public_url="${2:-}"; shift 2 ;;
    --database-url) database_url="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done
if [ -z "$issuer" ] || [ -z "$client_id" ]; then
  echo "--issuer and --client-id are required" >&2
  exit 2
fi
issuer="${issuer%/}"

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
touch "$env_file"
chmod 600 "$env_file"
add_value ASTRAL_NODE_PROFILE personal
add_value ASTRAL_NODE_DATA_DIR "$data_dir"
add_value KEYCLOAK_AUTHORITY "$issuer"
add_value KEYCLOAK_CLIENT_ID "$client_id"
add_value WEB_SESSION_ENC_KEY "$(random_fernet)"
add_value CREDENTIAL_ENCRYPTION_KEY "$(random_fernet)"
add_value AUDIT_HMAC_SECRET "$(random_hex)"
add_value AGENT_API_KEY "$(random_hex)"
[ -n "$public_url" ] && add_value PUBLIC_BASE_URL "${public_url%/}"
[ -n "$database_url" ] && add_value DATABASE_URL "$database_url"
if [ -n "$owner_subject" ]; then
  issuer_hash="$(printf '%s' "$issuer" | openssl dgst -sha256 -hex | sed 's/^.* //' | cut -c1-16)"
  add_value ASTRAL_NODE_OWNER "oidc.$issuer_hash.$owner_subject"
fi

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
echo
echo "Next steps"
echo "  1. Check the issuer, client id and database settings in $env_file."
if ! has_key DATABASE_URL; then
  echo "  2. Add DATABASE_URL for the node's PostgreSQL database to $env_file."
else
  echo "  2. DATABASE_URL is set."
fi
echo "  3. Start the orchestrator with that file, for example: docker run --env-file $env_file -v $data_dir:$data_dir -p 8001:8001 <image>"
echo "  4. Sign in from your app, then ask the node for a pairing code with POST /api/node/pairing."
echo "  5. Keep $data_dir and $env_file private and back them up. The signing key is the node's identity."
