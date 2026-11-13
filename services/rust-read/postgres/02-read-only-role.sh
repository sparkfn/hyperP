#!/usr/bin/env bash
#
# Create the restricted read-only login used by the #491 Rust read spike.
#
# This script runs from the official Postgres image's init sequence
# (/docker-entrypoint-initdb.d) as the database owner (POSTGRES_USER), after
# 01-schema.sql has created the projection tables. It is safe to run again:
# the login, its attributes, its memberships and its grants are all reset.
#
# The reader role is deliberately minimal:
#   * no superuser / createdb / createrole / replication / bypassrls rights
#   * no role memberships (so it can never inherit writer privileges)
#   * default_transaction_read_only = on
#   * CONNECT on the database, USAGE on schema public, SELECT on the seven
#     projection tables, and nothing else
#   * no inherited PUBLIC CREATE on schema public and no inherited
#     PUBLIC CREATE/TEMPORARY on the database
#
# The password comes from RUST_SPIKE_READ_PASSWORD. It is read back into a
# psql variable from the environment, so it never appears in this script, in
# a SQL string, or in any process argument list.

(
    set -Eeuo pipefail

    if [ -z "${POSTGRES_USER:-}" ]; then
        echo "02-read-only-role.sh: POSTGRES_USER is required" >&2
        exit 1
    fi

    if [ -z "${POSTGRES_DB:-}" ]; then
        echo "02-read-only-role.sh: POSTGRES_DB is required" >&2
        exit 1
    fi

    if [ -z "${RUST_SPIKE_READ_PASSWORD:-}" ]; then
        echo "02-read-only-role.sh: RUST_SPIKE_READ_PASSWORD is required and must not be empty" >&2
        exit 1
    fi

    if ! command -v psql >/dev/null 2>&1; then
        echo "02-read-only-role.sh: psql is not available" >&2
        exit 1
    fi

    export RUST_SPIKE_READ_PASSWORD

    psql \
        --no-psqlrc \
        --set=ON_ERROR_STOP=1 \
        --set=dbname="${POSTGRES_DB}" \
        --username="${POSTGRES_USER}" \
        --dbname="${POSTGRES_DB}" \
        --file=- <<'SQL'
-- Read the reader password from the environment into a psql variable. The
-- command text holds no secret, and the value is substituted by psql as a
-- properly quoted SQL literal.
\set read_password `printf %s "$RUST_SPIKE_READ_PASSWORD"`

-- Create the login once, then keep its attributes and password authoritative.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'spike_reader') THEN
        CREATE ROLE spike_reader
            LOGIN
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOREPLICATION
            NOBYPASSRLS;
    END IF;
END
$$;

-- Role attributes and password.
ALTER ROLE spike_reader WITH
    LOGIN
    NOINHERIT
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOREPLICATION
    NOBYPASSRLS
    PASSWORD :'read_password';

-- Drop every membership the reader may have inherited from an earlier run.
SELECT format('REVOKE %I FROM spike_reader', granted.rolname)
FROM pg_auth_members AS membership
JOIN pg_roles AS granted ON granted.oid = membership.roleid
JOIN pg_roles AS member ON member.oid = membership.member
WHERE member.rolname = 'spike_reader'
\gexec

-- The reader may never write, not even implicitly.
ALTER ROLE spike_reader SET default_transaction_read_only = on;

-- Remove inherited PUBLIC creation rights on the schema and the database.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE CREATE, TEMPORARY ON DATABASE :"dbname" FROM PUBLIC;

-- Remove direct privileges as well as inherited PUBLIC write privileges.
REVOKE ALL PRIVILEGES ON TABLE public.person, public.person_identifier, public.person_address,
    public.person_connection, public.source_record, public.identity_link_rev,
    public.summary_counter FROM spike_reader, PUBLIC;
REVOKE ALL PRIVILEGES ON SCHEMA public FROM spike_reader;
REVOKE ALL PRIVILEGES ON DATABASE :"dbname" FROM spike_reader;

-- Grant exactly what the reader needs and nothing else.
GRANT CONNECT ON DATABASE :"dbname" TO spike_reader;
GRANT USAGE ON SCHEMA public TO spike_reader;
GRANT SELECT ON TABLE public.person, public.person_identifier, public.person_address,
    public.person_connection, public.source_record, public.identity_link_rev,
    public.summary_counter TO spike_reader;
SQL

    echo "02-read-only-role.sh: spike_reader is configured with SELECT-only access"
)
