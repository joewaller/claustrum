-- Claustrum Cloud — 0003: cold archive (hot/cold split, no BigQuery).
-- Phase 5 follow-up. The original v2 schema assumed done rows would stream to
-- BigQuery and leave Postgres; Phase 5 chose Postgres-only, so there is no BQ.
-- This migration keeps that decision and bounds the hot `sessions` table by
-- moving cold rows to a sibling table *in the same database* — completed work
-- is kept forever and stays readable (paginated), never deleted, never in BQ.
--
-- Cold = (a) `done` rows older than the archive window, and (b) long-stale
-- `paused` rows (abandoned sessions — we deliberately do NOT auto-close them to
-- `done`; status is preserved, the row is just cold-stored). The daily
-- /jobs/archive-cold mover copies-then-deletes atomically (a CTE DELETE ...
-- RETURNING feeding an INSERT) so a row is never lost in transit.
--
-- Reads that must see *all* completed work (the solved-archive nudge in
-- /v1/list + /v1/classify_self, and the /v1/archive browse endpoint) read the
-- `v_sessions_all` view below, which unions hot + cold — so moving a row to the
-- cold table never hides it. The live-presence collision path stays on
-- `sessions` only (archived rows are stale by definition and not collision-
-- relevant), keeping the hot per-turn path fast.

-- ---------------------------------------------------------------------------
-- sessions_archive — cold store. Same columns as `sessions` (0001 + 0002) plus
-- archived_at. No defaults/CHECKs beyond the PK: values are copied verbatim
-- from `sessions`, not authored here. uid stays the primary key so a row that
-- is archived, resurrected (re-checkin), and re-archived upserts cleanly.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sessions_archive (
    uid               text PRIMARY KEY,
    user_email        text NOT NULL,
    machine           text NOT NULL,
    label             text,
    task              text,
    working_on        text,
    topic             text,
    topic_confidence  smallint,
    status            text NOT NULL,
    repo              text,
    branch            text,
    pr_number         integer,
    files_touched     jsonb NOT NULL DEFAULT '[]'::jsonb,
    last_push_at      timestamptz,
    last_activity_at  timestamptz,
    last_seen         timestamptz NOT NULL,
    started_at        timestamptz NOT NULL,
    cwd               text,
    is_quiet          boolean NOT NULL DEFAULT false,
    is_private        boolean NOT NULL DEFAULT false,
    created_at        timestamptz NOT NULL,
    updated_at        timestamptz NOT NULL,
    resolution        text,
    done_at           timestamptz,
    archived_at       timestamptz NOT NULL DEFAULT now()
);

-- Serve the solved-archive tier match + the browse endpoint against cold rows.
-- All three are partial on the relevant status so they stay small.
CREATE INDEX IF NOT EXISTS idx_archive_done_topic
    ON sessions_archive (topic, done_at DESC) WHERE status = 'done';
CREATE INDEX IF NOT EXISTS idx_archive_done_repo
    ON sessions_archive (repo, done_at DESC) WHERE status = 'done';
CREATE INDEX IF NOT EXISTS idx_archive_done_at
    ON sessions_archive (done_at DESC) WHERE status = 'done';

-- ---------------------------------------------------------------------------
-- v_sessions_all — every session, hot + cold, with an `archived` flag. The
-- canonical "all work ever" relation. Reads that span history union through
-- this so the physical hot/cold location is invisible to callers. Column lists
-- in both branches are identical and in `sessions` order (UNION ALL requires
-- it); archived_at is cold-only and intentionally not exposed (no reader needs
-- it — `archived` answers "which side did this come from").
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_sessions_all AS
    SELECT uid, user_email, machine, label, task, working_on, topic,
           topic_confidence, status, repo, branch, pr_number, files_touched,
           last_push_at, last_activity_at, last_seen, started_at, cwd,
           is_quiet, is_private, created_at, updated_at, resolution, done_at,
           false AS archived
    FROM sessions
    UNION ALL
    SELECT uid, user_email, machine, label, task, working_on, topic,
           topic_confidence, status, repo, branch, pr_number, files_touched,
           last_push_at, last_activity_at, last_seen, started_at, cwd,
           is_quiet, is_private, created_at, updated_at, resolution, done_at,
           true AS archived
    FROM sessions_archive;

INSERT INTO _schema_migrations (version) VALUES ('0003_cold_archive')
    ON CONFLICT (version) DO NOTHING;
