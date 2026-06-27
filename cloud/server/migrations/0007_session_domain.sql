-- 0007_session_domain.sql
-- Phase 2 of "Claustrum as canonical domain+topic taxonomy": store the domain on
-- the session itself, so a session classified into a brand-new topic (one not yet
-- joinable in `topics`) still shows a domain on the board. classify_self writes
-- it (passed explicitly, or derived from topics.domain); the board reads
-- COALESCE(sessions.domain, topics.domain).
--
-- Nullable: an untagged session has no domain. FK keeps it honest once set.
-- Idempotent: ADD COLUMN IF NOT EXISTS.

ALTER TABLE sessions ADD COLUMN IF NOT EXISTS domain text REFERENCES domains(name);

CREATE INDEX IF NOT EXISTS idx_sessions_domain_active
    ON sessions (domain, last_seen DESC) WHERE status = 'active';

INSERT INTO _schema_migrations (version) VALUES ('0007_session_domain')
    ON CONFLICT (version) DO NOTHING;
