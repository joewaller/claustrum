from fastapi import APIRouter, Depends, HTTPException

from app import db
from app.auth import current_user

router = APIRouter()


@router.get("/resume_check")
async def resume_check(uid: str, user_email: str = Depends(current_user)):
    """SessionStart summary for a resuming session: how long it's been idle and
    what changed in its repo/topic while it was away —

      - peer_activity : other live peers in my repo/topic seen since I last was
      - merged_prs    : distinct PRs on peers that finished ('done') since then
      - expired_claims: how many of my own soft claims lapsed while away

    Read-only. Scoped to the authenticated user.
    """
    async with db.conn() as c:
        async with c.cursor() as cur:
            await cur.execute(
                """
                SELECT repo, topic, last_seen,
                       extract(epoch FROM now() - last_seen)::int AS paused_for
                FROM sessions WHERE uid = %(uid)s AND user_email = %(e)s
                """,
                {"uid": uid, "e": user_email},
            )
            me = await cur.fetchone()
            if me is None:
                raise HTTPException(
                    status_code=404,
                    detail="No session with that uid for the authenticated user.",
                )
            my_repo, my_topic, last_seen, paused_for = me

            # Peers active since I was last seen, sharing my repo or topic.
            await cur.execute(
                """
                SELECT uid, user_email, repo, topic, working_on, last_seen
                FROM sessions
                WHERE uid <> %(uid)s AND is_private = false
                  AND status IN ('active', 'paused')
                  AND last_seen > %(since)s
                  AND ((%(repo)s::text  IS NOT NULL AND repo  = %(repo)s)
                    OR (%(topic)s::text IS NOT NULL AND topic = %(topic)s))
                ORDER BY last_seen DESC
                LIMIT 10
                """,
                {"uid": uid, "since": last_seen, "repo": my_repo, "topic": my_topic},
            )
            cols = [d[0] for d in cur.description]
            peer_activity = [dict(zip(cols, r)) for r in await cur.fetchall()]

            # PRs on peers that finished in my repo/topic since I left.
            await cur.execute(
                """
                SELECT DISTINCT repo, pr_number, user_email
                FROM sessions
                WHERE uid <> %(uid)s AND is_private = false
                  AND status = 'done' AND pr_number IS NOT NULL
                  AND done_at > %(since)s
                  AND ((%(repo)s::text  IS NOT NULL AND repo  = %(repo)s)
                    OR (%(topic)s::text IS NOT NULL AND topic = %(topic)s))
                ORDER BY repo, pr_number
                LIMIT 20
                """,
                {"uid": uid, "since": last_seen, "repo": my_repo, "topic": my_topic},
            )
            merged_prs = [
                {"repo": r[0], "pr_number": r[1], "user_email": r[2]}
                for r in await cur.fetchall()
            ]

            # My own soft claims that have since expired.
            await cur.execute(
                "SELECT count(*) FROM claims WHERE uid = %(uid)s AND expires_at < now()",
                {"uid": uid},
            )
            expired_claims = (await cur.fetchone())[0]

    return {
        "uid": uid,
        "paused_for_seconds": paused_for,
        "since_last_seen": {
            "merged_prs": merged_prs,
            "peer_activity": peer_activity,
            "expired_claims": expired_claims,
        },
    }
