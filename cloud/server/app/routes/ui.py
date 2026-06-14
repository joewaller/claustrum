"""Read-only web UI for Claustrum — a browsable counterpart to the terminal
tray + `claustrum` CLI.

Two views, server-rendered (no build step, no JS framework), riding the same
IAP-protected LB as the API so Google SSO is free:

  GET /ui          live board  — active+paused sessions, grouped by topic→repo
  GET /ui/archive  archive      — paginated browse of solved work (/v1/archive)

PRIVACY: never renders `is_private` rows, and only ever shows the value-scrubbed
layer (topic / repo / branch / working_on / resolution) — never raw detail.
"""

import html
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from app import db
from app.auth import current_user

router = APIRouter()

_ARCHIVE_MAX_LIMIT = 200
_ARCHIVE_DEFAULT_LIMIT = 50

_CSS = """
:root { --fg:#1a1a1a; --mut:#6b7280; --line:#e5e7eb; --bg:#fafafa;
        --accent:#2563eb; --cold:#9ca3af; --active:#16a34a; --paused:#d97706; }
* { box-sizing: border-box; }
body { font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       color: var(--fg); background: var(--bg); margin: 0; padding: 0; }
header { background: #fff; border-bottom: 1px solid var(--line); padding: 12px 20px;
         display: flex; align-items: baseline; gap: 20px; position: sticky; top: 0; }
header h1 { font-size: 16px; margin: 0; }
header nav a { color: var(--accent); text-decoration: none; margin-right: 14px; }
header nav a.on { color: var(--fg); font-weight: 600; }
header .who { margin-left: auto; color: var(--mut); font-size: 12px; }
main { padding: 20px; max-width: 1100px; margin: 0 auto; }
h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .04em;
     color: var(--mut); margin: 24px 0 8px; }
table { width: 100%; border-collapse: collapse; background: #fff;
        border: 1px solid var(--line); border-radius: 6px; overflow: hidden; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--line);
         vertical-align: top; }
th { font-size: 11px; text-transform: uppercase; letter-spacing: .04em;
     color: var(--mut); background: #fcfcfd; }
tr:last-child td { border-bottom: 0; }
.mut { color: var(--mut); }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.pill { font-size: 11px; padding: 1px 7px; border-radius: 10px; border: 1px solid var(--line); }
.pill.active { color: var(--active); border-color: var(--active); }
.pill.paused { color: var(--paused); border-color: var(--paused); }
.cold { color: var(--cold); font-size: 11px; }
.empty { color: var(--mut); padding: 16px 0; }
form.filters { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 14px; }
form.filters input { padding: 5px 8px; border: 1px solid var(--line); border-radius: 5px; font: inherit; }
form.filters button { padding: 5px 12px; border: 1px solid var(--accent); background: var(--accent);
                      color: #fff; border-radius: 5px; cursor: pointer; }
.pager { margin-top: 14px; display: flex; gap: 10px; align-items: center; }
.pager a { color: var(--accent); text-decoration: none; }
.pager .mut { font-size: 12px; }
"""


def _esc(v) -> str:
    return html.escape(str(v)) if v is not None else ""


def _fmt_ago(ts: datetime | None) -> str:
    if ts is None:
        return "—"
    now = datetime.now(timezone.utc)
    secs = (now - ts).total_seconds()
    if secs < 0:
        secs = 0
    if secs < 90:
        return f"{int(secs)}s ago"
    if secs < 5400:
        return f"{int(secs // 60)}m ago"
    if secs < 172800:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _fmt_date(ts: datetime | None) -> str:
    return ts.strftime("%Y-%m-%d") if ts is not None else "—"


def _fmt_age(ts: datetime | None) -> str:
    """Compact elapsed-since-start, no trailing 'ago' (for an Age column)."""
    return _fmt_ago(ts).removesuffix(" ago")


def _short_email(email) -> str:
    s = str(email or "")
    return s.split("@", 1)[0] if "@" in s else s


def _page(title: str, viewer: str, active_tab: str, body: str) -> str:
    def cls(tab):
        return ' class="on"' if tab == active_tab else ""
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        f"<title>{_esc(title)} · Claustrum</title>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        f"<style>{_CSS}</style></head><body>"
        "<header><h1>🗂 Claustrum</h1><nav>"
        f'<a href="/ui"{cls("board")}>Board</a>'
        f'<a href="/ui/archive"{cls("archive")}>Archive</a>'
        f'</nav><span class=who>{_esc(viewer)}</span></header>'
        f"<main>{body}</main></body></html>"
    )


@router.get("/ui", response_class=HTMLResponse)
async def ui_board(viewer: str = Depends(current_user)) -> HTMLResponse:
    """Live board — active+paused non-private sessions, grouped topic→repo."""
    async with db.conn() as c:
        async with c.cursor() as cur:
            await cur.execute(
                """
                SELECT user_email, machine, label, repo, branch, topic, status,
                       working_on, last_seen, started_at, pr_number
                FROM sessions
                WHERE is_private = false AND status IN ('active', 'paused')
                ORDER BY topic NULLS LAST, repo NULLS LAST, last_seen DESC
                """
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in await cur.fetchall()]

    active_n = sum(1 for r in rows if r["status"] == "active")
    body = [
        f'<h2>{active_n} active · {len(rows) - active_n} paused</h2>'
    ]
    if not rows:
        body.append('<p class="empty">No live sessions.</p>')
    else:
        # One flat table, grouped by topic via the SQL ordering. Repeated topic
        # cells are blanked so it reads grouped without breaking into tables.
        body.append("<table>")
        body.append(
            "<tr><th>Topic</th><th>Session</th><th>Status</th>"
            "<th>Repo · branch</th><th>Working on</th><th>Age</th><th>Seen</th></tr>"
        )
        prev_topic = object()  # sentinel so the first row always prints its topic
        for r in rows:
            st = r["status"]
            pill = f'<span class="pill {st}">{st}</span>'
            topic = r["topic"] or "(untagged)"
            topic_cell = "" if topic == prev_topic else f'<strong>{_esc(topic)}</strong>'
            prev_topic = topic
            who = _esc(r["label"]) if r["label"] else _esc(_short_email(r["user_email"]))
            meta = f'{_esc(_short_email(r["user_email"]))} · {_esc(r["machine"])}' if r["label"] \
                else _esc(r["machine"])
            repo = _esc(r["repo"] or "—")
            branch = f' <span class="mut mono">{_esc(r["branch"])}</span>' if r["branch"] else ""
            pr = f' <span class="mut">PR #{_esc(r["pr_number"])}</span>' if r["pr_number"] else ""
            body.append(
                "<tr>"
                f"<td>{topic_cell}</td>"
                f'<td>{who}<br><span class="mut mono">{meta}</span></td>'
                f"<td>{pill}</td>"
                f"<td class=mono>{repo}{branch}{pr}</td>"
                f'<td>{_esc(r["working_on"] or "—")}</td>'
                f'<td class=mut>{_esc(_fmt_age(r["started_at"]))}</td>'
                f'<td class=mut>{_esc(_fmt_ago(r["last_seen"]))}</td>'
                "</tr>"
            )
        body.append("</table>")
    # Glanceable: refresh every 15s.
    page = _page("Board", viewer, "board", "".join(body)).replace(
        "<head>", '<head><meta http-equiv="refresh" content="15">', 1
    )
    return HTMLResponse(page)


@router.get("/ui/archive", response_class=HTMLResponse)
async def ui_archive(
    repo: str | None = None,
    topic: str | None = None,
    person: str | None = None,
    limit: int = _ARCHIVE_DEFAULT_LIMIT,
    offset: int = 0,
    viewer: str = Depends(current_user),
) -> HTMLResponse:
    """Archive browser — paginated solved work, any age, hot+cold. Same data
    layer as GET /v1/archive."""
    limit = max(1, min(limit, _ARCHIVE_MAX_LIMIT))
    offset = max(0, offset)
    repo = (repo or "").strip() or None
    topic = (topic or "").strip() or None
    person = (person or "").strip() or None

    async with db.conn() as c:
        async with c.cursor() as cur:
            await cur.execute(
                """
                SELECT user_email, repo, topic, pr_number, done_at,
                       resolution, archived
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
                    "repo": repo, "topic": topic, "person": person,
                    "limit": limit + 1, "offset": offset,
                },
            )
            cols = [d[0] for d in cur.description]
            fetched = [dict(zip(cols, r)) for r in await cur.fetchall()]

    has_more = len(fetched) > limit
    items = fetched[:limit]

    def _filt_input(name, val):
        return f'<input name="{name}" value="{_esc(val or "")}" placeholder="{name}">'

    body = [
        '<form class="filters" method="get" action="/ui/archive">',
        _filt_input("repo", repo), _filt_input("topic", topic),
        _filt_input("person", person),
        f'<input type="hidden" name="limit" value="{limit}">',
        "<button>Filter</button>",
        "</form>",
    ]
    if not items:
        body.append('<p class="empty">No solved work matches.</p>')
    else:
        body.append("<table>")
        body.append(
            "<tr><th>Who</th><th>When</th><th>Resolution</th>"
            "<th>Where</th><th></th></tr>"
        )
        for it in items:
            pr = it["pr_number"]
            pr_s = f' <span class="mut">PR #{_esc(pr)}</span>' if pr else ""
            where = _esc(it["repo"] or it["topic"] or "?")
            cold = ' <span class="cold">·cold</span>' if it["archived"] else ""
            body.append(
                "<tr>"
                f'<td>{_esc(_short_email(it["user_email"]))}</td>'
                f'<td class=mut>{_esc(_fmt_date(it["done_at"]))}</td>'
                f'<td>{_esc(it["resolution"])}{pr_s}</td>'
                f"<td class=mono>{where}</td>"
                f"<td>{cold}</td>"
                "</tr>"
            )
        body.append("</table>")

    # Pager — preserve filters across pages.
    def _page_link(new_offset):
        qs = [f"limit={limit}", f"offset={new_offset}"]
        if repo:
            qs.append(f"repo={_esc(repo)}")
        if topic:
            qs.append(f"topic={_esc(topic)}")
        if person:
            qs.append(f"person={_esc(person)}")
        return "/ui/archive?" + "&amp;".join(qs)

    pager = ['<div class="pager">']
    if offset > 0:
        pager.append(f'<a href="{_page_link(max(0, offset - limit))}">‹ prev</a>')
    pager.append(
        f'<span class="mut">showing {len(items)} from offset {offset}</span>'
    )
    if has_more:
        pager.append(f'<a href="{_page_link(offset + limit)}">next ›</a>')
    pager.append("</div>")
    body.append("".join(pager))

    return HTMLResponse(_page("Archive", viewer, "archive", "".join(body)))
