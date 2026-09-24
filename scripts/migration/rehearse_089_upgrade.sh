#!/usr/bin/env bash
# Rehearses the guarded migration chain against a disposable copy of the populated candidate
# database, never the live one, so a destructive rollback can be tested safely.
set -euo pipefail
export MSYS_NO_PATHCONV=1

PG=astraldeep-postgres
SRC=astralbody
COPY=astralbody_089_rehearsal
ENVFILE="${ASTRAL_ENV_FILE:-Y:/WORK/MCP/AstralDeep/.env}"
CHAIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -W 2>/dev/null || pwd)"

q() { docker exec "$PG" psql -U astral -d "$1" -t -A -c "$2"; }

run_chain() {
  docker run --rm --network astraldeep_default --env-file "$ENVFILE" \
    -e DB_HOST=postgres -e DB_NAME="$COPY" \
    -v "${ASTRAL_BACKEND:-Y:/WORK/MCP/AstralDeep/backend}:/app/backend" \
    -v "${CHAIN_DIR}:/chain" \
    -w /app/backend astraldeep:latest python /chain/run_089_chain.py 2>&1 | tail -8
}

snapshot() { q "$COPY" "select md5(coalesce(string_agg(t::text, '|' order by t::text),'')) from $1 t"; }

echo "== 1. the live populated database, as the candidate stack left it =="
echo "revision:         $(q $SRC "select value from schema_meta where key='revision'")"
echo "migration digest: $(q $SRC "select value from schema_meta where key='astralplane_migration_digest'")"
for t in users user_llm_config user_typesafe_credential user_data_sharing_acknowledgment; do
  printf '  %-34s %s\n' "$t" "$(q $SRC "select count(*) from $t")"
done

echo
echo "== 2. taking a copy to rehearse against =="
docker exec "$PG" psql -U astral -d postgres -q -c "drop database if exists $COPY" >/dev/null 2>&1
docker exec "$PG" psql -U astral -d postgres -q -c "create database $COPY" >/dev/null
docker exec "$PG" sh -lc "pg_dump -U astral -d $SRC | psql -U astral -q -d $COPY" >/dev/null 2>&1
echo "  copy created: $COPY"
LLM_BEFORE=$(snapshot user_llm_config)
USERS_BEFORE=$(snapshot users)
echo "  user_llm_config content digest: $LLM_BEFORE"
echo "  users content digest:           $USERS_BEFORE"

echo
echo "== 3. the documented rollback: drop both 089 tables, restore the 088.008 marker =="
docker exec -i "$PG" psql -U astral -d "$COPY" -q -v ON_ERROR_STOP=1 <<'SQL'
BEGIN;
DROP TABLE IF EXISTS user_typesafe_credential;
DROP TABLE IF EXISTS user_data_sharing_acknowledgment;
UPDATE schema_meta SET value = '088.008' WHERE key = 'revision';
UPDATE schema_meta SET value = '4cddbddbc3eed66232f35bc24452451f92aa2b3e90968eb6fd6754f9a64358d8'
  WHERE key = 'astralplane_migration_digest';
COMMIT;
SQL
echo "  revision after rollback: $(q $COPY "select value from schema_meta where key='revision'")"
echo "  089 tables remaining:    $(q $COPY "select count(*) from information_schema.tables where table_schema='public' and table_name in ('user_typesafe_credential','user_data_sharing_acknowledgment')")"
LLM_AFTER=$(snapshot user_llm_config)
USERS_AFTER=$(snapshot users)
echo "  user_llm_config unchanged: $([ "$LLM_BEFORE" = "$LLM_AFTER" ] && echo yes || echo NO)"
echo "  users unchanged:           $([ "$USERS_BEFORE" = "$USERS_AFTER" ] && echo yes || echo NO)"

echo
echo "== 4. upgrading the rolled-back copy, on populated data =="
run_chain
echo "  revision after upgrade: $(q $COPY "select value from schema_meta where key='revision'")"
echo "  089 tables present:     $(q $COPY "select count(*) from information_schema.tables where table_schema='public' and table_name in ('user_typesafe_credential','user_data_sharing_acknowledgment')")"
echo "  migration digest:       $(q $COPY "select value from schema_meta where key='astralplane_migration_digest'")"

echo
echo "== 4b. the repeat start: the same chain, again =="
run_chain
echo "  revision after repeat:  $(q $COPY "select value from schema_meta where key='revision'")"

echo
echo "== 5. nothing the upgrade touched changed the legacy rows =="
LLM_FINAL=$(snapshot user_llm_config)
USERS_FINAL=$(snapshot users)
echo "  user_llm_config unchanged end to end: $([ "$LLM_BEFORE" = "$LLM_FINAL" ] && echo yes || echo NO)"
echo "  users unchanged end to end:           $([ "$USERS_BEFORE" = "$USERS_FINAL" ] && echo yes || echo NO)"

echo
docker exec "$PG" psql -U astral -d postgres -q -c "drop database if exists $COPY" >/dev/null
echo "copy dropped; the live database was never written to"
