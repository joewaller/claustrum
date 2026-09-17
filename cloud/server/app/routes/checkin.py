from fastapi import APIRouter, Depends

from app import db
from app.archive import resurrect_from_archive
from app.auth import current_user
from app.models import CheckinRequest, CheckinResponse, TaxonomyEntry

router = APIRouter()

_FIRST_TURN_MESSAGE = (
    "Untagged session. On your next response, call "
    "claustrum_classify_self(uid, topic) with the best-fitting topic from the "
    "taxonomy above. If none fit, claustrum_propose_topic(uid, name, description)."
)


@router.post("/checkin", response_model=CheckinResponse)
async def checkin(req: CheckinRequest, user_email: str = Depends(current_user)) -> CheckinResponse:
    if req.is_private:
        # Documented opt-out: client should not call /checkin at all when private.
        # Returning ok=true with topic_required=false keeps the surface stable
        # for clients that still call it defensively.
        return CheckinResponse(topic_required=False)

    async with db.conn() as c:
        async with c.cursor() as cur:
            # Resume case: if this uid was archived (a long-paused session that
            # got cold-stored, now resuming via `claude --resume`), pull it back
            # into `sessions` first so the upsert below refreshes the real row
            # (keeping its topic/files/task) instead of creating a bare new one
            # and leaving a duplicate in the archive. No-op for fresh sessions.
            # Never resurrect placeholder tmux sessions — they are synthetic and
            # must not inherit months-old state from recycled pane numbers.
            if not req.uid.startswith("tmux-"):
                await resurrect_from_archive(cur, req.uid)

            await cur.execute(
                """
                INSERT INTO sessions (
                    uid, user_email, machine, label, task, repo, branch, cwd,
                    is_quiet, is_private, status, last_seen, started_at,
                    last_activity_at
                )
                VALUES (
                    %(uid)s, %(user_email)s, %(machine)s, %(label)s, %(task)s,
                    %(repo)s, %(branch)s, %(cwd)s, %(is_quiet)s, %(is_private)s,
                    'active', now(), COALESCE(%(started_at)s, now()),
                    CASE WHEN %(repo)s::text IS NOT NULL THEN now() ELSE NULL END
                )
                ON CONFLICT (uid) DO UPDATE SET
                    user_email = EXCLUDED.user_email,
                    machine    = EXCLUDED.machine,
                    label      = COALESCE(EXCLUDED.label,    sessions.label),
                    task       = COALESCE(EXCLUDED.task,     sessions.task),
                    repo       = COALESCE(EXCLUDED.repo,     sessions.repo),
                    branch     = COALESCE(EXCLUDED.branch,   sessions.branch),
                    cwd        = COALESCE(EXCLUDED.cwd,      sessions.cwd),
                    is_quiet   = EXCLUDED.is_quiet,
                    status     = CASE
                        -- If a session was paused due to inactivity (>24h silence), routine background
                        -- heartbeats (without a new task) keep it paused rather than waking it.
                        WHEN sessions.status = 'paused'
                         AND COALESCE(sessions.last_activity_at, sessions.started_at) < now() - make_interval(hours => 24)
                         AND EXCLUDED.task IS NULL
                        THEN 'paused'
                        ELSE 'active'
                    END,
                    last_seen  = now(),
                    started_at = CASE
                        -- Reset started_at on placeholder reuse or when resurrected from pause
                        WHEN EXCLUDED.uid LIKE 'tmux-%%' AND (sessions.status = 'paused' OR EXCLUDED.started_at IS NOT NULL)
                        THEN COALESCE(EXCLUDED.started_at, now())
                        ELSE sessions.started_at
                    END,
                    updated_at = now()
                RETURNING topic, topic_confidence, domain
                """,
                {
                    "uid": req.uid,
                    "user_email": user_email,
                    "machine": req.machine,
                    "label": req.label,
                    "task": req.task,
                    "repo": req.repo,
                    "branch": req.branch,
                    "cwd": req.cwd,
                    "is_quiet": req.is_quiet,
                    "is_private": req.is_private,
                    "started_at": req.started_at,
                },
            )
            row = await cur.fetchone()
            current_topic = row[0] if row else None
            current_confidence = row[1] if row else None
            current_domain = row[2] if row else None

            if current_topic is None:
                # Full taxonomy (name + domain) so the client's one-step classify
                # directive can show every option grouped by domain. LIMIT is a
                # generous safety cap, not a real bound (taxonomy is ~70 topics).
                await cur.execute(
                    "SELECT name, description, domain FROM topics ORDER BY name LIMIT 200"
                )
                taxonomy = [
                    TaxonomyEntry(name=r[0], description=r[1], domain=r[2])
                    for r in await cur.fetchall()
                ]
                await c.commit()
                return CheckinResponse(
                    topic_required=True,
                    taxonomy=taxonomy,
                    first_turn_message=_FIRST_TURN_MESSAGE,
                )

        await c.commit()

    # Already tagged — echo the topic (+ domain) back so the client mirrors it
    # locally (offline-joinable by uid).
    return CheckinResponse(
        topic_required=False,
        topic=current_topic,
        topic_confidence=current_confidence,
        domain=current_domain,
    )
