from fastapi import APIRouter, Depends, HTTPException

from app.auth import current_user
from app.models import ClaimRequest, OkResponse, ReleaseRequest

router = APIRouter()


@router.post("/claim")
async def claim(req: ClaimRequest, user_email: str = Depends(current_user)):
    """Soft claim — recorded but not enforced. Returns existing peer
    conflicts for the same (repo, rel_path)."""
    raise HTTPException(status_code=501, detail="not yet implemented")


@router.post("/release", response_model=OkResponse)
async def release(req: ReleaseRequest, user_email: str = Depends(current_user)) -> OkResponse:
    raise HTTPException(status_code=501, detail="not yet implemented")
