import hashlib

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app import db
from app.auth import current_user
from app.models import (
    DomainEntry,
    DomainsResponse,
    RegisterDomainRequest,
    RegisterDomainResponse,
)
from app.routes.topics import _require_registrar

router = APIRouter()

# Mirror topics: short client cache window, ETag revalidation past it. The
# domain taxonomy changes even less than topics, so this is cheap and safe.
_DOMAINS_CACHE_CONTROL = "private, max-age=60"


def _etag(rows) -> str:
    """Strong ETag over the full domain payload — any name/description/parent/
    source change busts it. Stable across requests when nothing changed."""
    h = hashlib.sha256()
    for r in rows:
        h.update(repr((r[0], r[1], r[2], r[3])).encode())
        h.update(b"\x00")
    return '"' + h.hexdigest()[:24] + '"'


@router.get("/domains")
async def list_domains(
    request: Request,
    response: Response,
    user_email: str = Depends(current_user),
):
    """Return the full canonical domain taxonomy. Read-only; available to any
    authenticated caller. A classifying sub-agent reads this (via `claustrum
    domains`) to pick or propose a domain.

    ETag + If-None-Match: an unchanged taxonomy returns 304 with no body."""
    async with db.conn() as c:
        async with c.cursor() as cur:
            await cur.execute(
                "SELECT name, description, parent, source FROM domains ORDER BY name"
            )
            rows = await cur.fetchall()

    etag = _etag(rows)
    if request.headers.get("if-none-match") == etag:
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": _DOMAINS_CACHE_CONTROL},
        )

    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = _DOMAINS_CACHE_CONTROL
    return DomainsResponse(
        domains=[
            DomainEntry(name=r[0], description=r[1], parent=r[2], source=r[3])
            for r in rows
        ]
    )


@router.post("/domains/register", response_model=RegisterDomainResponse)
async def register_domain(
    req: RegisterDomainRequest,
    user_email: str = Depends(current_user),
    _registrar: bool = Depends(_require_registrar),
) -> RegisterDomainResponse:
    """Write-through path for a trusted registrar: add a canonical domain if it
    doesn't already exist. Idempotent — re-registering an existing name is a
    no-op and returns created=false. Stamped source='proposed' + promoted_at so
    it's immediately part of the official taxonomy (the registrar is trusted; it
    doesn't need the distinct-user promotion gate). Gated by the same registrar
    secret as topics (_require_registrar)."""
    name = req.name.strip().lower()
    if not name:
        raise HTTPException(status_code=422, detail="name must be non-empty")
    if not req.description.strip():
        raise HTTPException(status_code=422, detail="description must be non-empty")

    async with db.conn() as c:
        async with c.cursor() as cur:
            if req.parent:
                await cur.execute(
                    "SELECT 1 FROM domains WHERE name = %(p)s", {"p": req.parent}
                )
                if await cur.fetchone() is None:
                    raise HTTPException(
                        status_code=422,
                        detail=f"parent '{req.parent}' is not a known domain",
                    )
            await cur.execute(
                """
                INSERT INTO domains (name, description, source, parent, promoted_at)
                VALUES (%(name)s, %(desc)s, 'proposed', %(parent)s, now())
                ON CONFLICT (name) DO NOTHING
                RETURNING name
                """,
                {"name": name, "desc": req.description.strip(), "parent": req.parent},
            )
            created = await cur.fetchone() is not None
        await c.commit()

    return RegisterDomainResponse(ok=True, name=name, created=created)
