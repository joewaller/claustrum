"""Gather taxonomy usage and turn it into a merge plan. Shared by
/jobs/topic-merge (which stores PROPOSALS) and `python -m app.merge_cli plan`
(which only prints). Neither path ever approves a merge — see merge_cli.approve.
"""

from __future__ import annotations

from app import taxonomy

MERGE_WINDOW_DAYS = 60    # adopter sets are built from sessions seen this recently
MAX_PROPOSALS = 500       # per kind, strongest first — a review list, not a dump

_TOPIC_USAGE_SQL = """
    SELECT topic, array_agg(DISTINCT user_email), count(*)
    FROM v_sessions_all
    WHERE topic IS NOT NULL AND is_private = false
      AND last_seen > now() - make_interval(days => %(days)s)
    GROUP BY topic
"""

# Cold rows carry no domain column, so they count through their topic's domain.
_DOMAIN_USAGE_SQL = """
    SELECT domain, array_agg(DISTINCT user_email), count(*)
    FROM (
        SELECT s.user_email, COALESCE(s.domain, t.domain) AS domain
        FROM sessions s LEFT JOIN topics t ON t.name = s.topic
        WHERE s.is_private = false
          AND s.last_seen > now() - make_interval(days => %(days)s)
        UNION ALL
        SELECT a.user_email, t.domain
        FROM sessions_archive a JOIN topics t ON t.name = a.topic
        WHERE a.is_private = false
          AND a.last_seen > now() - make_interval(days => %(days)s)
    ) x
    WHERE domain IS NOT NULL
    GROUP BY domain
"""

_TABLES = {"topic": "topics", "domain": "domains"}
_USAGE_SQL = {"topic": _TOPIC_USAGE_SQL, "domain": _DOMAIN_USAGE_SQL}


def entries_from_rows(taxonomy_rows, usage_rows) -> list[dict]:
    """Join taxonomy names ({name, source}) with usage (name, adopters[],
    sessions). Free-text names that were used but never promoted are included
    too — they fragment the board just the same."""
    out: dict[str, dict] = {}
    for name, source in taxonomy_rows:
        out[name] = {"name": name, "source": source, "adopters": set(), "sessions": 0}
    for name, adopters, sessions in usage_rows:
        e = out.setdefault(
            name, {"name": name, "source": None, "adopters": set(), "sessions": 0}
        )
        e["adopters"] = set(adopters or ())
        e["sessions"] = sessions or 0
    return list(out.values())


async def build_merge_plan(cur, window_days: int = MERGE_WINDOW_DAYS) -> dict:
    """{'topic': [...], 'domain': [...]} of {alias, canonical, score, reasons,
    alias_adopters, canonical_adopters}. Names already folded (legacy `parent`
    or an approved alias) are excluded; rejected aliases are never re-proposed."""
    plan: dict[str, list[dict]] = {}
    for kind, table in _TABLES.items():
        await cur.execute(f"SELECT name, source, parent FROM {table}")
        rows = await cur.fetchall()
        folded = {r[0] for r in rows if r[2]}
        await cur.execute(
            "SELECT alias, status FROM taxonomy_aliases "
            "WHERE kind = %(k)s AND status IN ('approved', 'rejected')",
            {"k": kind},
        )
        rejected: set = set()
        for alias, status in await cur.fetchall():
            (folded if status == "approved" else rejected).add(alias)
        await cur.execute(_USAGE_SQL[kind], {"days": window_days})
        usage = await cur.fetchall()
        entries = entries_from_rows([(r[0], r[1]) for r in rows], usage)
        by_name = {e["name"]: e for e in entries}
        proposals = taxonomy.propose_merges(entries, folded=folded, rejected=rejected)
        for p in proposals:
            p["alias_adopters"] = len(by_name[p["alias"]]["adopters"])
            p["canonical_adopters"] = len(by_name[p["canonical"]]["adopters"])
        plan[kind] = proposals[:MAX_PROPOSALS]
    return plan


def format_plan(plan: dict) -> str:
    """Human-readable merge map, grouped by canonical name, for review before
    anything is approved."""
    lines: list[str] = []
    for kind in ("domain", "topic"):
        rows = plan.get(kind) or []
        lines.append(f"== {kind} merges proposed: {len(rows)} ==")
        groups: dict[str, list[dict]] = {}
        for r in rows:
            groups.setdefault(r["canonical"], []).append(r)
        for canonical in sorted(groups, key=lambda c: (-max(r["score"] for r in groups[c]), c)):
            members = groups[canonical]
            lines.append(
                f"{canonical}  ({members[0].get('canonical_adopters', '?')} adopters)"
            )
            for r in sorted(members, key=lambda r: (-r["score"], r["alias"])):
                lines.append(
                    f"    <- {r['alias']}  score={r['score']:.2f}  "
                    f"adopters={r.get('alias_adopters', '?')}  [{', '.join(r['reasons'])}]"
                )
        lines.append("")
    return "\n".join(lines)
