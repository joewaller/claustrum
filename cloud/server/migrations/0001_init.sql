-- Claustrum Cloud — initial schema
-- Postgres 16. Migrations applied forward-only, tracked in _schema_migrations.

CREATE TABLE IF NOT EXISTS _schema_migrations (
    version     text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- sessions — hot rows: active + paused. Archived rows leave PG for BQ.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sessions (
    uid               text PRIMARY KEY,
    user_email        text NOT NULL,
    machine           text NOT NULL,
    label             text,
    task              text,
    working_on        text,
    topic             text,
    topic_confidence  smallint,
    status            text NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active', 'paused', 'done')),
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
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sessions_repo_active
    ON sessions (repo, last_seen DESC) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_sessions_topic_active
    ON sessions (topic, last_seen DESC) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_sessions_user
    ON sessions (user_email);
CREATE INDEX IF NOT EXISTS idx_sessions_status_seen
    ON sessions (status, last_seen);
CREATE INDEX IF NOT EXISTS idx_sessions_files_touched
    ON sessions USING GIN (files_touched jsonb_path_ops);
CREATE INDEX IF NOT EXISTS idx_sessions_pr
    ON sessions (repo, pr_number) WHERE pr_number IS NOT NULL;

-- ---------------------------------------------------------------------------
-- messages — inter-session direct + broadcast. Monthly-partitioned.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS messages (
    id            bigserial,
    from_uid      text,
    to_uid        text,
    to_repo       text,
    to_topic      text,
    type          text NOT NULL DEFAULT 'info',
    body          text NOT NULL,
    metadata      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    delivered_at  timestamptz,
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);

-- Bootstrap two partitions so the table is usable on day one.
-- A monthly job creates the next partition; an archive job drops 90+d-old ones.
CREATE TABLE IF NOT EXISTS messages_default
    PARTITION OF messages DEFAULT;

CREATE INDEX IF NOT EXISTS idx_messages_undelivered
    ON messages (created_at) WHERE delivered_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_messages_to_uid
    ON messages (to_uid, created_at DESC) WHERE to_uid IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_to_repo
    ON messages (to_repo, created_at DESC) WHERE to_repo IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_to_topic
    ON messages (to_topic, created_at DESC) WHERE to_topic IS NOT NULL;

-- ---------------------------------------------------------------------------
-- claims — soft file claims with TTL. Not enforced as hard locks.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS claims (
    uid         text NOT NULL,
    repo        text NOT NULL,
    rel_path    text NOT NULL,
    claimed_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    PRIMARY KEY (repo, rel_path, uid)
);

-- (No partial-on-now() index here — Postgres rejects volatile functions in
-- index predicates. Lookups filter on expires_at > now() at query time;
-- the regular (repo, rel_path) index serves them.)
CREATE INDEX IF NOT EXISTS idx_claims_repo_path
    ON claims (repo, rel_path);
CREATE INDEX IF NOT EXISTS idx_claims_expires
    ON claims (expires_at);

-- ---------------------------------------------------------------------------
-- topics — current taxonomy. Seeded by 0004_seed_topics.sql (from the memory KG).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS topics (
    name            text PRIMARY KEY,
    description     text NOT NULL,
    source          text NOT NULL CHECK (source IN ('bootstrap', 'proposed', 'merged')),
    parent          text REFERENCES topics(name),
    proposal_count  integer NOT NULL DEFAULT 0,
    promoted_at     timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- topic_proposals — pending LLM-suggested new topics. Hourly job promotes
-- when count(distinct user_email) >= 2 (lowered 3 -> 2 on 2026-05-31 for
-- Finder team size; see PROMOTION_THRESHOLD in routes/propose.py).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS topic_proposals (
    id             bigserial PRIMARY KEY,
    uid            text NOT NULL,
    user_email     text NOT NULL,
    proposed_name  text NOT NULL,
    description    text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    resolved_at    timestamptz,
    resolution     text
);

CREATE INDEX IF NOT EXISTS idx_topic_proposals_open
    ON topic_proposals (proposed_name) WHERE resolved_at IS NULL;

INSERT INTO _schema_migrations (version) VALUES ('0001_init')
    ON CONFLICT (version) DO NOTHING;
