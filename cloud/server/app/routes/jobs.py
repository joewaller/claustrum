from fastapi import APIRouter, HTTPException

router = APIRouter()


# Note: job endpoints are intentionally NOT protected by current_user.
# Production deployments authenticate them via OIDC tokens signed by the
# scheduler's service account, validated separately — out of scope for the
# scaffold; will be wired in a later PR.


@router.post("/state-transitions")
async def state_transitions():
    """Every 5 minutes. active -> paused on stale heartbeat or no
    activity-bearing event in 60 minutes. Expire stale claims."""
    raise HTTPException(status_code=501, detail="not yet implemented")


@router.post("/topic-concentration")
async def topic_concentration():
    """Hourly. Detect topics with >=3 active sessions and emit topic-alert
    messages."""
    raise HTTPException(status_code=501, detail="not yet implemented")


@router.post("/validate-proposals")
async def validate_proposals():
    """Hourly. Promote proposals with >=3 distinct user_emails; reject
    proposals older than 7 days with low count."""
    raise HTTPException(status_code=501, detail="not yet implemented")


@router.post("/dedupe-digest")
async def dedupe_digest():
    """Hourly. Re-emit historical_dedupe payload to recently-classified
    sessions if new historical matches appeared since classification."""
    raise HTTPException(status_code=501, detail="not yet implemented")


@router.post("/recluster")
async def recluster():
    """Daily. Re-cluster sessions with topic IS NULL and non-empty task
    against the current taxonomy."""
    raise HTTPException(status_code=501, detail="not yet implemented")


@router.post("/topic-merge")
async def topic_merge():
    """Daily. Detect topic pairs with Jaccard overlap >0.5 over user_email
    sets last 60d. Emits proposals; never silently merges."""
    raise HTTPException(status_code=501, detail="not yet implemented")


@router.post("/archive-to-bq")
async def archive_to_bq():
    """Daily. Stream sessions older than 30d (or status='done') to
    BigQuery, then delete from PG. Fail-closed."""
    raise HTTPException(status_code=501, detail="not yet implemented")
