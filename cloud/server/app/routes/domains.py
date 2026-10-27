import hashlib

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app import db
from app.aliases import topic_join
from app.auth import current_user
from app.models import (
    DomainEntry,
    DomainsResponse,
    RegisterDomainRequest,
    RegisterDomainResponse,
)
from app.cache import cached
from app.routes.topics import COUNTS_CACHE_SECONDS, _require_registrar, fetch_rows

router = APIRouter()

# Mirror topics: short client cache window, ETag revalidation past it. The
# domain taxonomy changes even less than topics, so this is cheap and safe.
_DOMAINS_CACHE_CONTROL = "private, max-age=60"


def _etag(rows) -> str:
    """Strong ETag over the full domain payload — any name/description/parent/
    source change busts it. Stable across requests when nothing changed."""
    h = hashlib.sha256()
    for r in rows:
        h.update(repr(tuple(r)).encode())
        h.update(b"\x00")
    return '"' + h.hexdigest()[:24] + '"'


# Usage-only approved aliases, as in /v1/topics.
_ALIAS_FROM = "FROM taxonomy_aliases a JOIN domains c ON c.name = a.canonical"
_ALIAS_WHERE = """
    WHERE a.kind = 'domain' AND a.status = 'approved'
      AND NOT EXISTS (SELECT 1 FROM domains x WHERE x.name = a.alias)
"""

_DOMAINS_SQL = f"""
    SELECT d.name, d.description, d.parent, d.source, a.canonical
    FROM domains d
    LEFT JOIN taxonomy_aliases a
           ON a.kind = 'domain' AND a.alias = d.name AND a.status = 'approved'
    UNION ALL
    SELECT a.alias, c.description, a.canonical, 'merged', a.canonical
    {_ALIAS_FROM} {_ALIAS_WHERE}
    ORDER BY 1
"""

# Cold rows carry no domain column, so they count through their topic's domain.
_DOMAINS_WITH_COUNTS_SQL = f"""
    WITH tagged AS (
        SELECT s.user_email, COALESCE(s.domain, t.domain) AS domain
        FROM sessions s
        {topic_join("s.topic")}
        WHERE s.is_private = false
        UNION ALL
        SELECT a.user_email, t.domain
        FROM sessions_archive a
        {topic_join("a.topic", join="JOIN")}
        WHERE a.is_private = false
    ),
    usage AS (
        -- Usage under an approved domain alias counts toward its canonical.
        SELECT COALESCE(da.canonical, g.domain) AS name,
               count(DISTINCT g.user_email)     AS adopters,
               count(*)                         AS sessions
        FROM tagged g
        LEFT JOIN taxonomy_aliases da
               ON da.kind = 'domain' AND da.alias = g.domain AND da.status = 'approved'
        WHERE g.domain IS NOT NULL
        GROUP BY 1
    )
    SELECT d.name, d.description, d.parent, d.source, a.canonical,
           COALESCE(u.adopters, 0), COALESCE(u.sessions, 0)
    FROM domains d
    LEFT JOIN taxonomy_aliases a
           ON a.kind = 'domain' AND a.alias = d.name AND a.status = 'approved'
    LEFT JOIN usage u ON u.name = d.name
    UNION ALL
    SELECT a.alias, c.description, a.canonical, 'merged', a.canonical,
           COALESCE(u.adopters, 0), COALESCE(u.sessions, 0)
    {_ALIAS_FROM} LEFT JOIN usage u ON u.name = a.alias {_ALIAS_WHERE}
    ORDER BY 1
"""


@router.get("/domains")
async def list_domains(
    request: Request,
    response: Response,
    with_counts: bool = False,
    user_email: str = Depends(current_user),
):
    """Return the full canonical domain taxonomy. Read-only; available to any
    authenticated caller. A classifying sub-agent reads this (via `claustrum
    domains`) to pick or propose a domain.

    ETag + If-None-Match: an unchanged taxonomy returns 304 with no body.
    `alias_of` / opt-in `with_counts=1` mirror /v1/topics."""
    if with_counts:
        rows = await cached("domains_with_counts", COUNTS_CACHE_SECONDS,
                            lambda: fetch_rows(_DOMAINS_WITH_COUNTS_SQL))
    else:
        rows = await fetch_rows(_DOMAINS_SQL)

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
            DomainEntry(
                name=r[0], description=r[1], parent=r[2], source=r[3],
                alias_of=r[4],
                adopters=r[5] if with_counts else None,
                sessions=r[6] if with_counts else None,
            )
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
