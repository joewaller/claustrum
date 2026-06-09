-- Claustrum Cloud — 0002: solved-problem archive.
-- Phase 5. Turns Claustrum from a live-only collision detector into a library
-- of *solved* work: a session can record how it was resolved, and future
-- sessions matching the same files/PR/topic/repo see "this was solved before".
--
-- Postgres-only (no BigQuery). Done rows STAY in `sessions` and remain
-- queryable by the same dedup tiers as live peers — they are NOT evicted.
-- (The /jobs/archive-to-bq stub is repurposed to a copy-not-delete cold
-- offload for the long tail; it must never delete the hot rows the archive
-- lookup reads.)

ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS resolution text,
    ADD COLUMN IF NOT EXISTS done_at    timestamptz;

-- Serves the archive lookup: done sessions, newest first, clustered by topic.
-- The existing GIN(files_touched) and (repo, pr_number) indexes already serve
-- the t1/t2 overlap probes; this adds the t3/t4 + recency slice.
CREATE INDEX IF NOT EXISTS idx_sessions_done
    ON sessions (topic, done_at DESC) WHERE status = 'done';

CREATE INDEX IF NOT EXISTS idx_sessions_done_repo
    ON sessions (repo, done_at DESC) WHERE status = 'done';

INSERT INTO _schema_migrations (version) VALUES ('0002_archive')
    ON CONFLICT (version) DO NOTHING;
