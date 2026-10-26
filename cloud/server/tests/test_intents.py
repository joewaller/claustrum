"""Intents — declared planned work.

Two layers, like the rest of the server suite:
  * Pure (always run): path normalisation, shared-directory matching, intent
    ranking against a session, and the privacy filter every returned list
    passes through.
  * HTTP + Postgres (run only when CLAUSTRUM_TEST_DB_URL points at a throwaway
    database): create / dedupe / update / close, `/v1/intents` privacy, and the
    `intents` block on `/v1/list`. Migrations are applied by the fixture. Rows
    are namespaced per run, never truncated.
"""
import os
import uuid

import pytest

from app.routes.intents import (
    match_intents,
    normalize_path,
    normalize_paths,
    path_area,
    shared_area,
    visible_intents,
)

REPO = "workspace-automation"


def _intent(i, repo=REPO, paths=None, topic=None, email=None, private=False, sess_private=False):
    return {
        "id": i,
        "user_email": email or f"u{i}@finder.com",
        "uid": None,
        "repo": repo,
        "title": f"intent {i}",
        "paths": paths or [],
        "topic": topic,
        "domain": None,
        "status": "open",
        "source": "manual",
        "pr_number": None,
        "is_private": private,
        "session_private": sess_private,
        "created_at": None,
        "expires_at": None,
    }


# --- normalisation -----------------------------------------------------------

def test_normalize_path_strips_globs_and_slashes():
    assert normalize_path("artefacts/") == "artefacts"
    assert normalize_path("./mcp-proxies/artefacts/**") == "mcp-proxies/artefacts"
    assert normalize_path("src/**/*.py") == "src"
    assert normalize_path("src/rou*") == "src"
    assert normalize_path("/slack-bot/lib") == "slack-bot/lib"


def test_normalize_path_rejects_root_and_traversal():
    for bad in ("", "  ", "/", ".", "*", "**/*.py", "../etc", "a/../b", None):
        assert normalize_path(bad) is None


def test_normalize_paths_dedupes_and_caps():
    assert normalize_paths(["a/", "a", "./a/**", "b"]) == ["a", "b"]
    assert len(normalize_paths([f"d{i}" for i in range(100)])) == 25


def test_path_area_file_vs_dir():
    assert path_area("artefacts/publish.py") == "artefacts"
    assert path_area("artefacts") == "artefacts"
    assert path_area("README.md") == ""
    assert path_area(".github") == ".github"


def test_shared_area_equal_ancestor_and_descendant():
    assert shared_area(["artefacts"], ["artefacts/publish.py"]) == "artefacts"
    # their declared dir is an ancestor of the file I'm editing
    assert shared_area(["mcp-proxies"], ["mcp-proxies/artefacts/x.mjs"]) == "mcp-proxies"
    # their declared dir is narrower than my area
    assert shared_area(["mcp-proxies/artefacts"], ["mcp-proxies/x.md"]) == "mcp-proxies"


def test_shared_area_needs_a_real_boundary_and_ignores_root():
    assert shared_area(["art"], ["artefacts/x.py"]) is None
    assert shared_area(["README.md"], ["CONTRIBUTING.md"]) is None
    assert shared_area([], ["a/b.py"]) is None


# --- matching ----------------------------------------------------------------

def test_match_intents_path_beats_topic_and_skips_unrelated():
    cands = [
        _intent(1, paths=["slack-bot"]),                      # same repo, other dir
        _intent(2, paths=["artefacts"]),                      # path match
        _intent(3, repo="other", topic="artefacts-publish"),  # topic match
        _intent(4, repo="other", paths=["artefacts"]),        # other repo, no topic
    ]
    out = match_intents(cands, REPO, "artefacts-publish", ["artefacts/publish.py"])
    assert [(e["id"], e["match"]) for e in out] == [(2, "path"), (3, "topic")]
    assert out[0]["shared"] == "artefacts"
    assert out[0]["person"] == "u2@finder.com"


def test_match_intents_without_my_paths_still_matches_topic():
    out = match_intents([_intent(1, paths=["x"], topic="t")], REPO, "t", [])
    assert [e["match"] for e in out] == ["topic"]


def test_match_intents_limit():
    cands = [_intent(i, paths=["artefacts"]) for i in range(10)]
    assert len(match_intents(cands, REPO, None, ["artefacts/a.py"], limit=3)) == 3


# --- privacy -----------------------------------------------------------------

def test_visible_intents_hides_private_from_others_only():
    rows = [
        _intent(1, email="me@finder.com", private=True),
        _intent(2, email="them@finder.com", private=True),
        _intent(3, email="them@finder.com", sess_private=True),
        _intent(4, email="them@finder.com"),
    ]
    assert [r["id"] for r in visible_intents(rows, "me@finder.com")] == [1, 4]


# --- HTTP + Postgres ---------------------------------------------------------

DB_URL = os.environ.get("CLAUSTRUM_TEST_DB_URL")
needs_db = pytest.mark.skipif(not DB_URL, reason="CLAUSTRUM_TEST_DB_URL not set")


@pytest.fixture
async def api(monkeypatch):
    import httpx

    from app import db, migrate
    from app.main import app

    monkeypatch.setenv("CLAUSTRUM_DB_URL", DB_URL)
    migrate.main()
    await db.open_pool()
    run = uuid.uuid4().hex[:8]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        def as_user(name):
            email = f"{name}-{run}@finder.com"
            hdr = {"X-Claustrum-User-Email": email}

            class U:
                pass

            u = U()
            u.email = email
            u.get = lambda path, **kw: client.get(path, headers=hdr, **kw)
            u.post = lambda path, **kw: client.post(path, headers=hdr, **kw)
            return u

        yield as_user, run
    await db.close_pool()


async def _checkin(u, uid, repo, private=False):
    r = await u.post("/v1/checkin", json={
        "uid": uid, "machine": "m", "label": "l", "repo": repo, "is_private": private,
    })
    assert r.status_code == 200, r.text


@needs_db
async def test_intent_crud_dedupe_and_close(api):
    as_user, run = api
    kelly = as_user("kelly")
    repo = f"wa-{run}"

    r = await kelly.post("/v1/intents", json={
        "title": "Artefacts  sharing links", "repo": repo, "paths": ["artefacts/", "../x"],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True
    intent = body["intent"]
    assert intent["title"] == "Artefacts sharing links"
    assert intent["paths"] == ["artefacts"]
    assert intent["status"] == "open"

    # Same title again (plan re-exited): no duplicate, paths unioned.
    r = await kelly.post("/v1/intents", json={
        "title": "artefacts sharing links", "repo": repo, "paths": ["mcp-proxies/artefacts"],
    })
    assert r.json()["created"] is False
    assert r.json()["intent"]["id"] == intent["id"]
    assert r.json()["intent"]["paths"] == ["artefacts", "mcp-proxies/artefacts"]

    r = await kelly.post(f"/v1/intents/{intent['id']}", json={"pr_number": 42, "refresh": True})
    assert r.json()["intent"]["pr_number"] == 42

    mine = (await kelly.get("/v1/intents", params={"mine": "true"})).json()["intents"]
    assert [i["id"] for i in mine] == [intent["id"]]

    # Only the owner may touch it.
    other = as_user("pritom")
    assert (await other.post(f"/v1/intents/{intent['id']}/close")).status_code == 404

    r = await kelly.post(f"/v1/intents/{intent['id']}/close", json={"status": "abandoned"})
    assert r.json()["intent"]["status"] == "abandoned"
    mine = (await kelly.get("/v1/intents", params={"mine": "true"})).json()["intents"]
    assert mine == []


@needs_db
async def test_intent_privacy_and_list_block(api):
    as_user, run = api
    kelly, pritom = as_user("kelly"), as_user("pritom")
    repo = f"wa-{run}"

    public = (await kelly.post("/v1/intents", json={
        "title": "Artefacts publish", "repo": repo, "paths": ["artefacts"],
    })).json()["intent"]
    await kelly.post("/v1/intents", json={
        "title": "Secret reorg", "repo": repo, "paths": ["artefacts"], "is_private": True,
    })
    # An intent whose owning session turns private is hidden — and stays hidden
    # after the cold-archive job moves that session out of `sessions`.
    await _checkin(kelly, f"k-priv-{run}", repo)
    await kelly.post("/v1/intents", json={
        "title": "From private session", "paths": ["artefacts"], "uid": f"k-priv-{run}",
    })
    from app import db
    from app.archive import SESSION_COLS
    async with db.conn() as c:
        await c.execute("UPDATE sessions SET is_private = true WHERE uid = %s", (f"k-priv-{run}",))
        await c.execute(
            f"WITH moved AS (DELETE FROM sessions WHERE uid = %s RETURNING {SESSION_COLS}) "
            f"INSERT INTO sessions_archive ({SESSION_COLS}) SELECT {SESSION_COLS} FROM moved",
            (f"k-priv-{run}",),
        )
        await c.commit()

    seen = (await pritom.get("/v1/intents", params={"repo": repo})).json()["intents"]
    assert [i["title"] for i in seen] == ["Artefacts publish"]
    own = (await kelly.get("/v1/intents", params={"mine": "true"})).json()["intents"]
    assert len(own) == 3

    # Pritom's session, far outside any recency window of Kelly's, editing the
    # same directory: the intent shows up in /v1/list.
    await _checkin(pritom, f"p-{run}", repo)
    r = await pritom.get("/v1/list", params={
        "uid": f"p-{run}", "files_touched": "artefacts/new_feature.py",
    })
    assert r.status_code == 200, r.text
    block = r.json()["intents"]
    assert [(i["id"], i["match"], i["shared"]) for i in block] == [(public["id"], "path", "artefacts")]

    # Own intents never come back as overlaps; opt-out removes the block.
    await _checkin(kelly, f"k-{run}", repo)
    r = await kelly.get("/v1/list", params={"uid": f"k-{run}", "files_touched": "artefacts/a.py"})
    assert r.json()["intents"] == []
    r = await pritom.get("/v1/list", params={"uid": f"p-{run}", "include_intents": "false"})
    assert "intents" not in r.json()

    # Closed intents stop matching.
    await kelly.post(f"/v1/intents/{public['id']}/close")
    r = await pritom.get("/v1/list", params={"uid": f"p-{run}", "files_touched": "artefacts/x.py"})
    assert r.json()["intents"] == []


@needs_db
async def test_reset_deletes_own_intents(api):
    as_user, run = api
    joe = as_user("joe")
    await joe.post("/v1/intents", json={"title": "x", "repo": f"r-{run}"})
    r = await joe.post("/v1/reset", json={})
    assert r.json()["deleted"]["intents"] == 1


@needs_db
async def test_intent_for_archived_private_session_inherits_privacy_and_repo(api):
    as_user, run = api
    kelly, pritom = as_user("kelly"), as_user("pritom")
    repo = f"wa-{run}"
    uid = f"k-cold-{run}"
    await _checkin(kelly, uid, repo)
    from app import db
    from app.archive import SESSION_COLS
    async with db.conn() as c:
        await c.execute("UPDATE sessions SET is_private = true WHERE uid = %s", (uid,))
        await c.execute(
            f"WITH moved AS (DELETE FROM sessions WHERE uid = %s RETURNING {SESSION_COLS}) "
            f"INSERT INTO sessions_archive ({SESSION_COLS}) SELECT {SESSION_COLS} FROM moved",
            (uid,),
        )
        await c.commit()

    # Declared only after the session went cold: it must still be recognised as
    # the owner, supply the repo, and keep the intent private.
    it = (await kelly.post("/v1/intents", json={
        "title": "Declared after archive", "paths": ["artefacts"], "uid": uid,
    })).json()["intent"]
    assert it["uid"] == uid and it["repo"] == repo
    async with db.conn() as c:
        cur = await c.execute("SELECT is_private FROM intents WHERE id = %s", (it["id"],))
        assert (await cur.fetchone())[0] is True
    seen = (await pritom.get("/v1/intents", params={"repo": repo})).json()["intents"]
    assert seen == []


@needs_db
async def test_list_degrades_when_intents_table_missing(api, monkeypatch):
    import psycopg

    from app.routes import list_peers

    as_user, run = api
    pritom = as_user("pritom")
    await _checkin(pritom, f"p-nomig-{run}", f"wa-{run}")

    async def missing(*a, **k):
        raise psycopg.errors.UndefinedTable('relation "intents" does not exist')

    monkeypatch.setattr(list_peers, "fetch_intent_candidates", missing)
    r = await pritom.get("/v1/list", params={"uid": f"p-nomig-{run}"})
    assert r.status_code == 200, r.text
    assert r.json()["intents"] == []


async def _status(intent_id):
    from app import db
    async with db.conn() as c:
        cur = await c.execute("SELECT status FROM intents WHERE id = %s", (intent_id,))
        return (await cur.fetchone())[0]


@needs_db
async def test_session_done_closes_its_intents_but_not_hand_declared_ones(api):
    as_user, run = api
    kelly = as_user("kelly")
    repo = f"wa-{run}"
    uid, other = f"k-done-{run}", f"k-other-{run}"
    await _checkin(kelly, uid, repo)
    await _checkin(kelly, other, repo)
    owned = (await kelly.post("/v1/intents", json={"title": "Plan A", "uid": uid})).json()["intent"]
    sibling = (await kelly.post("/v1/intents", json={"title": "Plan B", "uid": other})).json()["intent"]
    manual = (await kelly.post("/v1/intents", json={"title": "Someday", "repo": repo})).json()["intent"]

    r = await kelly.post("/v1/update", json={"uid": uid, "status": "done", "resolution": "shipped"})
    assert r.status_code == 200, r.text
    assert await _status(owned["id"]) == "done"
    assert await _status(sibling["id"]) == "open"
    assert await _status(manual["id"]) == "open"


@needs_db
async def test_merged_pr_closes_session_and_pr_linked_intents_only(api):
    as_user, run = api
    kelly, pritom = as_user("kelly"), as_user("pritom")
    repo = f"wa-{run}"
    impl, planner = f"k-impl-{run}", f"k-plan-{run}"
    await _checkin(kelly, impl, repo)
    await _checkin(kelly, planner, repo)
    by_session = (await kelly.post("/v1/intents", json={"title": "Impl", "uid": impl})).json()["intent"]
    by_pr = (await kelly.post("/v1/intents", json={
        "title": "Planned elsewhere", "uid": planner, "pr_number": 77,
    })).json()["intent"]
    manual_pr = (await kelly.post("/v1/intents", json={
        "title": "Hand-declared", "repo": repo, "pr_number": 77,
    })).json()["intent"]
    other_pr = (await kelly.post("/v1/intents", json={
        "title": "Different PR", "uid": planner, "pr_number": 78,
    })).json()["intent"]
    await _checkin(pritom, f"p-{run}", repo)
    theirs = (await pritom.post("/v1/intents", json={
        "title": "Pritom's", "uid": f"p-{run}", "pr_number": 77,
    })).json()["intent"]

    r = await kelly.post("/v1/intents/close-linked", json={"uid": impl, "repo": repo, "pr_number": 77})
    assert r.status_code == 200, r.text
    assert sorted(r.json()["closed"]) == sorted([by_session["id"], by_pr["id"]])
    assert await _status(manual_pr["id"]) == "open"
    assert await _status(other_pr["id"]) == "open"
    assert await _status(theirs["id"]) == "open"

    assert (await kelly.post("/v1/intents/close-linked", json={})).status_code == 422
