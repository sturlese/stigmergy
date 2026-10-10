-- The suite's own database, created beside the development one.
--
-- The Postgres fixtures truncate `capture_queue` at setup, so a suite sharing the DSN of a local
-- MCP server would delete that server's queued captures. The index is a disposable cache (D10) and
-- can be rebuilt; a queued capture exists nowhere else until the librarian files it.
--
-- Two databases in one instance rather than a second Postgres service: the suites need the same
-- pgvector image, the same port and the same credentials — only a different `dbname` — and one
-- service keeps `docker compose up --wait` meaning "the whole stack is usable".
--
-- The suites connect as `stigmergy_app`, a role that is neither a superuser nor exempt from
-- row-level security, as the deployed service is: tenant isolation is a policy, and a superuser
-- connection would pass every isolation test without exercising it. The role owns the test
-- database and its tables, so it runs the same start-up DDL production runs.
--
-- The postgres image runs everything in `/docker-entrypoint-initdb.d/` exactly once, on an EMPTY
-- data directory. The composition has no named volume on purpose (every `up` starts clean), so
-- this runs on every fresh start. An ALREADY-RUNNING container predating this file never ran it:
-- `make db-down && make db-up` recreates it, and `tests/testdb.py` fails loudly (never skips)
-- when the server is up but this database is missing, so the gap cannot hide inside a green run.
CREATE ROLE stigmergy_app LOGIN PASSWORD 'stigmergy_app' NOSUPERUSER NOBYPASSRLS NOCREATEROLE;

CREATE DATABASE stigmergy_test OWNER stigmergy_app;

COMMENT ON DATABASE stigmergy_test IS
  'The pytest suites truncate capture_queue/job_runs/ingest_errors/audit_log here. Never point a '
  'running brain at this database, and never point the suites anywhere else.';

\connect stigmergy_test
CREATE EXTENSION IF NOT EXISTS vector;
ALTER SCHEMA public OWNER TO stigmergy_app;

-- The local brain database is the service's too, so local runs meet the same isolation.
ALTER DATABASE stigmergy OWNER TO stigmergy_app;
\connect stigmergy
CREATE EXTENSION IF NOT EXISTS vector;
ALTER SCHEMA public OWNER TO stigmergy_app;
