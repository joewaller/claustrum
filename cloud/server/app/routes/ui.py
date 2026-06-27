"""Read-only web UI for Claustrum — a browsable counterpart to the terminal
tray + `claustrum` CLI.

Two views, server-rendered (no build step, no JS framework), riding the same
IAP-protected LB as the API so Google SSO is free:

  GET /ui                live board  — active+paused sessions, one flat table
  GET /ui/archive        archive      — paginated browse of solved work
  GET /ui/session/{uid}  detail       — full scrubbed record (hot or cold), the
                                        drill-down for "is this the same problem?"

PRIVACY: never renders `is_private` rows, and only ever shows the value-scrubbed
layer (label / topic / repo / branch / task / working_on / files_touched /
resolution + timestamps) — descriptions of work, never raw secret values.
"""

import html
from datetime import datetime, timezone
from urllib.parse import quote_plus

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
form.filters input, form.filters select { padding: 5px 8px; border: 1px solid var(--line); border-radius: 5px; font: inherit; }
th a.row { color: var(--mut); }
th a.row:hover { color: var(--accent); }
th a.row.sorted { color: var(--fg); }
form.filters button { padding: 5px 12px; border: 1px solid var(--accent); background: var(--accent);
                      color: #fff; border-radius: 5px; cursor: pointer; }
.pager { margin-top: 14px; display: flex; gap: 10px; align-items: center; }
.pager a { color: var(--accent); text-decoration: none; }
.pager .mut { font-size: 12px; }
a.row { color: var(--accent); text-decoration: none; }
a.row:hover { text-decoration: underline; }
dl.detail { display: grid; grid-template-columns: 150px 1fr; gap: 5px 16px;
            background: #fff; border: 1px solid var(--line); border-radius: 6px;
            padding: 16px 18px; max-width: 820px; }
dl.detail dt { color: var(--mut); font-size: 11px; text-transform: uppercase;
               letter-spacing: .04em; padding-top: 2px; }
dl.detail dd { margin: 0; }
.chip { display: inline-block; padding: 1px 6px; margin: 1px 2px; background: #f3f4f6;
        border: 1px solid var(--line); border-radius: 4px; font-size: 11px; }
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


def _fmt_dt(ts: datetime | None) -> str:
    return ts.strftime("%Y-%m-%d %H:%M") if ts is not None else "—"


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


# Whitelisted board sorts: key -> (column header, safe ORDER BY fragment).
# Values are constants (never user input) so they interpolate into SQL safely.
_BOARD_SORTS = {
    "domain":  ("Domain",        "domain NULLS LAST, topic NULLS LAST, repo NULLS LAST, last_seen DESC"),
    "topic":   ("Topic",         "topic NULLS LAST, repo NULLS LAST, last_seen DESC"),
    "session": ("Session",       "label NULLS LAST, user_email, last_seen DESC"),
    "machine": ("Machine",       "machine NULLS LAST, last_seen DESC"),
    "status":  ("Status",        "status, last_seen DESC"),
    "repo":    ("Repo · branch", "repo NULLS LAST, last_seen DESC"),
    "age":     ("Age",           "started_at ASC"),
    "seen":    ("Seen",          "last_seen DESC"),
}
# Header row order; None = a non-sortable column.
_BOARD_HEADERS = ["domain", "topic", "session", "machine", "status", "repo", None, "age", "seen"]


@router.get("/ui", response_class=HTMLResponse)
async def ui_board(
    domain: str | None = None,
    topic: str | None = None,
    repo: str | None = None,
    person: str | None = None,
    status: str | None = None,
    sort: str = "domain",
    viewer: str = Depends(current_user),
) -> HTMLResponse:
    """Live board — non-private sessions. Defaults to live (active, currently
    heartbeating) sessions only; `?status=paused` shows dead/idle sessions and
    `?status=all` shows both. Filterable (domain / topic / repo / person /
    status) and sortable via clickable column headers. Defaults to a
    domain->topic-grouped view (repeated domain and topic cells blanked); any
    other sort shows domain + topic on every row. Domain is derived by joining
    the session's topic to the canonical `topics.domain`."""
    domain = (domain or "").strip() or None
    topic = (topic or "").strip() or None
    repo = (repo or "").strip() or None
    person = (person or "").strip() or None
    status = status if status in ("active", "paused", "all") else None
    if sort not in _BOARD_SORTS:
        sort = "domain"

    where = ["is_private = false"]
    params: dict = {}
    # Default (no status filter) shows only live, heartbeating sessions. The
    # state-transitions job demotes active -> paused after STALE_ACTIVE_MINUTES,
    # so `paused` means "no heartbeat for an hour" — dead or idle work that
    # shouldn't crowd the live board. Reach it with ?status=paused, or
    # ?status=all to see both at once.
    if status == "paused":
        where.append("status = 'paused'")
    elif status == "all":
        where.append("status IN ('active', 'paused')")
    else:
        where.append("status = 'active'")
    if domain:
        where.append("t.domain = %(domain)s")
        params["domain"] = domain
    if topic:
        where.append("s.topic = %(topic)s")
        params["topic"] = topic
    if repo:
        where.append("s.repo = %(repo)s")
        params["repo"] = repo
    if person:
        where.append("s.user_email = %(person)s")
        params["person"] = person

    async with db.conn() as c:
        async with c.cursor() as cur:
            # LEFT JOIN the canonical taxonomy so each session carries the
            # domain of its topic (topics.domain is NOT NULL). Untagged sessions
            # — or free-text topics not in the taxonomy — get domain NULL and
            # fall into the (untagged) group.
            await cur.execute(
                f"""
                SELECT s.uid, s.user_email, s.machine, s.label, s.repo, s.branch,
                       s.topic, s.status, s.working_on, s.last_seen, s.started_at,
                       s.pr_number, t.domain AS domain
                FROM sessions s
                LEFT JOIN topics t ON t.name = s.topic
                WHERE {' AND '.join(where)}
                ORDER BY {_BOARD_SORTS[sort][1]}
                """,
                params,
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in await cur.fetchall()]

    # Build a query string that carries the active filters + sort, overriding
    # individual keys. Default sort is omitted for clean URLs.
    def _qs(**over):
        cur_params = {"domain": domain, "topic": topic, "repo": repo,
                      "person": person, "status": status, "sort": sort}
        cur_params.update(over)
        parts = []
        for k, v in cur_params.items():
            if not v or (k == "sort" and v == "domain"):
                continue
            parts.append(f"{k}={quote_plus(str(v))}")
        return "&amp;".join(parts)

    active_n = sum(1 for r in rows if r["status"] == "active")
    paused_n = len(rows) - active_n
    # `content_filter` drives the "(filtered)" label + empty-state wording — a
    # status view (paused/all) isn't a content filter. `any_filter` (incl.
    # status) drives the clear link so any non-default view can reset to live.
    content_filter = bool(domain or topic or repo or person)
    any_filter = bool(domain or topic or repo or person or status)
    if status == "paused":
        head = f"{paused_n} paused"
    elif status == "all":
        head = f"{active_n} active · {paused_n} paused"
    else:
        head = f"{active_n} live"

    def _inp(name, val):
        return f'<input name="{name}" value="{_esc(val or "")}" placeholder="{name}">'

    body = [
        '<form class="filters" method="get" action="/ui">',
        _inp("domain", domain), _inp("topic", topic), _inp("repo", repo),
        _inp("person", person),
        f'<select name="status"><option value="">live</option>'
        f'<option value="paused"{" selected" if status == "paused" else ""}>paused</option>'
        f'<option value="all"{" selected" if status == "all" else ""}>all</option>'
        f"</select>",
        f'<input type="hidden" name="sort" value="{_esc(sort)}">',
        "<button>Filter</button>",
        ('<a class="row" href="/ui">clear</a>' if any_filter else ""),
        "</form>",
        f'<h2>{head}{" (filtered)" if content_filter else ""}</h2>',
    ]
    if not rows:
        body.append('<p class="empty">No live sessions'
                    f'{" match" if any_filter else ""}.</p>')
    else:
        # Header row — sortable columns link to /ui?sort=<key> (preserving
        # filters); the active column is marked. A non-default sort drops the
        # topic-grouping suppression so rows read correctly out of topic order.
        ths = []
        for key in _BOARD_HEADERS:
            if key is None:
                ths.append("<th>Working on</th>")
                continue
            label = _BOARD_SORTS[key][0]
            on = " sorted" if key == sort else ""
            arrow = " ▾" if key == sort else ""
            ths.append(f'<th><a class="row{on}" href="/ui?{_qs(sort=key)}">{label}{arrow}</a></th>')
        body.append("<table>")
        body.append("<tr>" + "".join(ths) + "</tr>")
        # Domain grouping is active in the default domain sort; topic grouping in
        # both the domain and topic sorts (topics nest under their domain). Any
        # other sort shows domain + topic on every row.
        grouped_domain = sort == "domain"
        grouped_topic = sort in ("domain", "topic")
        prev_domain = object()  # sentinels so the first row always prints both
        prev_topic = object()
        for r in rows:
            st = r["status"]
            pill = f'<span class="pill {st}">{st}</span>'
            domain_v = r["domain"] or "(untagged)"
            topic_v = r["topic"] or "(untagged)"
            if grouped_domain:
                new_domain = domain_v != prev_domain
                domain_cell = f"<strong>{_esc(domain_v)}</strong>" if new_domain else ""
                # Reset topic grouping at each domain boundary so a topic reprints
                # under a new domain heading.
                if new_domain:
                    prev_topic = object()
                prev_domain = domain_v
            else:
                domain_cell = _esc(domain_v)
            if grouped_topic:
                topic_cell = "" if topic_v == prev_topic else f"<strong>{_esc(topic_v)}</strong>"
                prev_topic = topic_v
            else:
                topic_cell = _esc(topic_v)
            who_txt = _esc(r["label"]) if r["label"] else _esc(_short_email(r["user_email"]))
            who = f'<a class="row" href="/ui/session/{_esc(r["uid"])}">{who_txt}</a>'
            # Person (short email) sits under the label; machine now has its own
            # column. With no label the person IS the who, so drop the sub-line.
            meta = _esc(_short_email(r["user_email"])) if r["label"] else ""
            who_sub = f'<br><span class="mut mono">{meta}</span>' if meta else ""
            machine_c = _esc(r["machine"] or "—")
            repo_c = _esc(r["repo"] or "—")
            branch = f' <span class="mut mono">{_esc(r["branch"])}</span>' if r["branch"] else ""
            pr = f' <span class="mut">PR #{_esc(r["pr_number"])}</span>' if r["pr_number"] else ""
            body.append(
                "<tr>"
                f"<td>{domain_cell}</td>"
                f"<td>{topic_cell}</td>"
                f"<td>{who}{who_sub}</td>"
                f'<td class="mut mono">{machine_c}</td>'
                f"<td>{pill}</td>"
                f"<td class=mono>{repo_c}{branch}{pr}</td>"
                f'<td>{_esc(r["working_on"] or "—")}</td>'
                f'<td class=mut>{_esc(_fmt_age(r["started_at"]))}</td>'
                f'<td class=mut>{_esc(_fmt_ago(r["last_seen"]))}</td>'
                "</tr>"
            )
        body.append("</table>")
    # Glanceable: refresh every 15s (preserves the current filter/sort URL).
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
                SELECT uid, user_email, repo, topic, pr_number, done_at,
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
            view = f'<a class="row" href="/ui/session/{_esc(it["uid"])}">view ›</a>'
            body.append(
                "<tr>"
                f'<td>{_esc(_short_email(it["user_email"]))}</td>'
                f'<td class=mut>{_esc(_fmt_date(it["done_at"]))}</td>'
                f'<td>{_esc(it["resolution"])}{pr_s}</td>'
                f"<td class=mono>{where}</td>"
                f"<td>{view}{cold}</td>"
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


@router.get("/ui/session/{uid}", response_class=HTMLResponse)
async def ui_session(uid: str, viewer: str = Depends(current_user)) -> HTMLResponse:
    """Full value-scrubbed record for one session, hot or cold — the drill-down
    used to judge whether an in-flight or already-solved session is the *same
    problem* you're about to start on. `files_touched` is the strongest signal:
    same files = almost certainly the same work. Private rows are never shown."""
    async with db.conn() as c:
        async with c.cursor() as cur:
            await cur.execute(
                """
                SELECT uid, user_email, machine, label, task, working_on, topic,
                       topic_confidence, status, repo, branch, pr_number,
                       files_touched, last_push_at, last_activity_at, last_seen,
                       started_at, resolution, done_at, archived
                FROM v_sessions_all
                WHERE uid = %(uid)s AND is_private = false
                LIMIT 1
                """,
                {"uid": uid},
            )
            cols = [d[0] for d in cur.description]
            raw = await cur.fetchone()

    if raw is None:
        return HTMLResponse(
            _page("Not found", viewer, "",
                  '<p class="empty">No such session — it may be private, '
                  'or the id is wrong.</p>'),
            status_code=404,
        )
    r = dict(zip(cols, raw))

    st = r["status"]
    pill = f'<span class="pill {st}">{_esc(st)}</span>'
    title = _esc(r["label"]) if r["label"] else _esc(_short_email(r["user_email"]))
    archived = ' <span class="cold">·cold</span>' if r["archived"] else ""
    conf = f' · {_esc(r["topic_confidence"])}% conf' if r["topic_confidence"] is not None else ""
    branch = f' <span class="mut mono">{_esc(r["branch"])}</span>' if r["branch"] else ""
    pr = f' · PR #{_esc(r["pr_number"])}' if r["pr_number"] else ""

    files = r["files_touched"] or []
    if files:
        chips = " ".join(f'<span class="chip mono">{_esc(f)}</span>' for f in files)
        files_html = f'{len(files)} — {chips}'
    else:
        files_html = '<span class=mut>none recorded</span>'

    def _dl(k, v):
        return f"<dt>{k}</dt><dd>{v}</dd>"

    body = [
        f"<h2>{title} {pill}{archived}</h2>",
        '<dl class="detail">',
        _dl("Who", f'{_esc(_short_email(r["user_email"]))} '
                   f'<span class="mut mono">{_esc(r["machine"])}</span>'),
        _dl("Topic", f'{_esc(r["topic"] or "(untagged)")}{conf}'),
        _dl("Repo · branch", f'<span class=mono>{_esc(r["repo"] or "—")}{branch}</span>{pr}'),
        _dl("Working on", _esc(r["working_on"] or "—")),
        _dl("Task", _esc(r["task"] or "—")),
        _dl("Files touched", files_html),
        _dl("Started", _esc(_fmt_dt(r["started_at"]))),
        _dl("Last activity", _esc(_fmt_dt(r["last_activity_at"]))),
        _dl("Last push", _esc(_fmt_dt(r["last_push_at"]))),
        _dl("Last seen", _esc(_fmt_dt(r["last_seen"]))),
    ]
    if st == "done" or r["resolution"]:
        body.append(_dl("Resolved", _esc(_fmt_dt(r["done_at"]))))
        body.append(_dl("Resolution", _esc(r["resolution"] or "—")))
    body.append("</dl>")
    body.append('<p class="pager"><a href="/ui">‹ board</a> '
                '<a href="/ui/archive">archive ›</a></p>')

    return HTMLResponse(_page(title, viewer, "", "".join(body)))
