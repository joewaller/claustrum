from fastapi import APIRouter, Depends, HTTPException

from app import db
from app.auth import current_user

router = APIRouter()


def _parents(paths: list[str]) -> set[str]:
    """Immediate parent directory of each path. 'src/routes/list.py' ->
    'src/routes'; a top-level file -> '' (treated as no shared dir)."""
    out: set[str] = set()
    for p in paths:
        p = p.strip()
        if not p:
            continue
        head, sep, _ = p.rpartition("/")
        if sep:
            out.add(head)
    return out


def _peer(row: dict) -> dict:
    """Presence summary for the board — never includes raw detail beyond
    working_on (the curated, value-scrubbed line)."""
    return {
        "uid": row["uid"],
        "user_email": row["user_email"],
        "machine": row["machine"],
        "repo": row["repo"],
        "branch": row["branch"],
        "topic": row["topic"],
        "status": row["status"],
        "pr_number": row["pr_number"],
        "last_seen": row["last_seen"],
        "working_on": row["working_on"],
    }


def bucket_tiers(
    candidates: list[dict],
    my_repo: str | None,
    my_topic: str | None,
    my_pr: int | None,
    my_files: list[str],
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Assign each candidate peer to its *strongest* overlap tier (no double
    counting): t1 exact-file, t2 same-PR-or-shared-dir, t3 same-topic, t4
    same-repo. Pure function over already-filtered candidates — unit-testable
    without a DB. Candidates are assumed to already share my repo or my topic
    (the t5 cross-repo set is filtered out by the SQL before we get here)."""
    my_file_set = set(my_files)
    my_parents = _parents(my_files)

    t1: list[dict] = []
    t2: list[dict] = []
    t3: list[dict] = []
    t4: list[dict] = []

    for row in candidates:
        same_repo = my_repo is not None and row["repo"] == my_repo
        same_topic = my_topic is not None and row["topic"] == my_topic
        their_files = list(row["files_touched"] or [])

        # t1 — exact file overlap (only meaningful within the same repo).
        overlap = my_file_set & set(their_files) if same_repo else set()
        if overlap:
            peer = _peer(row)
            peer["overlap_files"] = sorted(overlap)
            t1.append(peer)
            continue

        # t2 — same PR, or shared working directory, within the same repo.
        if same_repo:
            same_pr = my_pr is not None and row["pr_number"] == my_pr
            shared_dirs = my_parents & _parents(their_files)
            if same_pr or shared_dirs:
                peer = _peer(row)
                peer["reason"] = (
                    f"pr:{my_pr}" if same_pr else f"dir:{sorted(shared_dirs)[0]}"
                )
                t2.append(peer)
                continue

        # t3 — same topic (any repo).
        if same_topic:
            t3.append(_peer(row))
            continue

        # t4 — same repo, no stronger signal.
        if same_repo:
            t4.append(_peer(row))

    return t1, t2, t3, t4


def solved_matches(
    candidates: list[dict],
    my_repo: str | None,
    my_topic: str | None,
    my_pr: int | None,
    my_files: list[str],
    limit: int = 5,
) -> list[dict]:
    """Rank already-*done* sessions by the same overlap tiers as live dedup,
    strongest first — the "has this been solved before?" payload. Pure function
    over a pre-filtered candidate set (done, not private, recency-bounded),
    unit-testable without a DB.

    Each entry carries the resolution layer (`resolution`, `done_at`, `person`)
    plus `match_tier` so the client can say "solved by <person> on <date>:
    <resolution>". Reuses `bucket_tiers` so the matching logic never drifts
    from the live path."""
    t1, t2, t3, t4 = bucket_tiers(candidates, my_repo, my_topic, my_pr, my_files)
    by_uid = {row["uid"]: row for row in candidates}

    out: list[dict] = []
    for tier_label, peers in (
        ("t1_file_overlap", t1),
        ("t2_path_or_pr", t2),
        ("t3_topic", t3),
        ("t4_repo", t4),
    ):
        for p in peers:
            row = by_uid[p["uid"]]
            entry = {
                "uid": row["uid"],
                "person": row["user_email"],
                "repo": row["repo"],
                "topic": row["topic"],
                "pr_number": row["pr_number"],
                "done_at": row.get("done_at"),
                "resolution": row.get("resolution"),
                "match_tier": tier_label,
            }
            if "overlap_files" in p:
                entry["overlap_files"] = p["overlap_files"]
            out.append(entry)
            if len(out) >= limit:
                return out
    return out


# Columns the solved-archive tier match needs, selected from v_sessions_all so
# the candidate set spans hot + cold (a done row that the mover relocated to
# sessions_archive must still match). Shared by /v1/list and /v1/classify_self
# so the two solved paths can never drift.
_SOLVED_SELECT = """
    SELECT uid, user_email, machine, repo, branch, topic, status,
           pr_number, files_touched, last_seen, working_on,
           done_at, resolution
    FROM v_sessions_all
    WHERE uid <> %(uid)s
      AND is_private = false
      AND status = 'done'
      AND resolution IS NOT NULL
      AND (
            (%(repo)s::text IS NOT NULL AND repo = %(repo)s)
         OR (%(topic)s::text IS NOT NULL AND topic = %(topic)s)
      )
    ORDER BY done_at DESC
"""


async def fetch_solved_candidates(cur, uid: str, my_repo, my_topic) -> list[dict]:
    """Done, resolution-bearing sessions (ANY age — no time bound by design;
    Joe: don't hide completed work after half a year) sharing my repo or topic,
    newest first. Reads the union view so cold-archived solves are included.
    The caller runs `solved_matches` over these to keep only the strongest few.
    Resolution-less done rows are excluded (no actionable "how it was solved")."""
    await cur.execute(_SOLVED_SELECT, {"uid": uid, "repo": my_repo, "topic": my_topic})
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in await cur.fetchall()]


@router.get("/list")
async def list_peers(
    uid: str,
    repo: str | None = None,
    topic: str | None = None,
    files_touched: str | None = None,
    recency_min: int = 10,
    include_paused: bool = False,
    tier_max: int = 4,
    include_solved: bool = True,
    user_email: str = Depends(current_user),
):
    """Per-turn peer query — the dedup engine. Answers "is someone already on
    this?" by overlap strength, strongest first:

      t1 file_overlap : same repo AND ≥1 exact file in common (the loudest signal)
      t2 path_or_pr   : same repo AND (same PR number OR shared parent directory)
      t3 topic        : same topic (any repo) — count + up to 3 peers
      t4 repo         : same repo — count + up to 1 peer

    Each peer is reported in its *strongest* applicable tier only (no double
    counting). Tier-5 (cross-repo, no shared topic) is never returned — two
    people in unrelated repos aren't colliding. `tier_max` (1-4) caps how deep
    we report; the default 4 returns everything.

    Self (the calling `uid`) is always excluded; private and stale sessions
    (older than `recency_min` minutes) are filtered server-side. Without
    `include_paused`, only `active` peers are considered.

    The `solved` block answers the *other* question — "has this been solved
    *before*?" — by matching `status='done'` sessions (any age — no time bound,
    read from the hot+cold union view so archived solves still count) through
    the same overlap tiers, keeping the strongest few. Live presence and solved
    history are separate signals: presence stops simultaneous duplication,
    solved history stops re-solving a closed problem. Disable with
    `include_solved=false`; browse the full archive via /v1/archive.
    """
    # Fall back to the caller's own stored session for any dimension the client
    # didn't pass explicitly (repo / topic / files / pr_number).
    my_files = [f.strip() for f in (files_touched or "").split(",") if f.strip()]
    statuses = ["active", "paused"] if include_paused else ["active"]

    async with db.conn() as c:
        async with c.cursor() as cur:
            await cur.execute(
                """
                SELECT repo, topic, pr_number, files_touched
                FROM sessions WHERE uid = %(uid)s
                """,
                {"uid": uid},
            )
            me = await cur.fetchone()
            if me is None:
                raise HTTPException(
                    status_code=404,
                    detail="Unknown uid — call /v1/checkin before /v1/list.",
                )
            my_repo = repo if repo is not None else me[0]
            my_topic = topic if topic is not None else me[1]
            my_pr = me[2]
            if not my_files:
                my_files = list(me[3] or [])

            # Candidate set: anything sharing my repo OR my topic. Everything
            # else is t5 (never returned), so we don't even fetch it.
            await cur.execute(
                """
                SELECT uid, user_email, machine, repo, branch, topic, status,
                       pr_number, files_touched, last_seen, working_on
                FROM sessions
                WHERE uid <> %(uid)s
                  AND is_private = false
                  AND status = ANY(%(statuses)s)
                  AND last_seen > now() - make_interval(mins => %(recency)s)
                  AND (
                        (%(repo)s::text IS NOT NULL AND repo = %(repo)s)
                     OR (%(topic)s::text IS NOT NULL AND topic = %(topic)s)
                  )
                ORDER BY last_seen DESC
                """,
                {
                    "uid": uid,
                    "statuses": statuses,
                    "recency": recency_min,
                    "repo": my_repo,
                    "topic": my_topic,
                },
            )
            cols = [d[0] for d in cur.description]
            candidates = [dict(zip(cols, r)) for r in await cur.fetchall()]

            solved: list[dict] = []
            if include_solved:
                solved_candidates = await fetch_solved_candidates(
                    cur, uid, my_repo, my_topic
                )
                solved = solved_matches(
                    solved_candidates, my_repo, my_topic, my_pr, my_files
                )

    t1, t2, t3, t4 = bucket_tiers(candidates, my_repo, my_topic, my_pr, my_files)

    result: dict = {"uid": uid, "tiers": {}}
    tiers = result["tiers"]
    if tier_max >= 1:
        tiers["t1_file_overlap"] = t1
    if tier_max >= 2:
        tiers["t2_path_or_pr"] = t2
    if tier_max >= 3:
        tiers["t3_topic"] = {
            "topic": my_topic,
            "count": len(t3),
            "peers": t3[:3],
        }
    if tier_max >= 4:
        tiers["t4_repo"] = {
            "repo": my_repo,
            "count": len(t4),
            "peers": t4[:1],
        }
    if include_solved:
        result["solved"] = solved
    return result


# Browse cap: clamp client-supplied page size into a sane range so a caller
# can't ask for the whole table in one request (or a non-positive page).
_ARCHIVE_MAX_LIMIT = 200
_ARCHIVE_DEFAULT_LIMIT = 50


@router.get("/archive")
async def archive(
    repo: str | None = None,
    topic: str | None = None,
    person: str | None = None,
    limit: int = _ARCHIVE_DEFAULT_LIMIT,
    offset: int = 0,
    user_email: str = Depends(current_user),
):
    """Browse the full solved-problem archive — every `done`, resolution-bearing
    session ever recorded, newest first, regardless of age (the deliberate
    counterpart to the recency-bounded per-turn nudge). Reads `v_sessions_all`,
    so hot and cold-archived rows are returned uniformly; the physical hot/cold
    split is invisible here.

    Paginated with limit/offset (limit clamped to 1..200). `has_more` is true
    when another page exists — computed by fetching one extra row rather than a
    COUNT(*), so it stays cheap as the archive grows. Optional repo / topic /
    person filters narrow the browse. Private rows are never returned."""
    limit = max(1, min(limit, _ARCHIVE_MAX_LIMIT))
    offset = max(0, offset)

    async with db.conn() as c:
        async with c.cursor() as cur:
            await cur.execute(
                """
                SELECT uid, user_email, machine, repo, branch, topic,
                       pr_number, done_at, resolution, working_on, archived
                FROM v_sessions_all
                WHERE is_private = false
                  AND status = 'done'
                  AND resolution IS NOT NULL
                  AND (%(repo)s::text IS NULL OR repo = %(repo)s)
                  AND (%(topic)s::text IS NULL OR topic = %(topic)s)
                  AND (%(person)s::text IS NULL OR user_email = %(person)s)
                ORDER BY done_at DESC NULLS LAST
                LIMIT %(limit)s OFFSET %(offset)s
                """,
                {
                    "repo": repo,
                    "topic": topic,
                    "person": person,
                    # Fetch one extra to detect a further page without COUNT(*).
                    "limit": limit + 1,
                    "offset": offset,
                },
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in await cur.fetchall()]

    has_more = len(rows) > limit
    items = rows[:limit]
    return {
        "items": items,
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
    }
