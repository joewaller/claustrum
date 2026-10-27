from fastapi import APIRouter, Depends, HTTPException

from app import db, taxonomy
from app.aliases import load_alias_map
from app.auth import current_user
from app.models import ProposeDomainRequest, ProposeTopicRequest

router = APIRouter()

# Retained for the validate-proposals cleanup job (Phase 1). The classify path
# below no longer waits for it: a proposed name is promoted INLINE (effective
# threshold 1) once it clears the similarity guard, so new domains/topics are
# usable fleet-wide immediately (Joe's Phase 2 decision). The old distinct-user
# gate is replaced by the automated dedup guard below + the sub-agent's own
# judgement about whether something is genuinely dissimilar.
PROMOTION_THRESHOLD = 2

# Junk catch-all names that mean "couldn't classify" rather than a real subject.
# These must never enter the canonical taxonomy — once present, the match-first
# judge is shown them as candidates and reuses them as an "I'm unsure" sink
# (the 'unclassified-work' problem: it became a proposed topic and drew 78
# sessions / 38 users). Rejecting them here is defence-in-depth alongside the
# client-side guard in run_classification_skill; kept in sync with claustrum's
# CLASSIFY_TOPIC_BLOCKLIST. Compared case-folded.
_TOPIC_BLOCKLIST = frozenset({
    "unclassified-work", "unclassified", "uncategorized", "uncategorised",
    "misc", "miscellaneous", "no-topic", "none", "unknown", "general-work",
    "general", "untagged", "tbd", "todo", "other",
})


def _norm_tokens(name: str) -> frozenset:
    return taxonomy.norm_tokens(name)


def _near_duplicate(name: str, existing: list[str]) -> str | None:
    """Return the existing canonical name `name` is a near-duplicate of, or None.

    Pure + unit-tested. Compares normalised tokens (case/punctuation/order,
    en-AU/en-US spelling, plurals, wp->wordpress): identical sets, token-Jaccard
    >= taxonomy.DUP_JACCARD, or the same core once generic kind-of-work tokens
    are dropped ('artefacts-app' ~ 'artifacts'). Conservative by design — the
    judge already made the semantic call; this catches surface variants."""
    return taxonomy.near_duplicate(name, existing)


@router.post("/propose_topic")
async def propose_topic(req: ProposeTopicRequest, user_email: str = Depends(current_user)):
    """Add a topic to the canonical taxonomy NOW (effective promote-at-1), unless
    it's a near-duplicate of an existing one.

    Flow: validate the domain exists -> run the similarity guard against existing
    topic names. If a near-duplicate exists, create nothing and return it as
    `mapped_to` so the caller classifies into the existing name. Otherwise insert
    the topic canonically (source='proposed', promoted_at=now) carrying the
    chosen `domain` (default 'general') and return created=true.

    topics.domain is NOT NULL, so the domain must already be a known domain
    (propose-domain it first) — 422 otherwise.
    """
    name = req.name.strip().lower()
    if not name:
        raise HTTPException(status_code=422, detail="name must be non-empty")
    if name in _TOPIC_BLOCKLIST:
        raise HTTPException(
            status_code=422,
            detail=f"'{name}' is a junk catch-all, not a real topic — "
            "leave the session untagged so it can be reclassified.",
        )
    domain = (req.domain or "general").strip().lower()

    async with db.conn() as c:
        async with c.cursor() as cur:
            await cur.execute("SELECT name FROM domains")
            known_domains = {r[0] for r in await cur.fetchall()}
            canonical_domain = taxonomy.resolve(domain, await load_alias_map(cur, "domain"))
            if canonical_domain in known_domains:
                domain = canonical_domain
            if domain not in known_domains:
                raise HTTPException(
                    status_code=422,
                    detail=f"domain '{domain}' is not a known domain "
                    "(propose-domain it first)",
                )

            await cur.execute("SELECT name FROM topics")
            existing = [r[0] for r in await cur.fetchall()]
            aliases = await load_alias_map(cur, "topic")
            dup = name if (name in existing or name in aliases) else _near_duplicate(name, existing)
            if dup is not None:
                dup = taxonomy.resolve(dup, aliases)
                await cur.execute(
                    "SELECT domain FROM topics WHERE name = %(n)s", {"n": dup}
                )
                drow = await cur.fetchone()
                return {
                    "ok": True,
                    "name": dup,
                    "created": False,
                    "mapped_to": dup,
                    "domain": drow[0] if drow else domain,
                }

            await cur.execute(
                """
                INSERT INTO topics (name, description, source, parent, domain, promoted_at)
                VALUES (%(name)s, %(desc)s, 'proposed', NULL, %(domain)s, now())
                ON CONFLICT (name) DO NOTHING
                """,
                {"name": name, "desc": req.description.strip(), "domain": domain},
            )
        await c.commit()

    return {"ok": True, "name": name, "created": True, "mapped_to": None, "domain": domain}


@router.post("/propose_domain")
async def propose_domain(req: ProposeDomainRequest, user_email: str = Depends(current_user)):
    """Add a domain to the canonical taxonomy NOW (effective promote-at-1), unless
    it's a near-duplicate of an existing one. Mirror of propose_topic without the
    domain field."""
    name = req.name.strip().lower()
    if not name:
        raise HTTPException(status_code=422, detail="name must be non-empty")

    async with db.conn() as c:
        async with c.cursor() as cur:
            await cur.execute("SELECT name FROM domains")
            existing = [r[0] for r in await cur.fetchall()]
            aliases = await load_alias_map(cur, "domain")
            dup = name if (name in existing or name in aliases) else _near_duplicate(name, existing)
            if dup is not None:
                dup = taxonomy.resolve(dup, aliases)
                return {"ok": True, "name": dup, "created": False, "mapped_to": dup}

            await cur.execute(
                """
                INSERT INTO domains (name, description, source, promoted_at)
                VALUES (%(name)s, %(desc)s, 'proposed', now())
                ON CONFLICT (name) DO NOTHING
                """,
                {"name": name, "desc": req.description.strip()},
            )
        await c.commit()

    return {"ok": True, "name": name, "created": True, "mapped_to": None}
