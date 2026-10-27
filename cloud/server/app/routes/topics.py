import hashlib
import os

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from app import db
from app.auth import current_user
from app.cache import cached
from app.models import (
    RegisterTopicRequest,
    RegisterTopicResponse,
    TopicEntry,
    TopicsResponse,
)

router = APIRouter()

# Short client/intermediary cache window; ETag revalidation handles correctness
# past it. The taxonomy changes rarely, so this is safe and keeps hundreds of
# resolve-only clients cheap.
_TOPICS_CACHE_CONTROL = "private, max-age=60"
# Usage counts aggregate all session history; the classifier only needs them
# roughly right, so one aggregation per instance per window serves every caller.
COUNTS_CACHE_SECONDS = 300.0


async def fetch_rows(sql: str):
    async with db.conn() as c:
        async with c.cursor() as cur:
            await cur.execute(sql)
            return await cur.fetchall()


def _etag(rows) -> str:
    """Strong ETag over the full taxonomy payload — any name/description/parent/
    source/domain change busts it. Stable across requests when nothing changed."""
    h = hashlib.sha256()
    for r in rows:
        h.update(repr(tuple(r)).encode())
        h.update(b"\x00")
    return '"' + h.hexdigest()[:24] + '"'


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


# An approved alias with no topics row of its own (a free-text name folded by a
# merge) is listed too, so a pick of that name resolves instead of being minted.
# `parent` is set as well for consumers that only collapse variants via parent.
_ALIAS_FROM = "FROM taxonomy_aliases a JOIN topics c ON c.name = a.canonical"
_ALIAS_WHERE = """
    WHERE a.kind = 'topic' AND a.status = 'approved'
      AND NOT EXISTS (SELECT 1 FROM topics x WHERE x.name = a.alias)
"""

_TOPICS_SQL = f"""
    SELECT t.name, t.description, t.parent, t.source, t.domain, a.canonical
    FROM topics t
    LEFT JOIN taxonomy_aliases a
           ON a.kind = 'topic' AND a.alias = t.name AND a.status = 'approved'
    UNION ALL
    SELECT a.alias, c.description, a.canonical, 'merged', c.domain, a.canonical
    {_ALIAS_FROM} {_ALIAS_WHERE}
    ORDER BY 1
"""

_TOPICS_WITH_COUNTS_SQL = f"""
    WITH usage AS (
        -- Usage under an approved alias counts toward its canonical, whose row
        -- is the one the classifier ranks.
        SELECT COALESCE(ta.canonical, v.topic) AS name,
               count(DISTINCT v.user_email)   AS adopters,
               count(*)                       AS sessions
        FROM v_sessions_all v
        LEFT JOIN taxonomy_aliases ta
               ON ta.kind = 'topic' AND ta.alias = v.topic AND ta.status = 'approved'
        WHERE v.topic IS NOT NULL AND v.is_private = false
        GROUP BY 1
    )
    SELECT t.name, t.description, t.parent, t.source, t.domain, a.canonical,
           COALESCE(u.adopters, 0), COALESCE(u.sessions, 0)
    FROM topics t
    LEFT JOIN taxonomy_aliases a
           ON a.kind = 'topic' AND a.alias = t.name AND a.status = 'approved'
    LEFT JOIN usage u ON u.name = t.name
    UNION ALL
    SELECT a.alias, c.description, a.canonical, 'merged', c.domain, a.canonical,
           COALESCE(u.adopters, 0), COALESCE(u.sessions, 0)
    {_ALIAS_FROM} LEFT JOIN usage u ON u.name = a.alias {_ALIAS_WHERE}
    ORDER BY 1
"""


@router.get("/topics")
async def list_topics(
    request: Request,
    response: Response,
    with_counts: bool = False,
    user_email: str = Depends(current_user),
):
    """Return the full canonical taxonomy. Read-only; available to any
    authenticated caller. Consumers (e.g. the memory-enhanced MCP) cache this
    and resolve their derived topic names against it, collapsing variants via
    `parent`.

    ETag + If-None-Match: an unchanged taxonomy returns 304 with no body, so the
    steady-state refresh across hundreds of resolve-only clients is a tiny
    revalidation rather than a full payload + parse every cycle.

    `alias_of` carries an approved merge's canonical name. `with_counts=1` adds
    adopters/sessions so the classifier can rank candidates; it is opt-in so
    the default payload (and its ETag) stays stable for cache-heavy consumers."""
    if with_counts:
        rows = await cached("topics_with_counts", COUNTS_CACHE_SECONDS,
                            lambda: fetch_rows(_TOPICS_WITH_COUNTS_SQL))
    else:
        rows = await fetch_rows(_TOPICS_SQL)

    etag = _etag(rows)
    if request.headers.get("if-none-match") == etag:
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": _TOPICS_CACHE_CONTROL},
        )

    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = _TOPICS_CACHE_CONTROL
    return TopicsResponse(
        topics=[
            TopicEntry(
                name=r[0], description=r[1], parent=r[2], source=r[3], domain=r[4],
                alias_of=r[5],
                adopters=r[6] if with_counts else None,
                sessions=r[7] if with_counts else None,
            )
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
    # topics.domain is NOT NULL. Default to 'general' when the caller omits it
    # (the memory-enhanced registrar doesn't send a domain yet — Phase 3).
    domain = (req.domain or "general").strip().lower()

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
                "SELECT 1 FROM domains WHERE name = %(d)s", {"d": domain}
            )
            if await cur.fetchone() is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"domain '{domain}' is not a known domain",
                )
            await cur.execute(
                """
                INSERT INTO topics (name, description, source, parent, domain, promoted_at)
                VALUES (%(name)s, %(desc)s, 'proposed', %(parent)s, %(domain)s, now())
                ON CONFLICT (name) DO NOTHING
                RETURNING name
                """,
                {
                    "name": name,
                    "desc": req.description.strip(),
                    "parent": req.parent,
                    "domain": domain,
                },
            )
            created = await cur.fetchone() is not None
        await c.commit()

    return RegisterTopicResponse(ok=True, name=name, created=created)
