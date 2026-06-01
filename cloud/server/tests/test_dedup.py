"""Pure unit tests for the /v1/list dedup engine — no DB, no Docker.

These cover the part that actually carries the dedup logic: how candidate
peers are bucketed into overlap tiers. The DB query that *produces* the
candidate set is exercised by the optional HTTP integration pass in
test-local.sh (gated on CLAUSTRUM_DB_URL) and, for real, by the staging deploy.
"""

from app.routes.list_peers import _parents, bucket_tiers

REPO = "joewaller/claustrum"


def _mk(uid, repo=None, topic=None, pr=None, files=None):
    return {
        "uid": uid,
        "user_email": f"{uid}@finder.com",
        "machine": "m",
        "repo": repo,
        "branch": "b",
        "topic": topic,
        "status": "active",
        "pr_number": pr,
        "last_seen": None,
        "working_on": None,
        "files_touched": files or [],
    }


def _ids(peers):
    return [p["uid"] for p in peers]


def test_parents_immediate_dir_only():
    assert _parents(["src/routes/list.py", "top.py", "a/b/c.py"]) == {"src/routes", "a/b"}
    assert _parents([]) == set()
    assert _parents(["  ", ""]) == set()


def test_strongest_tier_wins_no_double_count():
    my_files = [
        "cloud/server/app/routes/list_peers.py",
        "cloud/server/app/routes/update.py",
    ]
    cands = [
        _mk("p1", repo=REPO, files=["cloud/server/app/routes/list_peers.py"]),  # t1 exact file
        _mk("p2", repo=REPO, pr=42),                                            # t2 same PR
        _mk("p3", repo=REPO, files=["cloud/server/app/routes/checkin.py"]),     # t2 shared dir
        _mk("p4", repo=REPO, topic="dedup-core"),                               # t3 topic (same repo, no file/pr/dir)
        _mk("p5", repo="other/repo", topic="dedup-core"),                       # t3 topic, different repo
        _mk("p6", repo=REPO, files=["docs/readme.md"]),                         # t4 repo only
    ]
    t1, t2, t3, t4 = bucket_tiers(cands, REPO, "dedup-core", 42, my_files)

    assert _ids(t1) == ["p1"]
    assert set(_ids(t2)) == {"p2", "p3"}
    assert set(_ids(t3)) == {"p4", "p5"}   # same-topic beats same-repo-only
    assert _ids(t4) == ["p6"]

    assert t1[0]["overlap_files"] == ["cloud/server/app/routes/list_peers.py"]
    assert any(p["reason"] == "pr:42" for p in t2)
    assert any(p["reason"].startswith("dir:") for p in t2)


def test_cross_repo_without_shared_topic_never_surfaces():
    # The SQL filters these out before bucket_tiers, but the function must also
    # be defensive — a row that shares neither repo nor topic belongs nowhere.
    t1, t2, t3, t4 = bucket_tiers([_mk("x", repo="z/z")], REPO, None, None, ["a/b.py"])
    assert (t1, t2, t3, t4) == ([], [], [], [])


def test_dedup_works_with_empty_taxonomy():
    # The whole point of the design pivot: file/repo dedup must not depend on
    # any topic existing.
    my_files = ["cloud/server/app/routes/list_peers.py"]
    cands = [_mk("y", repo=REPO, files=my_files)]
    t1, t2, t3, t4 = bucket_tiers(cands, REPO, None, None, my_files)
    assert _ids(t1) == ["y"]
    assert t3 == [] and t4 == []


def test_file_overlap_requires_same_repo():
    # Identical file paths in different repos are not a real collision.
    my_files = ["app/main.py"]
    cands = [_mk("z", repo="someone/else", files=["app/main.py"], topic="t")]
    t1, t2, t3, t4 = bucket_tiers(cands, REPO, "t", None, my_files)
    assert t1 == []          # not t1 — different repo
    assert _ids(t3) == ["z"]  # falls through to topic
