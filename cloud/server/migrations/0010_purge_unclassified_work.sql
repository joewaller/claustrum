-- 0010_purge_unclassified_work.sql
-- Retire the 'unclassified-work' junk sink. It was an emergent source='proposed'
-- topic in the 'general' domain meaning "couldn't classify this" — but the
-- match-first judge, once shown it as a candidate, reused it as an "I'm unsure"
-- bucket and committed it at CLASSIFY_SKILL_CONF (which marks a session done and
-- ends retries). It drew 78 sessions across 38 users (audit 2026-07-23), almost
-- all of them clearly classifiable.
--
-- Two moves, both idempotent:
--   1. Untag every session sitting on a blocklisted catch-all topic. NULLing
--      topic/domain/confidence returns ACTIVE ones to the classify queue (the
--      skill re-fires on sub-floor sessions with the reworded, reuse-biased
--      prompt + client-side blocklist guard) and makes DONE ones honestly
--      untagged instead of falsely bucketed.
--   2. Delete the catch-all topics from the taxonomy so the judge is never shown
--      them as candidates again. propose_topic now also rejects these names, so
--      they can't be re-minted.
--
-- The name list mirrors claustrum's CLASSIFY_TOPIC_BLOCKLIST / propose.py
-- _TOPIC_BLOCKLIST. Real one-off-but-legitimate general topics (e.g.
-- 'personal-private-work') are intentionally NOT touched.

UPDATE sessions
   SET topic = NULL,
       domain = NULL,
       topic_confidence = 0
 WHERE lower(topic) IN (
       'unclassified-work','unclassified','uncategorized','uncategorised',
       'misc','miscellaneous','no-topic','none','unknown','general-work',
       'general','untagged','tbd','todo','other'
 );

-- topics only — the 'general' DOMAIN is untouched (this deletes topic rows, and
-- no legitimate topic is named for a catch-all).
DELETE FROM topics
 WHERE lower(name) IN (
       'unclassified-work','unclassified','uncategorized','uncategorised',
       'misc','miscellaneous','no-topic','none','unknown','general-work',
       'general','untagged','tbd','todo','other'
 );

INSERT INTO _schema_migrations (version) VALUES ('0010_purge_unclassified_work')
    ON CONFLICT (version) DO NOTHING;
