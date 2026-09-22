#!/usr/bin/env bash
# Linux CI only. Every container/database is disposable; no host .env is used.
# Boot smoke is structural. Real login, LETS-enforce and enabled media still
# require protected staging before any backend/web production-readiness verdict.
set -euo pipefail
image="${1:?immutable local image ID required}"
phase="${2:-tests}"
[[ "$phase" == tests || "$phase" == boot ]]
[[ "$image" =~ ^sha256:[a-f0-9]{64}$ ]]
root="$(pwd)"
policy_root="$(cd "${ASTRAL_GATE_POLICY_ROOT:-$root}" && pwd -P)"
test -f "$policy_root/scripts/run_backend_web_tests.py"
test -f "$policy_root/scripts/backend_web_test_reporter.py"
test -f "$policy_root/tooling/backend-ci/requirements.lock.txt"
output="$root/build/backend-web"
mkdir -p "$output"
namespace="${ASTRAL_GATE_NAMESPACE:-ad-gates-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}-$$}"
[[ "$namespace" =~ ^ad-(bwq-)?gates-[a-z0-9-]+$ ]]
environment="$(mktemp)"
cleanup() {
  docker logs "$namespace-app" > "$output/boot.log" 2>&1 || true
  docker logs "$namespace-dev" > "$output/boot-development.log" 2>&1 || true
  docker rm -fv "$namespace-test" "$namespace-app" "$namespace-dev" "$namespace-negative" "$namespace-pg" >/dev/null 2>&1 || true
  docker network rm "$namespace" >/dev/null 2>&1 || true
  rm -f "$environment"
}
trap cleanup EXIT
docker network create --label "org.astraldeep.qualification.namespace=$namespace" "$namespace" >/dev/null
docker run -d --name "$namespace-pg" --network "$namespace" --cpus=2 --memory=2g \
  --label "org.astraldeep.qualification.namespace=$namespace" \
  --env POSTGRES_USER=astral --env POSTGRES_PASSWORD=isolated_ci_only \
  --env POSTGRES_DB=ad_gate_tests \
  postgres:17-alpine@sha256:dc17045ccfd343b49600570ea734b9c4991cf1c3f3302e67df51e3b402dd55c4 >/dev/null
for attempt in $(seq 1 60); do
  if docker exec "$namespace-pg" pg_isready -U astral -d ad_gate_tests >/dev/null 2>&1; then break; fi
  sleep 1
done
docker exec "$namespace-pg" pg_isready -U astral -d ad_gate_tests
docker exec "$namespace-pg" createdb -U astral ad_gate_smoke
docker exec "$namespace-pg" createdb -U astral ad_gate_development

# Missing production credentials must exit 78 before opening durable resources.
set +e
docker run --name "$namespace-negative" --network none --cpus=1 --memory=1g \
  --label "org.astraldeep.qualification.namespace=$namespace" \
  --env PYTHON_DOTENV_DISABLED=1 "$image" > "$output/boot-negative.log" 2>&1
negative=$?
set -e
test "$negative" -eq 78
printf '%s\n' "$negative" > "$output/boot-negative.exit"

# Fresh synthetic keys never leave this runner. These are not live credentials.
python - "$namespace" "$environment" <<'PY'
import base64
import os
import secrets
import sys
from pathlib import Path
name, path = sys.argv[1:]
values = {
    "ASTRAL_ENV": "production", "USE_MOCK_AUTH": "false",
    "PYTHON_DOTENV_DISABLED": "1", "LETS_MODE": "off",
    "WEB_SESSION_ENC_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode(),
    "CREDENTIAL_ENCRYPTION_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode(),
    "AUDIT_HMAC_SECRET": secrets.token_urlsafe(48),
    "AGENT_API_KEY": secrets.token_urlsafe(48),
    "KEYCLOAK_AUTHORITY": "https://idp.invalid/realms/isolated-boot",
    "KEYCLOAK_CLIENT_ID": "isolated-boot", "KEYCLOAK_CLIENT_SECRET": secrets.token_urlsafe(48),
    "DATABASE_URL": f"postgresql://astral:isolated_ci_only@{name}-pg:5432/ad_gate_smoke",
    "ATTACHMENT_UPLOAD_ROOT": "/tmp/boot-blobs",
    "PERSONAL_AGENT_ARTIFACT_ROOT": "/tmp/boot-agents",
}
Path(path).write_text("".join(f"{key}={value}\n" for key, value in values.items()))
PY
docker run -d --init --name "$namespace-app" --network "$namespace" --cpus=2 --memory=2g \
  --label "org.astraldeep.qualification.namespace=$namespace" \
  --env-file "$environment" "$image" >/dev/null
docker exec -i "$namespace-app" python - <<'PY' > "$output/boot-positive.json"
import json
import time
import urllib.error
import urllib.request
deadline = time.monotonic() + 120
while True:
    try:
        observations = {}
        for path in ("/healthz", "/readyz"):
            with urllib.request.urlopen("http://127.0.0.1:8001" + path, timeout=3) as response:
                assert response.status == 200
                observations[path] = json.load(response)
        try:
            urllib.request.urlopen("http://127.0.0.1:8001/lets/health", timeout=3)
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        else:
            raise AssertionError("unauthenticated protected route was admitted")
        print(json.dumps({"scope": "production-configuration-boot-only",
                          "real_authentication_tested": False,
                          "lets_enforce_tested": False, "observations": observations}))
        break
    except (OSError, AssertionError):
        if time.monotonic() >= deadline:
            raise
        time.sleep(1)
PY
docker exec "$namespace-app" python /app/scripts/install_local_components.py verify \
  --root /app --lock /opt/astral-component-wheels/astral-component-wheels.lock.json
docker stop "$namespace-app" >/dev/null

# The distinct development smoke proves the documented development startup and
# mock handshake only. It has its own database and cannot qualify production.
docker run -d --init --name "$namespace-dev" --network "$namespace" --cpus=2 --memory=2g \
  --label "org.astraldeep.qualification.namespace=$namespace" \
  --env ASTRAL_ENV=development --env USE_MOCK_AUTH=true --env PYTHON_DOTENV_DISABLED=1 \
  --env DATABASE_URL="postgresql://astral:isolated_ci_only@$namespace-pg:5432/ad_gate_development" \
  --env ATTACHMENT_UPLOAD_ROOT=/tmp/development-blobs \
  --env PERSONAL_AGENT_ARTIFACT_ROOT=/tmp/development-agents "$image" >/dev/null
docker exec -i "$namespace-dev" python - <<'PY' > "$output/boot-development.json"
import json
import time
import urllib.request
deadline = time.monotonic() + 120
while True:
    try:
        observations = {}
        for path in ("/healthz", "/readyz"):
            with urllib.request.urlopen("http://127.0.0.1:8001" + path, timeout=3) as response:
                assert response.status == 200
                observations[path] = json.load(response)
        print(json.dumps({"scope": "development-boot-only", "production_qualified": False,
                          "observations": observations}))
        break
    except (OSError, AssertionError):
        if time.monotonic() >= deadline:
            raise
        time.sleep(1)
PY
if [[ "$phase" == boot ]]; then exit 0; fi
url="postgresql://astral:isolated_ci_only@$namespace-pg:5432/ad_gate_tests"
# The isolated development server supplies the historical mock-token handshake;
# loopback can never resolve to an unrelated developer or production instance.
docker run --init --name "$namespace-test" --network "container:$namespace-dev" --cpus=2 --memory=4g \
  --label "org.astraldeep.qualification.namespace=$namespace" \
  --user "$(id -u):$(id -g)" --env HOME=/tmp \
  --env DATABASE_URL="$url" --env ASTRALPLANE_TEST_DATABASE_URL="$url" \
  --env ASTRALPLANE_TEST_POSTGRES_DSN="$url" --env ASTRAL_TEST_ISOLATED=1 \
  --env PYTHON_DOTENV_DISABLED=1 --env PYTHONDONTWRITEBYTECODE=1 \
  --env ATTACHMENT_UPLOAD_ROOT=/tmp/test-blobs --env PERSONAL_AGENT_ARTIFACT_ROOT=/tmp/test-agents \
  --env ASTRALDEEP_SOURCE_REPO=/workspace \
  --env PATH=/opt/ci-node:/usr/local/bin:/usr/bin:/bin \
  --volume "$(dirname "$(command -v node)"):/opt/ci-node:ro" \
  --volume "$policy_root:/qualification-policy:ro" \
  --volume "$root:/workspace" --workdir /workspace \
  --entrypoint /bin/bash "$image" -euc '
    python -m venv --system-site-packages /tmp/ci
    /tmp/ci/bin/python -m pip install --require-hashes -r /qualification-policy/tooling/backend-ci/requirements.lock.txt
    /tmp/ci/bin/python /qualification-policy/scripts/run_backend_web_tests.py --root /workspace --output /workspace/build/backend-web
  '
