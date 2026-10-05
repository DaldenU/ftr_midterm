#!/bin/bash
# Runs once on first start of the primary container (docker-entrypoint-initdb.d).
# Creates the replication role and allows the standby to connect (used by the "replication" profile).
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<-SQL
  CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD '${REPLICATION_PASSWORD:-replicator-pass}';
SQL
echo "host replication replicator all scram-sha-256" >> "$PGDATA/pg_hba.conf"
