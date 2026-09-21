#!/bin/sh
# Runs once on first boot of the postgres volume. Creates the two non-admin roles.
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
  CREATE ROLE dg_service LOGIN PASSWORD '$PG_SERVICE_PASSWORD';
  CREATE ROLE dg_reader  LOGIN PASSWORD '$PG_READER_PASSWORD';
EOSQL
