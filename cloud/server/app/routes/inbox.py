from fastapi import APIRouter, Depends, HTTPException

from app import db
from app.auth import current_user

router = APIRouter()


@router.get("/inbox_drain")
async def inbox_drain(uid: str, user_email: str = Depends(current_user)):
    """Atomically fetch-and-mark-delivered every pending message addressed to
    this session — direct (to_uid), or broadcast to its topic / repo — that it
    didn't send itself. The client persists these into
    ~/.claustrum/inbox/<uid>.json. This is the delivery path for server-emitted
    alerts (e.g. the topic-concentration job's 'topic-alert') and peer
    broadcasts.

    Draining is idempotent in effect: once a message's delivered_at is stamped
    it won't be returned again, so a second drain returns only what's new.
    Scoped to the authenticated user.
    """
    async with db.conn() as c:
        async with c.cursor() as cur:
            await cur.execute(
                "SELECT repo, topic FROM sessions WHERE uid = %(uid)s AND user_email = %(e)s",
                {"uid": uid, "e": user_email},
            )
            me = await cur.fetchone()
            if me is None:
                raise HTTPException(
                    status_code=404,
                    detail="No session with that uid for the authenticated user.",
                )
            my_repo, my_topic = me

            await cur.execute(
                """
                UPDATE messages SET delivered_at = now()
                WHERE delivered_at IS NULL
                  AND from_uid IS DISTINCT FROM %(uid)s
                  AND (
                        to_uid = %(uid)s
                     OR (%(topic)s::text IS NOT NULL AND to_topic = %(topic)s)
                     OR (%(repo)s::text  IS NOT NULL AND to_repo  = %(repo)s)
                  )
                RETURNING id, from_uid, to_uid, to_repo, to_topic, type, body,
                          metadata, created_at
                """,
                {"uid": uid, "topic": my_topic, "repo": my_repo},
            )
            cols = [d[0] for d in cur.description]
            messages = [dict(zip(cols, r)) for r in await cur.fetchall()]

        await c.commit()

    return {"uid": uid, "count": len(messages), "messages": messages}
