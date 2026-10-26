"""Intents — declared planned work.

Live presence (/v1/list peers) only covers sessions active in the same recency
window, so work someone has *planned* but not started is invisible. An intent
is a durable, value-scrubbed marker — a title plus repo-relative directory
prefixes — that /v1/list matches against a session's repo + paths (or topic)
regardless of recency, until it is closed or expires.

Privacy: a private intent, or one owned by a private session, is only ever
returned to its owner.
"""

from fastapi import APIRouter, Depends, HTTPException
from psycopg.types.json import Json

from app import db
from app.auth import current_user
from app.models import (
    INTENT_PATH_MAX,
    INTENT_PATHS_MAX,
    IntentCloseRequest,
    IntentCreateRequest,
    IntentUpdateRequest,
)

router = APIRouter()

_GLOB_CHARS = "*?[{"

# Session privacy is read from the hot+cold union so an owning session that
# the cold-archive job moved out of `sessions` still keeps its intent private.
_SESSION_PRIVATE = (
    "EXISTS (SELECT 1 FROM v_sessions_all s WHERE s.uid = i.uid AND s.is_private)"
)
_INTENT_COLS = (
    "i.id, i.user_email, i.uid, i.repo, i.title, i.paths, i.topic, i.domain, "
    "i.status, i.source, i.pr_number, i.is_private, i.created_at, "
    f"i.updated_at, i.expires_at, {_SESSION_PRIVATE} AS session_private"
)


def normalize_path(p: str) -> str | None:
    """Repo-relative directory/file prefix from a user- or plan-supplied path or
    glob. Globs are cut back to their last whole static segment
    (`src/**/*.py` -> `src`, `src/rou*` -> `src`); `..` and empty results are
    rejected. Returns None when nothing usable remains."""
    if not isinstance(p, str):
        return None
    p = p.strip().replace("\\", "/")
    cut = min((p.find(c) for c in _GLOB_CHARS if c in p), default=-1)
    if cut >= 0:
        p = p[:cut]
        if not p.endswith("/"):
            p = p.rpartition("/")[0]
    while p.startswith("./"):
        p = p[2:]
    p = p.strip("/")
    if not p or p == "." or ".." in p.split("/"):
        return None
    return p[:INTENT_PATH_MAX]


def normalize_paths(paths: list[str] | None) -> list[str]:
    out: list[str] = []
    for p in paths or []:
        n = normalize_path(p)
        if n and n not in out:
            out.append(n)
    return out[:INTENT_PATHS_MAX]


def path_area(p: str) -> str:
    """The directory a path stands for: a file (last segment has an extension)
    stands for its parent dir, anything else for itself. '' = repo root, which
    is never treated as a shared area (everyone co-tenants it)."""
    p = p.strip().strip("/")
    head, sep, last = p.rpartition("/")
    if "." in last.lstrip("."):
        return head if sep else ""
    return p


def shared_area(theirs: list[str], mine: list[str]) -> str | None:
    """First directory the two path sets share — equal, or one an ancestor of
    the other on a '/' boundary. Returns the broader of the pair (the common
    prefix), or None."""
    my_areas = {a for a in (path_area(m) for m in mine) if a}
    for t in theirs:
        ta = path_area(t)
        if not ta:
            continue
        for ma in my_areas:
            if ta == ma or ma.startswith(ta + "/"):
                return ta
            if ta.startswith(ma + "/"):
                return ma
    return None


def visible_intents(rows: list[dict], viewer_email: str) -> list[dict]:
    """Drop private intents (or intents owned by a private session) unless the
    viewer owns them. The SQL already filters these for other people; this is
    the belt-and-braces gate every returned list passes through."""
    return [
        r for r in rows
        if r["user_email"] == viewer_email
        or not (r.get("is_private") or r.get("session_private"))
    ]


def public_intent(row: dict) -> dict:
    return {
        "id": row["id"],
        "person": row["user_email"],
        "uid": row.get("uid"),
        "repo": row.get("repo"),
        "title": row["title"],
        "paths": list(row.get("paths") or []),
        "topic": row.get("topic"),
        "domain": row.get("domain"),
        "status": row.get("status"),
        "source": row.get("source"),
        "pr_number": row.get("pr_number"),
        "created_at": row.get("created_at"),
        "expires_at": row.get("expires_at"),
    }


def match_intents(
    candidates: list[dict],
    my_repo: str | None,
    my_topic: str | None,
    my_paths: list[str],
    limit: int = 5,
) -> list[dict]:
    """Rank open intents against this session: same repo AND a shared
    directory first (`match="path"`), then same topic (`match="topic"`).
    Same-repo intents with no path overlap and no shared topic are dropped —
    a repo-mate's unrelated plan isn't an overlap. Pure; no DB."""
    by_path: list[dict] = []
    by_topic: list[dict] = []
    for row in candidates:
        same_repo = my_repo is not None and row.get("repo") == my_repo
        shared = shared_area(list(row.get("paths") or []), my_paths) if same_repo else None
        if shared:
            entry = public_intent(row)
            entry["match"] = "path"
            entry["shared"] = shared
            by_path.append(entry)
        elif my_topic is not None and row.get("topic") == my_topic:
            entry = public_intent(row)
            entry["match"] = "topic"
            by_topic.append(entry)
    return (by_path + by_topic)[:limit]


async def fetch_intent_candidates(cur, user_email: str, my_repo, my_topic) -> list[dict]:
    """Other people's open, unexpired, non-private intents sharing my repo or
    topic — any age within their TTL (deliberately not recency-bounded)."""
    await cur.execute(
        f"""
        SELECT {_INTENT_COLS}
        FROM intents i
        WHERE i.status = 'open'
          AND i.expires_at > now()
          AND i.is_private = false
          AND NOT {_SESSION_PRIVATE}
          AND i.user_email <> %(me)s
          AND (
                (%(repo)s::text IS NOT NULL AND i.repo = %(repo)s)
             OR (%(topic)s::text IS NOT NULL AND i.topic = %(topic)s)
          )
        ORDER BY i.created_at DESC
        LIMIT 200
        """,
        {"me": user_email, "repo": my_repo, "topic": my_topic},
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in await cur.fetchall()]
    return visible_intents(rows, user_email)


async def _fetch_one(cur, intent_id: int) -> dict | None:
    await cur.execute(
        f"SELECT {_INTENT_COLS} FROM intents i "
        "WHERE i.id = %(id)s",
        {"id": intent_id},
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return dict(zip([d[0] for d in cur.description], row))


def _clean_title(title: str) -> str:
    title = " ".join(title.split())
    if not title:
        raise HTTPException(status_code=422, detail="title must not be blank")
    return title


@router.post("/intents")
async def create_intent(req: IntentCreateRequest, user_email: str = Depends(current_user)):
    """Declare planned work. Idempotent per (person, repo, title): re-declaring
    an open intent unions its paths and refreshes its expiry instead of adding
    a duplicate — plan mode can be exited several times for one plan."""
    title = _clean_title(req.title)
    paths = normalize_paths(req.paths)
    repo, topic, domain, is_private, uid = (
        req.repo, req.topic, req.domain, req.is_private, None,
    )

    async with db.conn() as c:
        async with c.cursor() as cur:
            if req.uid:
                await cur.execute(
                    "SELECT repo, topic, domain, is_private FROM sessions "
                    "WHERE uid = %(uid)s AND user_email = %(e)s",
                    {"uid": req.uid, "e": user_email},
                )
                sess = await cur.fetchone()
                if sess is not None:
                    uid = req.uid
                    repo = repo or sess[0]
                    topic = topic or sess[1]
                    domain = domain or sess[2]
                    is_private = is_private or bool(sess[3])

            await cur.execute(
                """
                SELECT id FROM intents
                WHERE user_email = %(e)s AND status = 'open'
                  AND repo IS NOT DISTINCT FROM %(repo)s
                  AND lower(title) = lower(%(title)s)
                ORDER BY created_at DESC LIMIT 1
                """,
                {"e": user_email, "repo": repo, "title": title},
            )
            existing = await cur.fetchone()
            params = {
                "e": user_email, "uid": uid, "repo": repo, "title": title,
                "paths": Json(paths), "topic": topic, "domain": domain,
                "pr": req.pr_number, "ttl": req.ttl_days,
                "private": is_private, "source": req.source,
            }
            if existing:
                params["id"] = existing[0]
                await cur.execute(
                    """
                    UPDATE intents SET
                        paths = (
                            SELECT COALESCE(jsonb_agg(DISTINCT e ORDER BY e), '[]'::jsonb)
                            FROM jsonb_array_elements(paths || %(paths)s::jsonb) AS e
                        ),
                        uid        = COALESCE(%(uid)s, uid),
                        topic      = COALESCE(%(topic)s, topic),
                        domain     = COALESCE(%(domain)s, domain),
                        pr_number  = COALESCE(%(pr)s, pr_number),
                        is_private = is_private OR %(private)s,
                        expires_at = GREATEST(expires_at, now() + make_interval(days => %(ttl)s)),
                        updated_at = now()
                    WHERE id = %(id)s
                    """,
                    params,
                )
                intent_id, created = existing[0], False
            else:
                await cur.execute(
                    """
                    INSERT INTO intents (user_email, uid, repo, title, paths, topic,
                                         domain, pr_number, is_private, source,
                                         expires_at)
                    VALUES (%(e)s, %(uid)s, %(repo)s, %(title)s, %(paths)s, %(topic)s,
                            %(domain)s, %(pr)s, %(private)s, %(source)s,
                            now() + make_interval(days => %(ttl)s))
                    RETURNING id
                    """,
                    params,
                )
                intent_id, created = (await cur.fetchone())[0], True
            row = await _fetch_one(cur, intent_id)
        await c.commit()

    return {"ok": True, "created": created, "intent": public_intent(row)}


async def _owned(cur, intent_id: int, user_email: str) -> dict:
    row = await _fetch_one(cur, intent_id)
    if row is None or row["user_email"] != user_email:
        raise HTTPException(status_code=404, detail="No intent with that id for the authenticated user.")
    return row


@router.post("/intents/{intent_id}")
async def update_intent(
    intent_id: int, req: IntentUpdateRequest, user_email: str = Depends(current_user)
):
    """Edit your own intent: title, paths (replaced), status, PR, or refresh
    its expiry. Only supplied fields change."""
    title = _clean_title(req.title) if req.title is not None else None
    paths = Json(normalize_paths(req.paths)) if req.paths is not None else None
    async with db.conn() as c:
        async with c.cursor() as cur:
            await _owned(cur, intent_id, user_email)
            await cur.execute(
                """
                UPDATE intents SET
                    title      = COALESCE(%(title)s, title),
                    paths      = COALESCE(%(paths)s::jsonb, paths),
                    status     = COALESCE(%(status)s, status),
                    pr_number  = COALESCE(%(pr)s, pr_number),
                    expires_at = CASE WHEN %(refresh)s
                                 THEN now() + make_interval(days => %(ttl)s)
                                 ELSE expires_at END,
                    updated_at = now()
                WHERE id = %(id)s
                """,
                {
                    "id": intent_id, "title": title, "paths": paths,
                    "status": req.status, "pr": req.pr_number,
                    "refresh": req.refresh, "ttl": req.ttl_days,
                },
            )
            row = await _fetch_one(cur, intent_id)
        await c.commit()
    return {"ok": True, "intent": public_intent(row)}


@router.post("/intents/{intent_id}/close")
async def close_intent(
    intent_id: int,
    req: IntentCloseRequest | None = None,
    user_email: str = Depends(current_user),
):
    """Mark your own intent done (default) or abandoned; it stops matching."""
    status = (req or IntentCloseRequest()).status
    async with db.conn() as c:
        async with c.cursor() as cur:
            await _owned(cur, intent_id, user_email)
            await cur.execute(
                "UPDATE intents SET status = %(s)s, updated_at = now() WHERE id = %(id)s",
                {"s": status, "id": intent_id},
            )
            row = await _fetch_one(cur, intent_id)
        await c.commit()
    return {"ok": True, "intent": public_intent(row)}


@router.get("/intents")
async def list_intents(
    mine: bool = False,
    repo: str | None = None,
    topic: str | None = None,
    include_closed: bool = False,
    limit: int = 50,
    user_email: str = Depends(current_user),
):
    """Browse intents, newest first. `mine=true` returns only yours (including
    private ones); otherwise yours plus everyone else's non-private ones.
    Closed / expired intents are hidden unless `include_closed=true`."""
    limit = max(1, min(limit, 200))
    async with db.conn() as c:
        async with c.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_INTENT_COLS}
                FROM intents i
                WHERE (
                        i.user_email = %(me)s
                     OR (NOT %(mine)s AND i.is_private = false
                         AND NOT {_SESSION_PRIVATE})
                  )
                  AND (%(closed)s OR (i.status = 'open' AND i.expires_at > now()))
                  AND (%(repo)s::text IS NULL OR i.repo = %(repo)s)
                  AND (%(topic)s::text IS NULL OR i.topic = %(topic)s)
                ORDER BY i.created_at DESC
                LIMIT %(limit)s
                """,
                {
                    "me": user_email, "mine": mine, "closed": include_closed,
                    "repo": repo, "topic": topic, "limit": limit,
                },
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in await cur.fetchall()]
    return {"intents": [public_intent(r) for r in visible_intents(rows, user_email)]}
