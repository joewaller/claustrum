"""Read side of the alias mechanism: one {alias: canonical} map per taxonomy
kind, combining the legacy `parent` column with approved `taxonomy_aliases`
rows (migration 0013). Readers resolve names through it at query time, so an
approved merge takes effect without rewriting any session history."""

from app import taxonomy
from app.cache import cached

_TABLES = {"topic": "topics", "domain": "domains"}
# Aliases change only when a human approves a merge, so the per-turn /v1/list
# path can serve them from memory; an approval takes effect within this window.
ALIAS_CACHE_SECONDS = 60.0


async def load_alias_map(cur, kind: str) -> dict:
    table = _TABLES[kind]
    await cur.execute(f"SELECT name, parent FROM {table} WHERE parent IS NOT NULL")
    parent_rows = await cur.fetchall()
    await cur.execute(
        "SELECT alias, canonical FROM taxonomy_aliases "
        "WHERE kind = %(k)s AND status = 'approved'",
        {"k": kind},
    )
    alias_rows = await cur.fetchall()
    return taxonomy.build_alias_map(parent_rows, alias_rows)


async def cached_alias_map(cur, kind: str) -> dict:
    return await cached(
        ("alias_map", kind), ALIAS_CACHE_SECONDS, lambda: load_alias_map(cur, kind)
    )


def topic_join(col: str, join: str = "LEFT JOIN") -> str:
    """SQL joining `t` to the taxonomy row of the topic in `col`, through one
    approved alias hop, so an alias-only topic still yields its canonical's
    domain. Aliases its own `ta`."""
    return (
        f"LEFT JOIN taxonomy_aliases ta ON ta.kind = 'topic' AND ta.alias = {col} "
        f"AND ta.status = 'approved' "
        f"{join} topics t ON t.name = COALESCE(ta.canonical, {col})"
    )
