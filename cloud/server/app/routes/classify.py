from fastapi import APIRouter, Depends, HTTPException

from app.auth import current_user
from app.models import ClassifySelfRequest

router = APIRouter()


@router.post("/classify_self")
async def classify_self(req: ClassifySelfRequest, user_email: str = Depends(current_user)):
    """Set the session's topic. Returns historical_dedupe payload — KG
    entities, merged PRs in same repo+topic last 90d, archived peer sessions
    in same topic cluster."""
    raise HTTPException(status_code=501, detail="not yet implemented")
