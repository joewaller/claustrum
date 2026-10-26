-- 0012_intents.sql
-- Declared PLANNED work. Live sessions only show who is active right now (the
-- /v1/list recency window), so queued work — a plan written yesterday, a
-- feature someone means to start next week — was invisible and three people
-- could build the same thing without knowing. An intent is a small, durable
-- "I'm going to work on <title> in <paths>" marker that /v1/list matches
-- against a session's repo + paths (or topic) regardless of recency.
--
-- Value-scrubbed by construction: a title (plan heading / one line) and
-- repo-relative directory prefixes only — never transcript content.
--
-- Retention: an intent stops matching at expires_at (default 30 days,
-- refreshable); /jobs/state-transitions deletes rows closed or lapsed more
-- than INTENT_RETENTION_DAYS (90) ago.
--
-- Additive: a new table nothing else depends on. Old servers never read it;
-- old clients never write it and ignore the extra `intents` key on /v1/list.

CREATE TABLE IF NOT EXISTS intents (
    id          bigserial PRIMARY KEY,
    user_email  text NOT NULL,
    uid         text,
    repo        text,
    title       text NOT NULL,
    paths       jsonb NOT NULL DEFAULT '[]'::jsonb,
    topic       text,
    domain      text,
    status      text NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'done', 'abandoned')),
    source      text NOT NULL DEFAULT 'manual',
    pr_number   integer,
    is_private  boolean NOT NULL DEFAULT false,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL DEFAULT now() + interval '30 days'
);

CREATE INDEX IF NOT EXISTS idx_intents_open_repo
    ON intents (repo, expires_at) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_intents_open_topic
    ON intents (topic, expires_at) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_intents_user
    ON intents (user_email, created_at DESC);

INSERT INTO _schema_migrations (version) VALUES ('0012_intents')
    ON CONFLICT (version) DO NOTHING;
