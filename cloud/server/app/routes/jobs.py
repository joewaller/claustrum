from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException
from psycopg.types.json import Json

from app import db
from app.archive import move_to_archive
from app.routes.propose import PROMOTION_THRESHOLD

router = APIRouter()


# Note: job endpoints are intentionally NOT protected by current_user.
#
# Auth model (decided Phase 3): we rely on Cloud Run ingress + IAP + IAM, not
# on app-side OIDC validation. The whole service runs with
# ingress=INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER behind an IAP-protected LB, so
# the *only* path to these routes is LB → IAP (authenticates) → Cloud Run.
# Cloud Scheduler reaches them by sending an OIDC token whose audience is the
# IAP OAuth client id; the scheduler service account is granted
# roles/iap.httpsResourceAccessor (see cloud/terraform/main.tf). IAP injects the
# scheduler SA's email in the auth header, which these routes simply ignore —
# they take no caller-supplied identity, so there is nothing to spoof. An
# unauthenticated request never gets past IAP, so /jobs/* are unreachable
# without a valid IAP token. Any IAP-authorised principal (the team, the
# scheduler SA) may also trigger them by hand — that is intentional, it is how
# the jobs are smoke-tested, and they are all idempotent.

# Window thresholds (module constants so tests + the routes agree).
STALE_PROPOSAL_DAYS = 7           # reject open proposals older than this, below threshold
STALE_ACTIVE_MINUTES = 60         # active -> paused after this long with no heartbeat
CONCENTRATION_THRESHOLD = 3       # >= this many active sessions on one topic -> alert
CONCENTRATION_REALERT_MINUTES = 60  # don't re-alert the same topic within this window
DONE_ARCHIVE_DAYS = 180           # done rows older than this -> cold archive table
PAUSED_ARCHIVE_DAYS = 30          # long-stale paused rows -> cold archive (status kept)


# ---------------------------------------------------------------------------
# Pure decision logic — DB-free, unit-tested in tests/test_jobs.py (mirrors
# bucket_tiers() in list_peers.py). The routes do the SQL I/O around these.
# ---------------------------------------------------------------------------

def classify_proposals(
    groups: list[dict],
    now: datetime,
    *,
    threshold: int = PROMOTION_THRESHOLD,
    stale_days: int = STALE_PROPOSAL_DAYS,
) -> dict:
    """Decide what to do with each open-proposal group.

    Each group summarises all open (resolved_at IS NULL) proposals for one
    proposed_name:
      {name, distinct_users, oldest_created_at, already_official, description}

    Returns three buckets keyed by action:
      promote               -> [{name, description, distinct_users}]  (insert topic)
      resolve_already_official -> [name, ...]  (a topic with this name already exists)
      reject_stale          -> [name, ...]     (too old, still below threshold)

    Names not in any bucket stay open (still accumulating distinct users).
    Precedence: an already-official name is never re-promoted (idempotent);
    otherwise threshold beats staleness (an old proposal that has reached the
    threshold is still promoted)."""
    promote: list[dict] = []
    resolve_already_official: list[str] = []
    reject_stale: list[str] = []
    cutoff = now - timedelta(days=stale_days)

    for g in groups:
        name = g["name"]
        if g["already_official"]:
            resolve_already_official.append(name)
        elif g["distinct_users"] >= threshold:
            promote.append(
                {
                    "name": name,
                    "description": g["description"] or "",
                    "distinct_users": g["distinct_users"],
                }
            )
        elif g["oldest_created_at"] is not None and g["oldest_created_at"] < cutoff:
            reject_stale.append(name)

    return {
        "promote": promote,
        "resolve_already_official": resolve_already_official,
        "reject_stale": reject_stale,
    }


def is_session_stale(
    last_seen: datetime | None,
    now: datetime,
    threshold_minutes: int = STALE_ACTIVE_MINUTES,
) -> bool:
    """True when an active session's heartbeat (last_seen, bumped every checkin
    = every turn) is older than the threshold — i.e. the session is dead and
    should drop to 'paused'. A missing last_seen counts as stale."""
    if last_seen is None:
        return True
    return last_seen < now - timedelta(minutes=threshold_minutes)


def is_past_retention(
    ts: datetime | None,
    now: datetime,
    days: int,
) -> bool:
    """True when a timestamp is older than `days` ago — the cold-archive cutoff
    used by /jobs/archive-cold (done rows by done_at, long-paused rows by
    last_seen). A missing timestamp is NOT past retention: we can't safely age
    out a row we can't date, so it stays hot. Mirrors is_session_stale's
    boundary semantics (strict <)."""
    if ts is None:
        return False
    return ts < now - timedelta(days=days)


def concentrated_topics(
    rows: list[dict],
    threshold: int = CONCENTRATION_THRESHOLD,
) -> list[dict]:
    """Keep only the topic groups at/above the concentration threshold. Each
    row: {topic, count, uids}. Pure filter so the threshold is unit-testable."""
    return [r for r in rows if r["count"] >= threshold]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/validate-proposals")
async def validate_proposals():
    """Hourly. Promote a proposed name into the official `topics` taxonomy once
    >= PROMOTION_THRESHOLD (2) distinct user_emails have an open proposal for
    it; resolve those proposals as 'promoted'. Resolve proposals for a name that
    is already official as 'already_official' (idempotent — never re-insert).
    Reject open proposals older than 7 days that are still below threshold.
    """
    async with db.conn() as c:
        async with c.cursor() as cur:
            await cur.execute("SELECT now()")
            now = (await cur.fetchone())[0]

            # One row per open-proposal name: distinct proposers, oldest open
            # proposal, whether a topic with this name already exists, and a
            # representative description (earliest proposal's) for promotion.
            # LEFT JOIN topics is 1:1 (topics.name is PK) so it can't inflate
            # the aggregates.
            await cur.execute(
                """
                SELECT tp.proposed_name                                   AS name,
                       count(DISTINCT tp.user_email)                      AS distinct_users,
                       min(tp.created_at)                                 AS oldest_created_at,
                       bool_or(t.name IS NOT NULL)                        AS already_official,
                       (array_agg(tp.description ORDER BY tp.created_at))[1] AS description
                FROM topic_proposals tp
                LEFT JOIN topics t ON t.name = tp.proposed_name
                WHERE tp.resolved_at IS NULL
                GROUP BY tp.proposed_name
                """
            )
            cols = [d[0] for d in cur.description]
            groups = [dict(zip(cols, r)) for r in await cur.fetchall()]

            decision = classify_proposals(groups, now)

            # Promote: insert the topic (idempotent via ON CONFLICT) then resolve
            # every open proposal for that name.
            for p in decision["promote"]:
                await cur.execute(
                    """
                    INSERT INTO topics (name, description, source, proposal_count, promoted_at)
                    VALUES (%(name)s, %(description)s, 'proposed', %(count)s, now())
                    ON CONFLICT (name) DO NOTHING
                    """,
                    {
                        "name": p["name"],
                        "description": p["description"],
                        "count": p["distinct_users"],
                    },
                )
                await cur.execute(
                    """
                    UPDATE topic_proposals
                    SET resolved_at = now(), resolution = 'promoted'
                    WHERE proposed_name = %(name)s AND resolved_at IS NULL
                    """,
                    {"name": p["name"]},
                )

            # Resolve open proposals for names that are already official.
            if decision["resolve_already_official"]:
                await cur.execute(
                    """
                    UPDATE topic_proposals
                    SET resolved_at = now(), resolution = 'already_official'
                    WHERE proposed_name = ANY(%(names)s) AND resolved_at IS NULL
                    """,
                    {"names": decision["resolve_already_official"]},
                )

            # Reject stale, below-threshold proposals.
            if decision["reject_stale"]:
                await cur.execute(
                    """
                    UPDATE topic_proposals
                    SET resolved_at = now(), resolution = 'rejected_stale'
                    WHERE proposed_name = ANY(%(names)s) AND resolved_at IS NULL
                    """,
                    {"names": decision["reject_stale"]},
                )

        await c.commit()

    return {
        "ok": True,
        "examined": len(groups),
        "promoted": [p["name"] for p in decision["promote"]],
        "already_official": decision["resolve_already_official"],
        "rejected_stale": decision["reject_stale"],
        "promotion_threshold": PROMOTION_THRESHOLD,
    }


@router.post("/state-transitions")
async def state_transitions():
    """Every 5 minutes. Drop active sessions to 'paused' once their heartbeat
    (last_seen, bumped every checkin) is older than STALE_ACTIVE_MINUTES, so the
    board stops showing dead sessions as active. Expire (delete) soft file
    claims whose TTL has passed."""
    async with db.conn() as c:
        async with c.cursor() as cur:
            await cur.execute(
                """
                UPDATE sessions
                SET status = 'paused', updated_at = now()
                WHERE status = 'active'
                  AND last_seen < now() - make_interval(mins => %(mins)s)
                """,
                {"mins": STALE_ACTIVE_MINUTES},
            )
            paused = cur.rowcount

            await cur.execute("DELETE FROM claims WHERE expires_at < now()")
            expired_claims = cur.rowcount

        await c.commit()

    return {
        "ok": True,
        "paused": paused,
        "expired_claims": expired_claims,
        "stale_active_minutes": STALE_ACTIVE_MINUTES,
    }


@router.post("/topic-concentration")
async def topic_concentration():
    """Hourly. Detect topics with >= CONCENTRATION_THRESHOLD (3) active,
    non-private sessions and emit a 'topic-alert' broadcast message (to_topic)
    so those sessions learn they may be duplicating work. Deduped: skip a topic
    already alerted within CONCENTRATION_REALERT_MINUTES."""
    async with db.conn() as c:
        async with c.cursor() as cur:
            await cur.execute(
                """
                SELECT topic,
                       count(*)                              AS count,
                       array_agg(uid ORDER BY last_seen DESC) AS uids
                FROM sessions
                WHERE status = 'active' AND is_private = false AND topic IS NOT NULL
                GROUP BY topic
                """
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in await cur.fetchall()]

            alerted: list[str] = []
            skipped_recent: list[str] = []
            for r in concentrated_topics(rows):
                topic, count, uids = r["topic"], r["count"], list(r["uids"] or [])
                body = (
                    f"{count} active sessions are on topic '{topic}'. "
                    "Check /v1/list before duplicating work."
                )
                # Insert only if we haven't alerted this topic recently — keeps
                # the hourly job from spamming a persistently-busy topic, and
                # makes re-runs within the window idempotent.
                await cur.execute(
                    """
                    INSERT INTO messages (from_uid, to_topic, type, body, metadata)
                    SELECT NULL, %(topic)s, 'topic-alert', %(body)s, %(metadata)s::jsonb
                    WHERE NOT EXISTS (
                        SELECT 1 FROM messages
                        WHERE to_topic = %(topic)s
                          AND type = 'topic-alert'
                          AND created_at > now() - make_interval(mins => %(window)s)
                    )
                    """,
                    {
                        "topic": topic,
                        "body": body,
                        "metadata": Json(
                            {"topic": topic, "session_count": count, "uids": uids}
                        ),
                        "window": CONCENTRATION_REALERT_MINUTES,
                    },
                )
                if cur.rowcount:
                    alerted.append(topic)
                else:
                    skipped_recent.append(topic)

        await c.commit()

    return {
        "ok": True,
        "alerted": alerted,
        "skipped_recent": skipped_recent,
        "concentration_threshold": CONCENTRATION_THRESHOLD,
    }


# ---------------------------------------------------------------------------
# Still-stubbed jobs (later phases).
# ---------------------------------------------------------------------------

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


@router.post("/archive-cold")
async def archive_cold():
    """Daily. Bound the hot `sessions` table by moving cold rows to
    `sessions_archive` — same database, kept forever, never deleted, no
    BigQuery. The rows stay readable via /v1/archive and the solved-archive
    nudge (both read `v_sessions_all`, which unions hot + cold), so moving a
    row never hides it.

    Two categories, copy-not-delete:
      • `done` rows older than DONE_ARCHIVE_DAYS (180d) — long-settled solves.
      • `paused` rows whose heartbeat is older than PAUSED_ARCHIVE_DAYS (30d) —
        abandoned sessions. Status is kept `paused` (we deliberately do NOT
        auto-close them to `done`); they just leave the hot board.

    Never touches `active` rows. Idempotent: moved rows are gone from
    `sessions`, so a re-run only moves newly-qualifying rows; re-archival of a
    resurrected uid upserts the cold copy."""
    async with db.conn() as c:
        async with c.cursor() as cur:
            done_moved = await move_to_archive(
                cur,
                "status = 'done' "
                "AND done_at < now() - make_interval(days => %(done_days)s)",
                {"done_days": DONE_ARCHIVE_DAYS},
            )
            paused_moved = await move_to_archive(
                cur,
                "status = 'paused' "
                "AND last_seen < now() - make_interval(days => %(paused_days)s)",
                {"paused_days": PAUSED_ARCHIVE_DAYS},
            )
        await c.commit()

    return {
        "ok": True,
        "done_archived": done_moved,
        "paused_archived": paused_moved,
        "done_archive_days": DONE_ARCHIVE_DAYS,
        "paused_archive_days": PAUSED_ARCHIVE_DAYS,
    }
