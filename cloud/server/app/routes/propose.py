from fastapi import APIRouter, Depends, HTTPException

from app.auth import current_user
from app.models import ProposeTopicRequest

router = APIRouter()


@router.post("/propose_topic")
async def propose_topic(req: ProposeTopicRequest, user_email: str = Depends(current_user)):
    """Queue a topic proposal. Hourly job promotes when at least 3 distinct
    user_emails have proposed the same name."""
    raise HTTPException(status_code=501, detail="not yet implemented")
