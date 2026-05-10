from fastapi import APIRouter, Depends, HTTPException

from app.auth import current_user

router = APIRouter()


@router.get("/list")
async def list_peers(
    uid: str,
    repo: str | None = None,
    topic: str | None = None,
    files_touched: str | None = None,
    recency_min: int = 10,
    include_paused: bool = False,
    tier_max: int = 4,
    user_email: str = Depends(current_user),
):
    """Per-turn peer query. Returns tiered overlap: tier-1 file overlap,
    tier-2 path-prefix or PR, tier-3 topic (count + top-3), tier-4 repo
    (count + top-1). Tier-5 cross-repo never returned."""
    raise HTTPException(status_code=501, detail="not yet implemented")
