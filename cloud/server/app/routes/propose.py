from fastapi import APIRouter, Depends

from app import db
from app.auth import current_user
from app.models import ProposeTopicRequest

router = APIRouter()

# Distinct-user count at which the validate-proposals job (Phase 3) promotes a
# proposed name into the official `topics` taxonomy. Joe lowered this 3 -> 2 on
# 2026-05-31 (Finder team size). The promotion itself lives in the hourly job;
# this route only queues proposals and reports progress toward the threshold.
PROMOTION_THRESHOLD = 2


@router.post("/propose_topic")
async def propose_topic(req: ProposeTopicRequest, user_email: str = Depends(current_user)):
    """Queue a topic proposal into topic_proposals. Idempotent per
    (uid, proposed_name): a session proposing the same name twice does not
    stack rows. Returns progress toward promotion so the agent gets feedback.

    Promotion to the official taxonomy is the validate-proposals job's job
    (Phase 3) — it fires when PROMOTION_THRESHOLD distinct user_emails have an
    open proposal for the same name.
    """
    async with db.conn() as c:
        async with c.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM topics WHERE name = %(name)s",
                {"name": req.name},
            )
            already_official = await cur.fetchone() is not None

            # Insert only if this uid has no open proposal for this name yet.
            await cur.execute(
                """
                INSERT INTO topic_proposals (uid, user_email, proposed_name, description)
                SELECT %(uid)s, %(user_email)s, %(name)s, %(description)s
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
        "already_official": already_official,
        "distinct_user_count": distinct_user_count,
        "promotion_threshold": PROMOTION_THRESHOLD,
        "promotable": distinct_user_count >= PROMOTION_THRESHOLD,
    }
