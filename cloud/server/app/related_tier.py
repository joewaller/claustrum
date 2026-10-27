"""t3b — the "related topic" overlap tier for /v1/list. Kept apart from
list_peers.bucket_tiers so the t1-t4 logic stays untouched: this runs after it
and only re-homes peers it left in t4 or unbucketed."""

from app.aliases import topic_join
from app.taxonomy import resolve, topic_relation

_RELATION_ORDER = {"topic~": 0, "stem:": 1, "domain:": 2}


def bucket_related(
    candidates: list[dict],
    tiers: tuple[list[dict], list[dict], list[dict], list[dict]],
    my_topic: str | None,
    my_domain: str | None,
    aliases: dict | None = None,
    domain_aliases: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    """t3b — "related topic": the overlap the exact-topic t3 misses because the
    taxonomy fragments (artifacts-app vs artefacts-reviews vs a shared specific
    domain). Pure, applied AFTER bucket_tiers so t1/t2/t3/t4 logic is untouched:
    any peer not already in t1-t3 whose (alias-resolved) topic is related to
    mine moves here — out of t4 if it was there (still no double counting).
    Returns (t3b, t4). Each t3b peer carries `reason` (topic~ / stem: /
    domain:), strongest first."""
    from app.routes.list_peers import _peer  # deferred: list_peers imports this module

    t1, t2, t3, t4 = tiers
    strong = {p["uid"] for p in (*t1, *t2, *t3)}
    my_t = resolve(my_topic, aliases)
    my_d = resolve(my_domain, domain_aliases)
    related: list[dict] = []
    for row in candidates:
        if row["uid"] in strong:
            continue
        reason = topic_relation(
            my_t,
            my_d,
            resolve(row.get("topic"), aliases),
            resolve(row.get("domain"), domain_aliases),
        )
        if reason:
            peer = _peer(row)
            peer["domain"] = row.get("domain")
            peer["reason"] = reason
            related.append(peer)
    related.sort(key=lambda p: next(
        (rank for prefix, rank in _RELATION_ORDER.items() if p["reason"].startswith(prefix)), 9
    ))
    moved = {p["uid"] for p in related}
    return related, [p for p in t4 if p["uid"] not in moved]


async def fetch_related_topics(
    cur, uid, statuses, recency_min, my_topic, my_domain, aliases, domain_aliases
) -> list[str]:
    """Live topic names (other than mine) that alias to mine or are related to
    it, so the candidate query can fetch those peers by topic. Computed in
    Python over the small set of distinct live (topic, domain) pairs —
    relatedness isn't expressible in SQL."""
    if not (my_topic or my_domain):
        return []
    await cur.execute(
        f"""
        SELECT DISTINCT s.topic, COALESCE(s.domain, t.domain)
        FROM sessions s
        {topic_join("s.topic")}
        WHERE s.uid <> %(uid)s
          AND s.is_private = false
          AND s.status = ANY(%(statuses)s)
          AND s.last_seen > now() - make_interval(mins => %(recency)s)
          AND s.topic IS NOT NULL
        """,
        {"uid": uid, "statuses": statuses, "recency": recency_min},
    )
    my_t = resolve(my_topic, aliases)
    my_d = resolve(my_domain, domain_aliases)
    out: set[str] = set()
    for topic, domain in await cur.fetchall():
        their_t = resolve(topic, aliases)
        if (my_t and their_t == my_t) or topic_relation(
            my_t, my_d, their_t, resolve(domain, domain_aliases)
        ):
            out.add(topic)
    return sorted(out)


async def topic_domain(cur, topic: str, aliases: dict | None) -> str | None:
    """The taxonomy domain of `topic`, preferring its alias-resolved canonical."""
    canon = resolve(topic, aliases)
    await cur.execute(
        "SELECT domain FROM topics WHERE name = ANY(%(names)s) "
        "ORDER BY (name = %(canon)s) DESC LIMIT 1",
        {"names": [canon, topic], "canon": canon},
    )
    row = await cur.fetchone()
    return row[0] if row else None
