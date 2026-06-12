from fastapi import APIRouter, Depends, HTTPException

from app import db
from app.auth import current_user
from app.models import ClassifySelfRequest
from app.routes.list_peers import fetch_solved_candidates, solved_matches

router = APIRouter()


@router.post("/classify_self")
async def classify_self(req: ClassifySelfRequest, user_email: str = Depends(current_user)):
    """Set the session's topic (any free-text string — the taxonomy is
    emergent, so we don't require the name to already exist in `topics`) and
    return a historical_dedupe payload so the agent can see who is — and who
    *was* — working on this before.

    Scoped to the authenticated user — you can only classify your own session.

    The payload:
      - active_peers: other *live* (active/paused, not private) sessions on the
        same topic — the immediate "someone is already on this" signal.
      - related_prs: distinct (repo, pr_number) seen on sessions in this topic.
      - solved: *done* sessions matched by the same overlap tiers as /v1/list
        (file > PR/dir > topic > repo) carrying who solved it, when, and the
        resolution — the "this has been solved before" signal that stops a
        second person re-solving a closed problem.
    `sources_pending` now lists only `kg_entities` (the server still has no KG
    access); the BQ archive is no longer pending — solved history is served
    from Postgres.
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
                RETURNING repo, pr_number, files_touched
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
            my_repo, my_pr, my_files = row[0], row[1], list(row[2] or [])

            await cur.execute(
                """
                SELECT uid, user_email, machine, repo, branch, status,
                       last_seen, working_on, pr_number
                FROM sessions
                WHERE topic = %(topic)s AND uid <> %(uid)s AND is_private = false
                  AND status IN ('active', 'paused')
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

            # Solved archive — done sessions (any age, hot + cold) sharing my
            # repo or topic, matched by the same tiers as /v1/list. Shared
            # helper so the two solved paths never drift.
            solved_candidates = await fetch_solved_candidates(
                cur, req.uid, my_repo, req.topic
            )
            solved = solved_matches(
                solved_candidates, my_repo, req.topic, my_pr, my_files
            )

        await c.commit()

    return {
        "ok": True,
        "topic": req.topic,
        "historical_dedupe": {
            "topic": req.topic,
            "active_peers": active_peers,
            "related_prs": related_prs,
            "solved": solved,
            "sources_pending": ["kg_entities"],
        },
    }
