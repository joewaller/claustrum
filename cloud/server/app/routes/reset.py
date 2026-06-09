from fastapi import APIRouter, Depends

from app import db
from app.auth import current_user

router = APIRouter()


@router.post("/reset")
async def reset(user_email: str = Depends(current_user)):
    """Per-user wipe. Deletes everything this user owns:
      - sessions / topic_proposals  — keyed by user_email directly
      - claims / messages           — keyed by the user's session uids

    Messages are scoped to those this user *sent* (from_uid in their sessions);
    messages they merely received are other users' content and are left intact.
    Does NOT touch the shared `topics` taxonomy or any BQ archive (none wired).
    Idempotent — a user with no rows gets all-zero counts.

    Scoped to the authenticated user; there is no way to reset another user.
    """
    async with db.conn() as c:
        async with c.cursor() as cur:
            await cur.execute(
                "SELECT uid FROM sessions WHERE user_email = %(e)s",
                {"e": user_email},
            )
            uids = [r[0] for r in await cur.fetchall()]

            claims_deleted = 0
            messages_deleted = 0
            if uids:
                await cur.execute(
                    "DELETE FROM claims WHERE uid = ANY(%(uids)s)", {"uids": uids}
                )
                claims_deleted = cur.rowcount
                await cur.execute(
                    "DELETE FROM messages WHERE from_uid = ANY(%(uids)s)", {"uids": uids}
                )
                messages_deleted = cur.rowcount

            await cur.execute(
                "DELETE FROM topic_proposals WHERE user_email = %(e)s", {"e": user_email}
            )
            proposals_deleted = cur.rowcount

            # Delete sessions last — the uid list above is derived from it.
            await cur.execute(
                "DELETE FROM sessions WHERE user_email = %(e)s", {"e": user_email}
            )
            sessions_deleted = cur.rowcount

        await c.commit()

    return {
        "ok": True,
        "deleted": {
            "sessions": sessions_deleted,
            "messages": messages_deleted,
            "claims": claims_deleted,
            "topic_proposals": proposals_deleted,
        },
    }
