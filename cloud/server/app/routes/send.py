from fastapi import APIRouter, Depends, HTTPException

from app import db
from app.auth import current_user
from app.models import SendRequest

router = APIRouter()


@router.post("/send")
async def send(req: SendRequest, user_email: str = Depends(current_user)):
    """Deliver a directed message to another person's session(s).

    Addressed by `to_email` (delivered to ONE of that person's live sessions —
    whichever drains it first; robust to the session uid churning) and/or
    `to_uid` (one specific session). The sender's
    identity is stamped from the authenticated caller as `from_email`, so the
    recipient can reply to the person, not to an ephemeral uid. The recipient
    picks the message up via /v1/inbox_drain on its next prompt.

    At least one target (to_uid or to_email) is required. There is deliberately
    no broadcast-to-everyone here: server-emitted topic/repo alerts cover fan-out;
    a person addresses a person or a specific session, nothing wider.
    """
    # Normalise blanks to NULL so an empty "" never lands in the column (where
    # it could become a stray match key for the inbox drain) and so the
    # target-required guard can't be satisfied by whitespace.
    to_uid = (req.to_uid or "").strip() or None
    to_email = (req.to_email or "").strip().lower() or None
    if not (to_uid or to_email):
        raise HTTPException(status_code=400, detail="Provide to_uid and/or to_email.")

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
                    "from_uid": (req.from_uid or "").strip() or None,
                    "from_email": user_email,
                    "to_uid": to_uid,
                    "to_email": to_email,
                    "type": req.type,
                    "body": req.body,
                },
            )
            row = await cur.fetchone()
        await c.commit()

    return {"ok": True, "id": row[0] if row else None}
