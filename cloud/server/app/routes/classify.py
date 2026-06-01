from fastapi import APIRouter, Depends, HTTPException

from app import db
from app.auth import current_user
from app.models import ClassifySelfRequest

router = APIRouter()


@router.post("/classify_self")
async def classify_self(req: ClassifySelfRequest, user_email: str = Depends(current_user)):
    """Set the session's topic (any free-text string — the taxonomy is
    emergent, so we don't require the name to already exist in `topics`) and
    return a historical_dedupe payload so the agent can see who has worked on
    this topic before.

    Scoped to the authenticated user — you can only classify your own session.

    The payload below is the PG-derivable subset of the eventual design:
      - active_peers: other live sessions (any status, not private) on the same
        topic — the immediate "someone is/was already on this" signal.
      - related_prs: distinct (repo, pr_number) seen on sessions in this topic —
        a proxy for "PRs associated with this topic".
    KG-entity and BQ-archive lookups described in the design land in a later
    phase (the server has no KG/BQ access yet); `sources_pending` flags that.
    """
    async with db.conn() as c:
        async with c.cursor() as cur:
            await cur.execute(
                """
                UPDATE sessions SET
                    topic            = %(topic)s,
                    topic_confidence = %(confidence)s,
                    last_activity_at = now(),
                    last_seen        = now(),
                    updated_at       = now()
                WHERE uid = %(uid)s AND user_email = %(user_email)s
                RETURNING repo
                """,
                {
                    "topic": req.topic,
                    "confidence": req.confidence,
                    "uid": req.uid,
                    "user_email": user_email,
                },
            )
            row = await cur.fetchone()
            if row is None:
                raise HTTPException(
                    status_code=404,
                    detail="No session with that uid for the authenticated user.",
                )

            await cur.execute(
                """
                SELECT uid, user_email, machine, repo, branch, status,
                       last_seen, working_on, pr_number
                FROM sessions
                WHERE topic = %(topic)s AND uid <> %(uid)s AND is_private = false
                ORDER BY last_seen DESC
                LIMIT 20
                """,
                {"topic": req.topic, "uid": req.uid},
            )
            active_peers = [
                {
                    "uid": r[0],
                    "user_email": r[1],
                    "machine": r[2],
                    "repo": r[3],
                    "branch": r[4],
                    "status": r[5],
                    "last_seen": r[6],
                    "working_on": r[7],
                    "pr_number": r[8],
                }
                for r in await cur.fetchall()
            ]

            await cur.execute(
                """
                SELECT DISTINCT repo, pr_number, user_email
                FROM sessions
                WHERE topic = %(topic)s AND pr_number IS NOT NULL
                  AND uid <> %(uid)s AND is_private = false
                ORDER BY repo, pr_number
                LIMIT 20
                """,
                {"topic": req.topic, "uid": req.uid},
            )
            related_prs = [
                {"repo": r[0], "pr_number": r[1], "user_email": r[2]}
                for r in await cur.fetchall()
            ]

        await c.commit()

    return {
        "ok": True,
        "topic": req.topic,
        "historical_dedupe": {
            "topic": req.topic,
            "active_peers": active_peers,
            "related_prs": related_prs,
            "sources_pending": ["kg_entities", "bq_archive"],
        },
    }
