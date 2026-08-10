from fastapi import APIRouter, Depends, HTTPException

from app import db
from app.auth import current_user
from app.models import SendRequest

router = APIRouter()


@router.post("/send")
async def send(req: SendRequest, user_email: str = Depends(current_user)):
    """Deliver a directed message to another person's session(s).

    Addressed by `to_email` (every live session of that person — robust to the
    session uid churning) and/or `to_uid` (one specific session). The sender's
    identity is stamped from the authenticated caller as `from_email`, so the
    recipient can reply to the person, not to an ephemeral uid. The recipient
    picks the message up via /v1/inbox_drain on its next prompt.

    At least one target (to_uid or to_email) is required. There is deliberately
    no broadcast-to-everyone here: server-emitted topic/repo alerts cover fan-out;
    a person addresses a person or a specific session, nothing wider.
    """
    if not (req.to_uid or req.to_email):
        raise HTTPException(status_code=400, detail="Provide to_uid and/or to_email.")

    to_email = req.to_email.strip().lower() if req.to_email else None

    async with db.conn() as c:
        async with c.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO messages
                    (from_uid, from_email, to_uid, to_email, type, body)
                VALUES
                    (%(from_uid)s, %(from_email)s, %(to_uid)s, %(to_email)s,
                     %(type)s, %(body)s)
                RETURNING id
                """,
                {
                    "from_uid": req.from_uid,
                    "from_email": user_email,
                    "to_uid": req.to_uid,
                    "to_email": to_email,
                    "type": req.type,
                    "body": req.body,
                },
            )
            row = await cur.fetchone()
        await c.commit()

    return {"ok": True, "id": row[0] if row else None}
