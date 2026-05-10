from fastapi import APIRouter, Depends, HTTPException

from app.auth import current_user

router = APIRouter()


@router.post("/reset")
async def reset(user_email: str = Depends(current_user)):
    """Per-user wipe: DELETE every row in sessions/messages/claims/
    topic_proposals where user_email = caller. Does NOT purge BQ archive."""
    raise HTTPException(status_code=501, detail="not yet implemented")
