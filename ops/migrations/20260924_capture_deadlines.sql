BEGIN;

LOCK TABLE capture_queue IN ACCESS EXCLUSIVE MODE;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM capture_queue WHERE status = 'processing') THEN
        RAISE EXCEPTION 'capture deadline cutover requires a drained queue';
    END IF;
END
$$;

ALTER TABLE capture_queue ADD COLUMN lease_started_at TIMESTAMPTZ;
ALTER TABLE capture_queue ADD COLUMN budget_deadline_at TIMESTAMPTZ;

COMMIT;
