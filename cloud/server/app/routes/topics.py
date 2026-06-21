import os

from fastapi import APIRouter, Depends, Header, HTTPException

from app import db
from app.auth import current_user
from app.models import (
    RegisterTopicRequest,
    RegisterTopicResponse,
    TopicEntry,
    TopicsResponse,
)

router = APIRouter()


def _require_registrar(
    x_claustrum_registrar_secret: str | None = Header(default=None),
) -> bool:
    """Gate for the trusted write-through registrar (the memory-enhanced MCP).

    Claustrum's taxonomy is normally *emergent* — names go through
    `propose_topic` and only promote at PROMOTION_THRESHOLD distinct users, so
    one-off names can't pollute it. `register` bypasses that gate, so it's
    restricted to callers holding the shared secret. Disabled (403) by default:
    if CLAUSTRUM_REGISTRAR_SECRET is unset, nobody can register, which keeps the
    emergent gate as the only write path until an operator opts in.
    """
    expected = os.environ.get("CLAUSTRUM_REGISTRAR_SECRET")
    if not expected:
        raise HTTPException(
            status_code=403,
            detail="Topic registration is disabled (no CLAUSTRUM_REGISTRAR_SECRET set).",
        )
    if not x_claustrum_registrar_secret or x_claustrum_registrar_secret != expected:
        raise HTTPException(status_code=403, detail="Invalid or missing registrar secret.")
    return True


@router.get("/topics", response_model=TopicsResponse)
async def list_topics(user_email: str = Depends(current_user)) -> TopicsResponse:
    """Return the full canonical taxonomy. Read-only; available to any
    authenticated caller. Consumers (e.g. the memory-enhanced MCP) cache this
    and resolve their derived topic names against it, collapsing variants via
    `parent`."""
    async with db.conn() as c:
        async with c.cursor() as cur:
            await cur.execute(
                "SELECT name, description, parent, source FROM topics ORDER BY name"
            )
            rows = await cur.fetchall()
    return TopicsResponse(
        topics=[
            TopicEntry(name=r[0], description=r[1], parent=r[2], source=r[3])
            for r in rows
        ]
    )


@router.post("/topics/register", response_model=RegisterTopicResponse)
async def register_topic(
    req: RegisterTopicRequest,
    user_email: str = Depends(current_user),
    _registrar: bool = Depends(_require_registrar),
) -> RegisterTopicResponse:
    """Write-through path for a trusted registrar: add a canonical topic if it
    doesn't already exist. Idempotent — re-registering an existing name is a
    no-op and returns created=false. Stamped source='proposed' + promoted_at so
    it's immediately part of the official taxonomy (the registrar is trusted; it
    doesn't need the distinct-user promotion gate)."""
    name = req.name.strip().lower()
    if not name:
        raise HTTPException(status_code=422, detail="name must be non-empty")
    if not req.description.strip():
        raise HTTPException(status_code=422, detail="description must be non-empty")

    async with db.conn() as c:
        async with c.cursor() as cur:
            if req.parent:
                await cur.execute(
                    "SELECT 1 FROM topics WHERE name = %(p)s", {"p": req.parent}
                )
                if await cur.fetchone() is None:
                    raise HTTPException(
                        status_code=422,
                        detail=f"parent '{req.parent}' is not a known topic",
                    )
            await cur.execute(
                """
                INSERT INTO topics (name, description, source, parent, promoted_at)
                VALUES (%(name)s, %(desc)s, 'proposed', %(parent)s, now())
                ON CONFLICT (name) DO NOTHING
                RETURNING name
                """,
                {"name": name, "desc": req.description.strip(), "parent": req.parent},
            )
            created = await cur.fetchone() is not None
        await c.commit()

    return RegisterTopicResponse(ok=True, name=name, created=created)
