from fastapi import APIRouter, Depends, HTTPException

from app import db
from app.auth import current_user
from app.models import ProposeDomainRequest, ProposeTopicRequest

router = APIRouter()

# Distinct-user count at which the validate-proposals job promotes a proposed
# name into the official taxonomy (topics AND domains). Joe lowered this 3 -> 2
# on 2026-05-31 (Finder team size). The promotion itself lives in the hourly job;
# these routes only queue proposals and report progress toward the threshold.
PROMOTION_THRESHOLD = 2


@router.post("/propose_topic")
async def propose_topic(req: ProposeTopicRequest, user_email: str = Depends(current_user)):
    """Queue a topic proposal into topic_proposals. Idempotent per
    (uid, proposed_name): a session proposing the same name twice does not
    stack rows. Returns progress toward promotion so the agent gets feedback.

    The proposal carries the chosen `domain` (default 'general' when omitted) so
    that, on promotion, the new topic lands in the right domain — topics.domain
    is NOT NULL. The domain must already be a known domain (propose/register it
    first); otherwise 422.

    Promotion to the official taxonomy is the validate-proposals job's job — it
    fires when PROMOTION_THRESHOLD distinct user_emails have an open proposal for
    the same name.
    """
    domain = (req.domain or "general").strip().lower()
    async with db.conn() as c:
        async with c.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM domains WHERE name = %(d)s", {"d": domain}
            )
            if await cur.fetchone() is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"domain '{domain}' is not a known domain "
                    "(propose-domain it first)",
                )

            await cur.execute(
                "SELECT 1 FROM topics WHERE name = %(name)s",
                {"name": req.name},
            )
            already_official = await cur.fetchone() is not None

            # Insert only if this uid has no open proposal for this name yet.
            await cur.execute(
                """
                INSERT INTO topic_proposals (uid, user_email, proposed_name, description, domain)
                SELECT %(uid)s, %(user_email)s, %(name)s, %(description)s, %(domain)s
                WHERE NOT EXISTS (
                    SELECT 1 FROM topic_proposals
                    WHERE uid = %(uid)s AND proposed_name = %(name)s
                      AND resolved_at IS NULL
                )
                """,
                {
                    "uid": req.uid,
                    "user_email": user_email,
                    "name": req.name,
                    "description": req.description,
                    "domain": domain,
                },
            )

            await cur.execute(
                """
                SELECT count(DISTINCT user_email)
                FROM topic_proposals
                WHERE proposed_name = %(name)s AND resolved_at IS NULL
                """,
                {"name": req.name},
            )
            distinct_user_count = (await cur.fetchone())[0]

        await c.commit()

    return {
        "ok": True,
        "proposed_name": req.name,
        "domain": domain,
        "already_official": already_official,
        "distinct_user_count": distinct_user_count,
        "promotion_threshold": PROMOTION_THRESHOLD,
        "promotable": distinct_user_count >= PROMOTION_THRESHOLD,
    }


@router.post("/propose_domain")
async def propose_domain(req: ProposeDomainRequest, user_email: str = Depends(current_user)):
    """Queue a domain proposal into domain_proposals. Mirror of propose_topic:
    idempotent per (uid, proposed_name), reports progress toward promotion, and
    the hourly validate-proposals job promotes at PROMOTION_THRESHOLD distinct
    proposers.
    """
    async with db.conn() as c:
        async with c.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM domains WHERE name = %(name)s",
                {"name": req.name},
            )
            already_official = await cur.fetchone() is not None

            await cur.execute(
                """
                INSERT INTO domain_proposals (uid, user_email, proposed_name, description)
                SELECT %(uid)s, %(user_email)s, %(name)s, %(description)s
                WHERE NOT EXISTS (
                    SELECT 1 FROM domain_proposals
                    WHERE uid = %(uid)s AND proposed_name = %(name)s
                      AND resolved_at IS NULL
                )
                """,
                {
                    "uid": req.uid,
                    "user_email": user_email,
                    "name": req.name,
                    "description": req.description,
                },
            )

            await cur.execute(
                """
                SELECT count(DISTINCT user_email)
                FROM domain_proposals
                WHERE proposed_name = %(name)s AND resolved_at IS NULL
                """,
                {"name": req.name},
            )
            distinct_user_count = (await cur.fetchone())[0]

        await c.commit()

    return {
        "ok": True,
        "proposed_name": req.name,
        "already_official": already_official,
        "distinct_user_count": distinct_user_count,
        "promotion_threshold": PROMOTION_THRESHOLD,
        "promotable": distinct_user_count >= PROMOTION_THRESHOLD,
    }
