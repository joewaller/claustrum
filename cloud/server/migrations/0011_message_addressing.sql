-- 0011_message_addressing.sql
-- Directed cross-PERSON messaging. Until now `messages` rows were only emitted
-- server-side (topic-alerts) and addressed by to_uid / to_topic / to_repo. To
-- let one person message another's session — and get a reply — add:
--   to_email   — address every live session of a given person, robust to the
--                ephemeral session uid churning between runs.
--   from_email — the sender's identity, stamped server-side from the
--                authenticated caller, so the recipient can reply to the
--                person rather than a uid that may already be gone.
-- Both nullable; existing rows and server-emitted broadcasts leave them NULL.
-- Idempotent (ADD COLUMN IF NOT EXISTS); ALTER cascades to all partitions.

ALTER TABLE messages ADD COLUMN IF NOT EXISTS to_email text;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS from_email text;

-- Match the existing to_uid/to_repo/to_topic partial-index convention so the
-- inbox_drain email lookup only scans pending rows.
CREATE INDEX IF NOT EXISTS idx_messages_to_email
    ON messages (to_email, created_at DESC) WHERE to_email IS NOT NULL;

INSERT INTO _schema_migrations (version) VALUES ('0011_message_addressing')
    ON CONFLICT (version) DO NOTHING;
