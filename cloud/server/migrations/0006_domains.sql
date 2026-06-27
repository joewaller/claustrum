-- 0006_domains.sql
-- Phase 1 of "Claustrum as canonical domain+topic taxonomy": add DOMAINS as a
-- first-class, fully-emergent taxonomy (mirroring `topics`/`topic_proposals`),
-- seed the 12 memory-KG domains, and give every topic a domain.
--
-- topics.domain is NOT NULL (FK -> domains.name). To make `SET NOT NULL` safe on
-- BOTH staging (46 seed topics) and prod (~63, incl. registrar-proposed topics
-- the staging seed lacks), this migration:
--   1. seeds domains FIRST (so the FK + UPDATEs resolve),
--   2. adds a NULLABLE `domain` column,
--   3. applies an explicit per-topic mapping (covers the live prod taxonomy),
--   4. catch-alls any still-NULL topic to 'general' (safety net for topics
--      created after the mapping snapshot — guarantees no NULLs remain),
--   5. only THEN flips the column to NOT NULL.
-- Idempotent: re-running is a no-op (IF NOT EXISTS / ON CONFLICT / re-UPDATE).

-- ---------------------------------------------------------------------------
-- domains — canonical domain taxonomy. Mirrors `topics` (same emergent machinery:
-- bootstrap seeds, registrar register, propose -> promote at the distinct-user
-- threshold). `parent` collapses variants onto a canonical name (as topics do).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS domains (
    name            text PRIMARY KEY,
    description     text NOT NULL,
    source          text NOT NULL CHECK (source IN ('bootstrap', 'proposed', 'merged')),
    parent          text REFERENCES domains(name),
    proposal_count  integer NOT NULL DEFAULT 0,
    promoted_at     timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- domain_proposals — pending LLM-suggested new domains. The hourly
-- /jobs/validate-proposals run promotes when count(distinct user_email) >= 2
-- (PROMOTION_THRESHOLD, shared with topics).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS domain_proposals (
    id             bigserial PRIMARY KEY,
    uid            text NOT NULL,
    user_email     text NOT NULL,
    proposed_name  text NOT NULL,
    description    text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    resolved_at    timestamptz,
    resolution     text
);

CREATE INDEX IF NOT EXISTS idx_domain_proposals_open
    ON domain_proposals (proposed_name) WHERE resolved_at IS NULL;

-- ---------------------------------------------------------------------------
-- Seed the 12 canonical domains (from the memory KG's domain enum).
-- ---------------------------------------------------------------------------
INSERT INTO domains (name, description, source) VALUES
  ('actions',     'Action items, to-dos, scheduled and recurring tasks, the scheduler subsystem.', 'bootstrap'),
  ('company',     'Company-wide: org & leadership, strategy & initiatives, brand, finance, legal, partners, QBR.', 'bootstrap'),
  ('data',        'Data & analytics — BigQuery, datasets, metrics, revenue/EPV, GA4/GSC, SEO data, reporting.', 'bootstrap'),
  ('engineering', 'Engineering — infra (GCP, networking), repos & code, CI, testing, websites, tooling code.', 'bootstrap'),
  ('gateway',     'MCP gateway server and individual MCP-server integrations (proxy, OAuth routes, tool config).', 'bootstrap'),
  ('general',     'Uncategorised work that does not fit a more specific domain.', 'bootstrap'),
  ('people',      'People — calendars, scheduling, contacts, person management.', 'bootstrap'),
  ('projects',    'Ongoing product/vertical initiatives and named programs.', 'bootstrap'),
  ('security',    'Security — credentials, secrets, access control, PII, security incidents.', 'bootstrap'),
  ('strategy',    'Strategic themes and cross-cutting strategic work.', 'bootstrap'),
  ('tooling',     'Agent tooling — Claude Code CLI, hooks, scripts, Claustrum, skills, memory/KG MCP.', 'bootstrap'),
  ('wally',       'Wally Slack bot — request routing, QA gate, planning layer.', 'bootstrap')
ON CONFLICT (name) DO NOTHING;

-- ---------------------------------------------------------------------------
-- topics gains a domain. Nullable first so existing rows survive the ADD; filled
-- below; flipped to NOT NULL last. proposals carry a domain so promotion can
-- supply it (NOT NULL DEFAULT 'general' keeps the existing 3-arg propose path
-- working until callers send a real domain).
-- ---------------------------------------------------------------------------
ALTER TABLE topics ADD COLUMN IF NOT EXISTS domain text REFERENCES domains(name);
ALTER TABLE topic_proposals ADD COLUMN IF NOT EXISTS domain text NOT NULL DEFAULT 'general';

-- Explicit topic -> domain mapping (sub-agent-generated over the live prod
-- taxonomy, 2026-06-27; variants inherit their parent topic's domain). Curate
-- here. ON CONFLICT not needed — UPDATE no-ops on absent names.
UPDATE topics SET domain = 'data' WHERE name = 'adops';
UPDATE topics SET domain = 'data' WHERE name = 'ahrefs';
UPDATE topics SET domain = 'engineering' WHERE name = 'app';
UPDATE topics SET domain = 'company' WHERE name = 'au';
UPDATE topics SET domain = 'projects' WHERE name = 'bhn';
UPDATE topics SET domain = 'data' WHERE name = 'bigquery';
UPDATE topics SET domain = 'company' WHERE name = 'brand';
UPDATE topics SET domain = 'gateway' WHERE name = 'braze';
UPDATE topics SET domain = 'tooling' WHERE name = 'claude';
UPDATE topics SET domain = 'strategy' WHERE name = 'claustrum';
UPDATE topics SET domain = 'data' WHERE name = 'content-intelligence';
UPDATE topics SET domain = 'gateway' WHERE name = 'datadog';
UPDATE topics SET domain = 'engineering' WHERE name = 'dev-patterns';
UPDATE topics SET domain = 'engineering' WHERE name = 'e2e';
UPDATE topics SET domain = 'gateway' WHERE name = 'email';
UPDATE topics SET domain = 'gateway' WHERE name = 'email-action';
UPDATE topics SET domain = 'data' WHERE name = 'epv';
UPDATE topics SET domain = 'gateway' WHERE name = 'figma';
UPDATE topics SET domain = 'company' WHERE name = 'finance-ap';
UPDATE topics SET domain = 'company' WHERE name = 'finder-au';
UPDATE topics SET domain = 'company' WHERE name = 'finder-fridays';
UPDATE topics SET domain = 'projects' WHERE name = 'finder-rewards';
UPDATE topics SET domain = 'tooling' WHERE name = 'finder-skills';
UPDATE topics SET domain = 'projects' WHERE name = 'findershopping';
UPDATE topics SET domain = 'data' WHERE name = 'ga4';
UPDATE topics SET domain = 'gateway' WHERE name = 'gateway';
UPDATE topics SET domain = 'engineering' WHERE name = 'gcp';
UPDATE topics SET domain = 'tooling' WHERE name = 'gemini';
UPDATE topics SET domain = 'gateway' WHERE name = 'github';
UPDATE topics SET domain = 'gateway' WHERE name = 'google';
UPDATE topics SET domain = 'gateway' WHERE name = 'google-workspace';
UPDATE topics SET domain = 'data' WHERE name = 'gsc';
UPDATE topics SET domain = 'gateway' WHERE name = 'hubspot';
UPDATE topics SET domain = 'gateway' WHERE name = 'jira';
UPDATE topics SET domain = 'company' WHERE name = 'legal';
UPDATE topics SET domain = 'gateway' WHERE name = 'mcp-gateway';
UPDATE topics SET domain = 'tooling' WHERE name = 'memory';
UPDATE topics SET domain = 'engineering' WHERE name = 'meraki';
UPDATE topics SET domain = 'engineering' WHERE name = 'n8n';
UPDATE topics SET domain = 'projects' WHERE name = 'niche';
UPDATE topics SET domain = 'engineering' WHERE name = 'office-it';
UPDATE topics SET domain = 'engineering' WHERE name = 'papi';
UPDATE topics SET domain = 'company' WHERE name = 'partners';
UPDATE topics SET domain = 'projects' WHERE name = 'product';
UPDATE topics SET domain = 'gateway' WHERE name = 'product-data-mcp';
UPDATE topics SET domain = 'company' WHERE name = 'qbr';
UPDATE topics SET domain = 'engineering' WHERE name = 'reporting';
UPDATE topics SET domain = 'projects' WHERE name = 'rewards';
UPDATE topics SET domain = 'actions' WHERE name = 'scheduler';
UPDATE topics SET domain = 'security' WHERE name = 'security';
UPDATE topics SET domain = 'data' WHERE name = 'seo';
UPDATE topics SET domain = 'engineering' WHERE name = 'site';
UPDATE topics SET domain = 'gateway' WHERE name = 'slack';
UPDATE topics SET domain = 'tooling' WHERE name = 'tune';
UPDATE topics SET domain = 'engineering' WHERE name = 'versionista';
UPDATE topics SET domain = 'engineering' WHERE name = 'wa';
UPDATE topics SET domain = 'wally' WHERE name = 'wally';
UPDATE topics SET domain = 'actions' WHERE name = 'weekly';
UPDATE topics SET domain = 'gateway' WHERE name = 'wordpress';
UPDATE topics SET domain = 'gateway' WHERE name = 'wordpress-mcp';
UPDATE topics SET domain = 'gateway' WHERE name = 'workspace';
UPDATE topics SET domain = 'engineering' WHERE name = 'workspace-automation';
UPDATE topics SET domain = 'gateway' WHERE name = 'wp';

-- Safety net: any topic the mapping didn't cover (created after the snapshot, or
-- a prod-only proposed topic) defaults to 'general' so SET NOT NULL can't fail.
UPDATE topics SET domain = 'general' WHERE domain IS NULL;

ALTER TABLE topics ALTER COLUMN domain SET NOT NULL;

INSERT INTO _schema_migrations (version) VALUES ('0006_domains')
    ON CONFLICT (version) DO NOTHING;
