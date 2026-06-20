-- 0004_seed_topics.sql
-- Seeds the `topics` taxonomy from the memory KG's existing topic: entities
-- (46 topics, captured 2026-06-20). Source of truth: cloud/bootstrap/seed_topics.json.
-- Idempotent: ON CONFLICT (name) DO NOTHING, so it never clobbers topics that
-- already exist (e.g. ones added via propose-topic). Duplicate-granularity names
-- are kept but pointed at a canonical topic via `parent` so joins don't fragment.

-- Canonical topics (parent IS NULL) — inserted first so the parent FK resolves.
INSERT INTO topics (name, description, source) VALUES ('adops', 'Ad operations — ad serving, inventory, and revenue ops.', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('ahrefs', 'Ahrefs SEO data and integration.', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('app', 'Application-level work not tied to a more specific subject.', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('bhn', 'BHN / Blackhawk Network gift-card fulfilment for rewards (evaluated vs Tremendous).', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('bigquery', 'BigQuery — datasets, SQL, and revenue/metrics pipelines.', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('braze', 'Braze customer-engagement and messaging platform.', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('brand', 'Brand and brand-comparison work.', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('claude', 'Claude Code / CLI, hooks, Claustrum, and agent tooling.', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('e2e', 'End-to-end testing.', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('email', 'Email automation and actions (Gmail).', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('epv', 'EPV (estimated partner value) weekly revenue tracker pipeline.', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('figma', 'Figma design files and integration.', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('finance-ap', 'Finance accounts-payable — invoices and contractor payments.', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('finder-au', 'Finder Australia org — finderau GitHub org and infrastructure.', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('finder-fridays', 'Finder Fridays internal initiative.', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('finder-rewards', 'Finder Rewards loyalty program.', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('finder-skills', 'Finder agent skills registry (finderau/finder-skills) — publish, install, manage skills.', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('findershopping', 'Finder Shopping product/vertical.', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('ga4', 'GA4 (Google Analytics 4) analytics.', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('gcp', 'Google Cloud Platform infra — VMs, Cloud Run, deploy.', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('google-workspace', 'Google Workspace — Gmail, Calendar, Sheets, Drive.', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('gsc', 'Google Search Console data and integration.', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('mcp-gateway', 'MCP gateway server — proxy, OAuth routes, tool config, deploy.', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('memory', 'Knowledge-graph / memory MCP — entities, domains, topics, hygiene, recall.', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('meraki', 'Cisco Meraki networking.', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('n8n', 'n8n workflow automation.', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('niche', 'Vertical niches / niche comparison pages.', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('papi', 'PAPI Product API — Finder''s product-data API, repo conventions, set composition.', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('product', 'Product team / product work.', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('product-data-mcp', 'Product-data MCP server.', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('site', 'Website / site-level work.', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('slack', 'Slack integration, OAuth, and bot messaging.', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('tune', 'Tuning and optimisation work (configs, performance, prompts).', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('versionista', 'Versionista web-change monitoring.', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('wa', 'workspace-automation repo — tooling, scripts, context.', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('wally', 'Wally Slack bot — request routing, QA gate, planning layer.', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('weekly', 'Weekly recurring reports and trackers.', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('wordpress', 'WordPress content — posts, pages, comments.', 'bootstrap') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source) VALUES ('wordpress-mcp', 'WordPress MCP server integration (content via MCP).', 'bootstrap') ON CONFLICT (name) DO NOTHING;

-- Duplicate-granularity variants, linked to their canonical topic via parent.
INSERT INTO topics (name, description, source, parent) VALUES ('gateway', 'Variant of mcp-gateway.', 'merged', 'mcp-gateway') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source, parent) VALUES ('rewards', 'Variant of finder-rewards.', 'merged', 'finder-rewards') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source, parent) VALUES ('au', 'Variant of finder-au.', 'merged', 'finder-au') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source, parent) VALUES ('workspace', 'Variant of google-workspace.', 'merged', 'google-workspace') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source, parent) VALUES ('google', 'Variant of google-workspace.', 'merged', 'google-workspace') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source, parent) VALUES ('wp', 'Variant of wordpress.', 'merged', 'wordpress') ON CONFLICT (name) DO NOTHING;
INSERT INTO topics (name, description, source, parent) VALUES ('email-action', 'Email-action feature; variant of email.', 'merged', 'email') ON CONFLICT (name) DO NOTHING;

INSERT INTO _schema_migrations (version) VALUES ('0004_seed_topics')
    ON CONFLICT (version) DO NOTHING;
