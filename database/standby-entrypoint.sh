#!/bin/bash
# Hot-standby bootstrap: clone the primary with pg_basebackup on first start, then run as streaming replica.
set -e
if [ ! -s "$PGDATA/PG_VERSION" ]; then
  echo "cloning primary..."
  export PGPASSWORD="${REPLICATION_PASSWORD:?REPLICATION_PASSWORD must be set}"
  until pg_basebackup -h postgres -U replicator -D "$PGDATA" -R -X stream -c fast; do sleep 2; done
  chmod 700 "$PGDATA"
fi
exec docker-entrypoint.sh postgres
