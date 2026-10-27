-- 0013_taxonomy_aliases.sql
-- Canonical-name mapping for the emergent domain/topic taxonomy. The fleet grew
-- to ~530 domains / ~1.7k topics with the same work split across spelling and
-- naming variants (artifacts / artefacts-reviews / artifacts-site ...). Rather
-- than rewriting session history, a merge is recorded here as alias -> canonical
-- and every reader (/v1/list overlap tiers, /v1/classify_self, the taxonomy
-- endpoints the classifier ranks) resolves names through it at query time.
--
-- Lifecycle: /jobs/topic-merge writes status='proposed' rows (never approves).
-- A human reviews the dry-run map (python -m app.merge_cli plan) and approves
-- explicitly; only 'approved' rows take effect. 'rejected' rows stop the job
-- re-proposing the same alias.
--
-- No FK to topics/domains: an alias may name a free-text topic that was never
-- promoted into the taxonomy. Additive + idempotent: old servers never read it,
-- old clients are unaffected.

CREATE TABLE IF NOT EXISTS taxonomy_aliases (
    kind         text NOT NULL CHECK (kind IN ('topic', 'domain')),
    alias        text NOT NULL,
    canonical    text NOT NULL,
    status       text NOT NULL DEFAULT 'proposed'
                     CHECK (status IN ('proposed', 'approved', 'rejected')),
    score        real,
    reasons      jsonb NOT NULL DEFAULT '[]'::jsonb,
    proposed_at  timestamptz NOT NULL DEFAULT now(),
    decided_at   timestamptz,
    decided_by   text,
    PRIMARY KEY (kind, alias),
    CHECK (alias <> canonical)
);

CREATE INDEX IF NOT EXISTS idx_taxonomy_aliases_approved
    ON taxonomy_aliases (kind, canonical) WHERE status = 'approved';

INSERT INTO _schema_migrations (version) VALUES ('0013_taxonomy_aliases')
    ON CONFLICT (version) DO NOTHING;
