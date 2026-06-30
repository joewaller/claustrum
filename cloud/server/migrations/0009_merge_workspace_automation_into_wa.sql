-- 0009_merge_workspace_automation_into_wa.sql
-- Collapse the duplicate 'workspace-automation' topic onto canonical 'wa' (same
-- thing: the workspace-automation repo / tooling). 'wa' stays canonical. Unlike
-- the 0004 variants this topic ALREADY exists (a KG-seeded 'proposed' row), so we
-- UPDATE its parent rather than INSERT a new variant. classify_self (server) and
-- the client _canonical_topic() then resolve any 'workspace-automation' pick onto
-- 'wa', so the board and collision detection converge on one row.
--
-- wordpress-mcp is intentionally NOT merged into wordpress — the MCP server work
-- is distinct from wordpress content (Joe's call, 2026-06-30).
--
-- Guarded on 'wa' existing so we never write a dangling parent; idempotent (a
-- second run is a no-op once parent/source already match).

UPDATE topics
   SET parent = 'wa', source = 'merged'
 WHERE name = 'workspace-automation'
   AND EXISTS (SELECT 1 FROM topics WHERE name = 'wa');

INSERT INTO _schema_migrations (version) VALUES ('0009_merge_workspace_automation_into_wa')
    ON CONFLICT (version) DO NOTHING;
