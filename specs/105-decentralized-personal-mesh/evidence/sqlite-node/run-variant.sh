#!/bin/bash
# Boots the personal node image on SQLite and runs e2e.py. Usage: run-variant.sh <stock|plane-fix> (plane-fix mounts a Plane sqlite.py named by PLANE_SQLITE_PATCHED).
V=$1; D=/tmp/sn105; OUT=$D/report-$V.txt
docker rm -f sn105-node >/dev/null 2>&1; docker volume rm -f sn105-data-$V >/dev/null 2>&1; docker volume create sn105-data-$V >/dev/null
EXTRA=()
[ "$V" = "plane-fix" ] && EXTRA=(-v $PLANE_SQLITE_PATCHED:/usr/local/lib/python3.11/site-packages/astralplane/database/sqlite.py:ro)
docker run -d --name sn105-node --network sn105-net -p 18001:8001 --env-file $D/node.env --env-file $D/secrets.env -v sn105-data-$V:/node-data -v $D/creds:/creds:ro "${EXTRA[@]}" sqlite-node-lane:105 >/dev/null
until curl -sf -m3 localhost:18001/readyz >/dev/null 2>&1; do sleep 3; done
{
echo "== variant $V =="
echo "\$ issuer mint owner-1 astral-app > /creds/owner.token (token not printed)"
docker exec -e ISSUER=http://sn105-issuer:9000/realms/astral sn105-issuer python /issuer.py mint owner-1 astral-app > $D/creds/owner.token
echo "\$ curl localhost:18001/healthz"; curl -s localhost:18001/healthz; echo
echo "\$ curl localhost:18001/readyz"; curl -s localhost:18001/readyz; echo
echo "\$ curl localhost:18001/api/node/descriptor (truncated)"; curl -s localhost:18001/api/node/descriptor | head -c 260; echo
docker exec -i sn105-node python - <<'PY'
import sqlite3
c = sqlite3.connect("file:/node-data/plane.sqlite3?mode=ro", uri=True)
print("sqlite tables:", c.execute("select count(*) from sqlite_master where type='table'").fetchone()[0])
print("schema_meta:", c.execute("select key, substr(value,1,64) from schema_meta").fetchall())
PY
echo "\$ python e2e.py (inside the node container)"
docker exec -i sn105-node python -u - < $D/e2e.py 2>&1 | cut -c1-260
echo "== fake model log =="; docker logs sn105-model 2>&1 | tail -6 | cut -c1-140
} > $OUT 2>&1
echo done > $D/report-$V.done
