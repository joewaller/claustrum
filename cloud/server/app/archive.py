"""Shared cold-archive helpers — the hot/cold split between `sessions` and
`sessions_archive` (migration 0003).

Used by the /jobs/archive-cold mover (moves cold rows out) and by /v1/checkin
(resurrection — moves a row back in when a paused-and-archived session resumes),
so the column list and the copy mechanics live in exactly one place and can't
drift between the two directions.
"""

# Columns shared by `sessions` and `sessions_archive`, in `sessions` order
# (0001 + 0002). The archive table appends archived_at; everything else is
# copied verbatim. The UNION in v_sessions_all and the SELECTs below all assume
# this order.
SESSION_COLS = (
    "uid, user_email, machine, label, task, working_on, topic, topic_confidence, "
    "status, repo, branch, pr_number, files_touched, last_push_at, last_activity_at, "
    "last_seen, started_at, cwd, is_quiet, is_private, created_at, updated_at, "
    "resolution, done_at"
)

# On re-archival of a uid already cold (archived -> resurrected -> re-archived),
# refresh every column so the cold copy reflects the latest hot state.
_ARCHIVE_UPSERT = ", ".join(
    f"{c} = EXCLUDED.{c}" for c in SESSION_COLS.replace(" ", "").split(",")
) + ", archived_at = now()"


async def move_to_archive(cur, where_sql: str, params: dict) -> int:
    """Atomically copy-then-delete rows matching `where_sql` from `sessions`
    into `sessions_archive`. The DELETE ... RETURNING feeds the INSERT in one
    statement, so the move commits or rolls back as a unit — a row is never in
    neither table (copy-not-delete). Returns rows moved."""
    await cur.execute(
        f"""
        WITH moved AS (
            DELETE FROM sessions
            WHERE {where_sql}
            RETURNING {SESSION_COLS}
        )
        INSERT INTO sessions_archive ({SESSION_COLS}, archived_at)
        SELECT {SESSION_COLS}, now() FROM moved
        ON CONFLICT (uid) DO UPDATE SET {_ARCHIVE_UPSERT}
        """,
        params,
    )
    return cur.rowcount


async def resurrect_from_archive(cur, uid: str) -> None:
    """If `uid` is cold, move it back into `sessions` verbatim so a resuming
    session keeps its accumulated topic / files_touched / task / resolution.
    The caller's checkin upsert then flips it back to `active`. A no-op (single
    PK lookup) when the uid was never archived. Must run in the same
    transaction as the checkin upsert that follows it.

    ON CONFLICT DO NOTHING guards the (should-not-happen) case where the uid is
    already hot — we never clobber a live row with a stale cold copy; the cold
    copy is still removed so the union view can't show the uid twice."""
    await cur.execute(
        f"""
        WITH back AS (
            DELETE FROM sessions_archive WHERE uid = %(uid)s
            RETURNING {SESSION_COLS}
        )
        INSERT INTO sessions ({SESSION_COLS})
        SELECT {SESSION_COLS} FROM back
        ON CONFLICT (uid) DO NOTHING
        """,
        {"uid": uid},
    )
