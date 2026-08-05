-- The suite's own database, created beside the dogfood one.
--
-- `make test` and the local dogfood MCP servers once resolved the SAME DSN, and the
-- Postgres fixtures truncate `capture_queue` at setup. During a live demo that deleted three real
-- captures mid-walk. The index is a disposable cache (D10) and could be rebuilt; a queued capture
-- exists NOWHERE else until the librarian files it, so it could not.
--
-- Two databases in one instance rather than a second Postgres service: the suites need the same
-- pgvector image, the same port and the same credentials — only a different `dbname` — and one
-- service keeps `docker compose up --wait` meaning "the whole stack is usable".
--
-- The postgres image runs everything in `/docker-entrypoint-initdb.d/` exactly once, on an EMPTY
-- data directory. The composition has no named volume on purpose (every `up` starts clean), so
-- this runs on every fresh start. An ALREADY-RUNNING container predating this file never ran it:
-- `make db-down && make db-up` recreates it, and `tests/testdb.py` fails loudly (never skips)
-- when the server is up but this database is missing, so the gap cannot hide inside a green run.
CREATE DATABASE stigmergy_test OWNER stigmergy;

COMMENT ON DATABASE stigmergy_test IS
  'The pytest suites truncate capture_queue/job_runs/ingest_errors/audit_log here. Never point a '
  'running brain at this database, and never point the suites anywhere else.';
