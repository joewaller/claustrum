from fastapi import APIRouter, Depends, HTTPException

from app import db
from app.auth import current_user
from app.models import ClaimRequest, OkResponse, ReleaseRequest

router = APIRouter()


async def _owns_session(cur, uid: str, user_email: str) -> bool:
    await cur.execute(
        "SELECT 1 FROM sessions WHERE uid = %(uid)s AND user_email = %(e)s",
        {"uid": uid, "e": user_email},
    )
    return await cur.fetchone() is not None


@router.post("/claim")
async def claim(req: ClaimRequest, user_email: str = Depends(current_user)):
    """Soft, TTL'd file claim — recorded for cross-machine visibility, never
    enforced as a hard lock. Upserts the (repo, rel_path, uid) claim with a
    fresh expiry and returns any *other* live (non-expired, non-private) peers
    holding a claim on the same path so the caller can coordinate before
    editing.

    Scoped to the authenticated user — the uid must be one of your sessions.
    """
    async with db.conn() as c:
        async with c.cursor() as cur:
            if not await _owns_session(cur, req.uid, user_email):
                raise HTTPException(
                    status_code=404,
                    detail="No session with that uid for the authenticated user.",
                )

            # Conflicts first: other uids holding a non-expired claim on this path.
            await cur.execute(
                """
                SELECT c.uid, s.user_email, s.working_on, c.expires_at
                FROM claims c JOIN sessions s ON s.uid = c.uid
                WHERE c.repo = %(repo)s AND c.rel_path = %(rel_path)s
                  AND c.uid <> %(uid)s
                  AND c.expires_at > now()
                  AND s.is_private = false
                ORDER BY c.claimed_at
                """,
                {"repo": req.repo, "rel_path": req.rel_path, "uid": req.uid},
            )
            conflicts = [
                {
                    "uid": r[0],
                    "user_email": r[1],
                    "working_on": r[2],
                    "expires_at": r[3],
                }
                for r in await cur.fetchall()
            ]

            await cur.execute(
                """
                INSERT INTO claims (uid, repo, rel_path, claimed_at, expires_at)
                VALUES (%(uid)s, %(repo)s, %(rel_path)s, now(),
                        now() + make_interval(secs => %(ttl)s))
                ON CONFLICT (repo, rel_path, uid) DO UPDATE SET
                    claimed_at = now(),
                    expires_at = now() + make_interval(secs => %(ttl)s)
                RETURNING expires_at
                """,
                {
                    "uid": req.uid,
                    "repo": req.repo,
                    "rel_path": req.rel_path,
                    "ttl": req.ttl_seconds,
                },
            )
            expires_at = (await cur.fetchone())[0]

        await c.commit()

    return {
        "ok": True,
        "claimed": {
            "repo": req.repo,
            "rel_path": req.rel_path,
            "expires_at": expires_at,
        },
        "conflicts": conflicts,
    }


@router.post("/release", response_model=OkResponse)
async def release(
    req: ReleaseRequest, user_email: str = Depends(current_user)
) -> OkResponse:
    """Release a soft claim you hold. Scoped to your own session uid; deleting a
    claim that doesn't exist is still ok (idempotent)."""
    async with db.conn() as c:
        async with c.cursor() as cur:
            if not await _owns_session(cur, req.uid, user_email):
                raise HTTPException(
                    status_code=404,
                    detail="No session with that uid for the authenticated user.",
                )
            await cur.execute(
                """
                DELETE FROM claims
                WHERE uid = %(uid)s AND repo = %(repo)s AND rel_path = %(rel_path)s
                """,
                {"uid": req.uid, "repo": req.repo, "rel_path": req.rel_path},
            )
        await c.commit()

    return OkResponse()
