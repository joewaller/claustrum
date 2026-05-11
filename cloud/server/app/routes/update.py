from fastapi import APIRouter, Depends, HTTPException

from app.auth import current_user
from app.models import OkResponse, UpdateRequest

router = APIRouter()


@router.post("/update", response_model=OkResponse)
async def update(req: UpdateRequest, user_email: str = Depends(current_user)) -> OkResponse:
    raise HTTPException(status_code=501, detail="not yet implemented")
