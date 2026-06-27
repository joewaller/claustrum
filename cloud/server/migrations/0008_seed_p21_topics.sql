-- 0008_seed_p21_topics.sql
-- Phase 2.1 (classification quality): mint topics for work that had nowhere good
-- to go, so even a perfect classifier stops falling back to app/versionista/
-- wordpress-mcp. Source of truth: cloud/bootstrap/seed_topics.json.
--   games        -> projects   (Joe's personal game projects)
--   code-review  -> engineering (PR / code review sessions)
--   youtube-mcp  -> gateway    } mirror the existing product-data-mcp /
--   meta-mcp     -> gateway    } wordpress-mcp per-platform MCP topics
--   tiktok-mcp   -> gateway    }
-- topics.domain is NOT NULL (since 0006), so these insert WITH a domain.
-- Idempotent: ON CONFLICT (name) DO NOTHING.

INSERT INTO topics (name, description, source, domain) VALUES
  ('games',       'Games / interactive game-development projects.', 'bootstrap', 'projects'),
  ('code-review', 'PR review and code-review sessions.',            'bootstrap', 'engineering'),
  ('youtube-mcp', 'YouTube MCP server integration.',               'bootstrap', 'gateway'),
  ('meta-mcp',    'Meta MCP server integration.',                  'bootstrap', 'gateway'),
  ('tiktok-mcp',  'TikTok MCP server integration.',                'bootstrap', 'gateway')
ON CONFLICT (name) DO NOTHING;

INSERT INTO _schema_migrations (version) VALUES ('0008_seed_p21_topics')
    ON CONFLICT (version) DO NOTHING;
