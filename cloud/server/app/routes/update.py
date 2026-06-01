from fastapi import APIRouter, Depends, HTTPException
from psycopg.types.json import Json

from app import db
from app.auth import current_user
from app.models import OkResponse, UpdateRequest

router = APIRouter()


@router.post("/update", response_model=OkResponse)
async def update(req: UpdateRequest, user_email: str = Depends(current_user)) -> OkResponse:
    """Write the detail layer for a session: task, working_on, files_touched,
    pr_number, last_push_at, status. All fields optional — only the ones
    supplied are written (COALESCE merge). files_touched is *unioned* into the
    existing set, never replaced, so the client can feed incremental
    PostToolUse paths without losing earlier ones. An update is an
    activity-bearing event, so it bumps last_activity_at + last_seen (this is
    what keeps the session 'active' for the state-transitions job).

    Scoped to the authenticated user — you can only update your own sessions.
    """
    # Wrap the new files as jsonb only when supplied; a bare None becomes a
    # SQL NULL so the CASE below leaves the existing array untouched.
    files_param = Json(req.files_touched) if req.files_touched is not None else None

    async with db.conn() as c:
        async with c.cursor() as cur:
            await cur.execute(
                """
                UPDATE sessions SET
                    task         = COALESCE(%(task)s::text,             task),
                    working_on   = COALESCE(%(working_on)s::text,       working_on),
                    pr_number    = COALESCE(%(pr_number)s::int,         pr_number),
                    last_push_at = COALESCE(%(last_push_at)s::timestamptz, last_push_at),
                    status       = COALESCE(%(status)s::text,           status),
                    files_touched = CASE
                        WHEN %(files)s::jsonb IS NULL THEN files_touched
                        ELSE (
                            SELECT COALESCE(jsonb_agg(DISTINCT e ORDER BY e), '[]'::jsonb)
                            FROM jsonb_array_elements(files_touched || %(files)s::jsonb) AS e
                        )
                    END,
                    last_activity_at = now(),
                    last_seen        = now(),
                    updated_at       = now()
                WHERE uid = %(uid)s AND user_email = %(user_email)s
                """,
                {
                    "task": req.task,
                    "working_on": req.working_on,
                    "pr_number": req.pr_number,
                    "last_push_at": req.last_push_at,
                    "status": req.status,
                    "files": files_param,
                    "uid": req.uid,
                    "user_email": user_email,
                },
            )
            if cur.rowcount == 0:
                raise HTTPException(
                    status_code=404,
                    detail="No session with that uid for the authenticated user.",
                )
        await c.commit()

    return OkResponse()
