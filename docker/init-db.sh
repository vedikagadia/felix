#!/usr/bin/env sh
# One-shot initializer for the felix CockroachDB node (run by docker-compose's
# crdb-init service). Creates the `felix` database, applies the schema, and
# seeds it from sql/seed_dump.sql — but only when the DB is empty, so bringing
# the stack up repeatedly is idempotent (the seed's INSERTs would collide on a
# re-run against already-populated tables).
#
# Everything here is plain POSIX sh so it runs on the minimal cockroach image.
set -eu

HOST="${CRDB_HOST:-crdb:26257}"

sql() { cockroach sql --insecure --host="$HOST" "$@"; }

echo "[init] waiting for CockroachDB at $HOST ..."
# depends_on: service_healthy already gates this, but re-check to be safe.
until sql -e "SELECT 1" >/dev/null 2>&1; do
  sleep 1
done

echo "[init] ensuring database 'felix' exists ..."
sql -e "CREATE DATABASE IF NOT EXISTS felix;"

echo "[init] applying schema (sql/schema.sql) ..."
sql --database=felix -f /sql/schema.sql

# Seed only if the incidents table is empty. --format=csv puts the count on the
# last line; strip whitespace and compare.
rows=$(sql --database=felix --format=csv -e "SELECT count(*) FROM incidents" | tail -1 | tr -d '[:space:]')
if [ "$rows" = "0" ]; then
  echo "[init] incidents table empty — seeding from sql/seed_dump.sql ..."
  sql --database=felix -f /sql/seed_dump.sql
  echo "[init] seed complete (incl. precomputed embeddings)."
else
  echo "[init] incidents already has $rows row(s) — skipping seed."
fi

echo "[init] done. felix DB is ready at $HOST."
