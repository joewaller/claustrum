from fastapi import APIRouter, Depends, HTTPException

from app.auth import current_user

router = APIRouter()


@router.get("/inbox_drain")
async def inbox_drain(uid: str, user_email: str = Depends(current_user)):
    """Fetch pending events for this uid, marking delivered_at atomically.
    Client persists into ~/.claustrum/inbox/<uid>.json."""
    raise HTTPException(status_code=501, detail="not yet implemented")
