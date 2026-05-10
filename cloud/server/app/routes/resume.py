from fastapi import APIRouter, Depends, HTTPException

from app.auth import current_user

router = APIRouter()


@router.get("/resume_check")
async def resume_check(uid: str, user_email: str = Depends(current_user)):
    """Returns paused_for_seconds + since_last_seen.{merged_prs, peer_activity,
    expired_claims}. Called by SessionStart hook on resume."""
    raise HTTPException(status_code=501, detail="not yet implemented")
