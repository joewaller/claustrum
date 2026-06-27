import re

from fastapi import APIRouter, Depends, HTTPException

from app import db
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

# Token-Jaccard at/above which a proposed name is treated as a duplicate of an
# existing canonical name. Deliberately HIGH — the classifying sub-agent already
# did the semantic pick-vs-propose call; this guard only catches near-identical
# surface variants (case/punctuation/word-order/one extra token), not merely
# related topics. Better to occasionally mint a slightly-similar topic than to
# wrongly collapse two distinct ones.
_DUP_JACCARD = 0.7


def _norm_tokens(name: str) -> frozenset:
    return frozenset(t for t in re.split(r"[^a-z0-9]+", (name or "").lower()) if t)


def _near_duplicate(name: str, existing: list[str]) -> str | None:
    """Return the existing canonical name `name` is a near-duplicate of, or None.

    Pure + unit-tested. Matches on (a) identical token sets ignoring
    case/punctuation/order (e.g. 'word-press' ~ 'wordpress', 'gateway deploy' ~
    'gateway-deploy'), or (b) token-Jaccard >= _DUP_JACCARD. Conservative by
    design — see _DUP_JACCARD."""
    nt = _norm_tokens(name)
    if not nt:
        return None
    for e in existing:
        et = _norm_tokens(e)
        if not et:
            continue
        if nt == et:
            return e
        union = len(nt | et)
        if union and len(nt & et) / union >= _DUP_JACCARD:
            return e
    return None


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
    domain = (req.domain or "general").strip().lower()

    async with db.conn() as c:
        async with c.cursor() as cur:
            await cur.execute("SELECT 1 FROM domains WHERE name = %(d)s", {"d": domain})
            if await cur.fetchone() is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"domain '{domain}' is not a known domain "
                    "(propose-domain it first)",
                )

            await cur.execute("SELECT name FROM topics")
            existing = [r[0] for r in await cur.fetchall()]
            dup = name if name in existing else _near_duplicate(name, existing)
            if dup is not None:
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
            dup = name if name in existing else _near_duplicate(name, existing)
            if dup is not None:
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
