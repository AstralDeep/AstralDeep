#!/bin/bash
# Boots the personal node image on SQLite with no PostgreSQL reachable, runs e2e.py and prints the result. Usage: run-pinned.sh <node-image> <output-file>
set -u
IMG=$1; OUT=$2; HERE=$(cd "$(dirname "$0")" && pwd); W=
P=di105; NET=$P-net; VOL=$P-data; KEYS=$P-keys
cleanup() { docker rm -f $P-node $P-issuer $P-model >/dev/null 2>&1; docker volume rm -f $VOL $KEYS >/dev/null 2>&1; docker network rm $NET >/dev/null 2>&1; rm -rf "$W"; }
cleanup; W=$(mktemp -d); trap cleanup EXIT
docker network create --internal $NET >/dev/null; docker volume create $VOL >/dev/null; docker volume create $KEYS >/dev/null
fernet() { docker run --rm --entrypoint python "$IMG" -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"; }
cat > "$W/node.env" <<ENV
ASTRAL_ENV=development
ASTRAL_NODE_PROFILE=personal
ASTRAL_NODE_DATA_DIR=/node-data/node
DATABASE_URL=sqlite:////node-data/plane.sqlite3
KEYCLOAK_AUTHORITY=http://$P-issuer:9000/realms/astral
KEYCLOAK_CLIENT_ID=astral-app
PUBLIC_BASE_URL=http://$P-node:8001
BACKEND_PUBLIC_URL=http://$P-node:8001
ATTACHMENT_UPLOAD_ROOT=/node-data/blobs
PERSONAL_AGENT_ARTIFACT_ROOT=/node-data/agents
AUTO_START_AGENTS=true
ORCHESTRATOR_PORT=8001
EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS=sn105-model
PYTHON_DOTENV_DISABLED=1
ENV
cat > "$W/secrets.env" <<ENV
WEB_SESSION_ENC_KEY=$(fernet)
CREDENTIAL_ENCRYPTION_KEY=$(fernet)
AUDIT_HMAC_SECRET=$(openssl rand -hex 32)
AGENT_API_KEY=$(openssl rand -hex 32)
ENV
mkdir "$W/creds"; chmod 777 "$W/creds"
docker run -d --name $P-issuer --network $NET -e ISSUER=http://$P-issuer:9000/realms/astral -e KEYDIR=/keys -v $KEYS:/keys -v "$HERE/issuer.py":/issuer.py:ro --entrypoint python "$IMG" /issuer.py serve >/dev/null
docker run -d --name $P-model --network $NET --network-alias sn105-model -e FAKE_TOOL=generate_dynamic_chart -v "$HERE/fakemodel.py":/fakemodel.py:ro --entrypoint python "$IMG" -u /fakemodel.py >/dev/null
docker run -d --name $P-node --network $NET --env-file "$W/node.env" --env-file "$W/secrets.env" -v $VOL:/node-data -v "$W/creds":/creds "$IMG" >/dev/null
probe() { docker exec $P-node python -c "import urllib.request,sys; r=urllib.request.urlopen('http://127.0.0.1:8001$1',timeout=3); print(r.read().decode()[:$2])"; }
for i in $(seq 1 60); do probe /readyz 10 >/dev/null 2>&1 && break; sleep 3; done
{
echo "== pinned Plane =="
echo "\$ importlib.metadata version and SCHEMA_REVISION (in the node image)"
docker exec $P-node python -c "import astralplane,importlib.metadata as m; print(m.version('astralplane'), astralplane.SCHEMA_REVISION)"
echo "\$ docker network inspect $NET: internal=$(docker network inspect $NET --format '{{.Internal}}'), PostgreSQL env in node: $(docker exec $P-node env | grep -c '^DATABASE_URL=postgres')"
echo "\$ issuer mint owner-1 astral-app > /creds/owner.token (token not printed)"
docker exec $P-issuer python /issuer.py mint owner-1 astral-app > "$W/creds/owner.token"
echo "\$ GET /healthz"; probe /healthz 200
echo "\$ GET /readyz"; probe /readyz 300
docker exec -i $P-node python - <<'PY'
import sqlite3
c = sqlite3.connect("file:/node-data/plane.sqlite3?mode=ro", uri=True)
print("sqlite tables:", c.execute("select count(*) from sqlite_master where type='table'").fetchone()[0])
print("schema_meta:", c.execute("select key, substr(value,1,64) from schema_meta").fetchall())
PY
echo "\$ python e2e.py (inside the node container)"
docker exec -i $P-node python -u - < "$HERE/e2e.py" 2>&1 | cut -c1-260
echo "== fake model log =="; docker logs $P-model 2>&1 | tail -6 | cut -c1-140
echo "== node log checks =="
docker logs $P-node > "$W/node.log" 2>&1
echo "lines: $(wc -l < "$W/node.log")"
echo "SQLite write lock was not available: $(grep -c 'write lock was not available' "$W/node.log")"
echo "database is locked: $(grep -c 'database is locked' "$W/node.log")"
echo "no such function: $(grep -c 'no such function' "$W/node.log")"
echo "Traceback: $(grep -c Traceback "$W/node.log")"
} > "$OUT" 2>&1
