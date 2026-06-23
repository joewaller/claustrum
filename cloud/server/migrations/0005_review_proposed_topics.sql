-- 0005_review_proposed_topics.sql
-- Curates the 15 topics that landed in `topics` with source='proposed' when
-- gateway topic-sync went live (2026-06-22). They were auto-derived from bare
-- `topic:` entities in the company KG and all carried the placeholder
-- description "Topic from the memory KG (domain: X)" — useless on a shared board.
--
-- This migration does NOT re-parent any of them: `parent` is for synonym/variant
-- collapse (cf. 0004: gateway -> mcp-gateway), and none of the 15 are synonyms of
-- an existing canonical (github != finder-au; seo is broader than ahrefs/gsc;
-- office-it is broader than meraki). So the hygiene actions are only:
--   1. Give the 13 genuine topics real descriptions + stamp promoted_at (= blessed).
--   2. Retire the 2 that are generic buckets, not session subjects.
--
-- source stays 'proposed' (accurate provenance — they arrived via the register
-- endpoint, not the bootstrap seed); promoted_at IS NOT NULL is the curated signal.
-- Idempotent via the `source='proposed'` guard: a row already curated/retired
-- won't be touched again.

-- ---------------------------------------------------------------------------
-- 1. Keep + describe + promote (13 genuine session topics).
-- ---------------------------------------------------------------------------

-- Tools / integrations (parallel to braze, figma, n8n, slack — distinct subjects).
UPDATE topics SET description = 'Datadog observability — metrics, logs, monitors, and APM.', promoted_at = now()
    WHERE name = 'datadog' AND source = 'proposed';
UPDATE topics SET description = 'Google Gemini — Gemini CLI/API and Gemma agent tooling (parallel to claude).', promoted_at = now()
    WHERE name = 'gemini' AND source = 'proposed';
UPDATE topics SET description = 'GitHub platform — repos, PRs, Actions, and the GitHub MCP integration.', promoted_at = now()
    WHERE name = 'github' AND source = 'proposed';
UPDATE topics SET description = 'HubSpot CRM — contacts, deals, and marketing automation (gateway MCP server).', promoted_at = now()
    WHERE name = 'hubspot' AND source = 'proposed';
UPDATE topics SET description = 'Atlassian Jira — issues, sprints, and project tracking (gateway MCP server).', promoted_at = now()
    WHERE name = 'jira' AND source = 'proposed';
UPDATE topics SET description = 'Scheduler — scheduler-mcp cron / recurring-task subsystem.', promoted_at = now()
    WHERE name = 'scheduler' AND source = 'proposed';

-- Broad subjects that sit ABOVE existing tool topics (kept canonical, not parented).
UPDATE topics SET description = 'SEO — search-optimisation work (umbrella over ahrefs, gsc, niche).', promoted_at = now()
    WHERE name = 'seo' AND source = 'proposed';
UPDATE topics SET description = 'Office IT — hardware, networking (incl. Meraki), and internal IT support.', promoted_at = now()
    WHERE name = 'office-it' AND source = 'proposed';
UPDATE topics SET description = 'Content intelligence — content performance analysis, scoring, and editorial optimisation.', promoted_at = now()
    WHERE name = 'content-intelligence' AND source = 'proposed';

-- Business / company subjects.
UPDATE topics SET description = 'Partners — commercial partner relationships, BD, and partner management.', promoted_at = now()
    WHERE name = 'partners' AND source = 'proposed';
UPDATE topics SET description = 'QBR — quarterly business review reporting and prep.', promoted_at = now()
    WHERE name = 'qbr' AND source = 'proposed';

-- Domain-overlapping but legitimate as session subjects (see header note in PR).
-- 'security' and 'legal' also exist as memory DOMAINS, but domain (KG partition)
-- and topic (what a session is about) are different axes — sessions genuinely do
-- security and legal work, so both stay. Sensitivity of a *specific* security
-- session is handled by the CLAUSTRUM_PRIVATE=1 privacy gate, not by dropping the topic.
UPDATE topics SET description = 'Security — credentials, secrets, access control, and security incidents. (Topic label only; set CLAUSTRUM_PRIVATE=1 for unannounced incidents — see privacy gate.)', promoted_at = now()
    WHERE name = 'security' AND source = 'proposed';
UPDATE topics SET description = 'Legal — contracts, compliance, terms, and privacy matters.', promoted_at = now()
    WHERE name = 'legal' AND source = 'proposed';

-- ---------------------------------------------------------------------------
-- 2. Retire noise (2).
-- Both are generic buckets, not subjects a session is *about*, and in a flat
-- (synonym-collapse, non-hierarchical) taxonomy they would compete with the
-- specific topics for auto-classify matches and dilute the collision signal:
--   'dev-patterns' — a knowledge-domain bucket ("development patterns").
--   'reporting'    — too generic; the real deliverables (weekly, epv, qbr)
--                    already carry the signal. Cannot act as a true umbrella
--                    because parent is synonym-collapse, not hierarchy.
-- Safe to delete: sessions.topic is free text (no FK), so any stray reference
-- survives as a string and simply drops out of the canonical vocabulary.
-- Guard: only deletes while still uncurated (source='proposed').
DELETE FROM topics WHERE name = 'dev-patterns' AND source = 'proposed';
DELETE FROM topics WHERE name = 'reporting' AND source = 'proposed';

INSERT INTO _schema_migrations (version) VALUES ('0005_review_proposed_topics')
    ON CONFLICT (version) DO NOTHING;
