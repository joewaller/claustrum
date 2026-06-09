"""Pure unit tests for the /v1/list dedup engine — no DB, no Docker.

These cover the part that actually carries the dedup logic: how candidate
peers are bucketed into overlap tiers. The DB query that *produces* the
candidate set is exercised by the optional HTTP integration pass in
test-local.sh (gated on CLAUSTRUM_DB_URL) and, for real, by the staging deploy.
"""

from app.routes.list_peers import _parents, bucket_tiers, solved_matches

REPO = "joewaller/claustrum"


def _mk(uid, repo=None, topic=None, pr=None, files=None, done_at=None, resolution=None):
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
        "done_at": done_at,
        "resolution": resolution,
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


# --- solved-problem archive (Phase 5) --------------------------------------
# solved_matches reuses bucket_tiers, so it inherits the strongest-tier-wins
# semantics; these tests cover the archive-specific behaviour: tier ordering of
# the flat list, the carried resolution layer, and the limit.

def test_solved_ranks_strongest_tier_first_and_carries_resolution():
    my_files = ["cloud/server/app/routes/list_peers.py"]
    cands = [
        _mk("repo_only", repo=REPO, files=["docs/x.md"],
            done_at="2026-05-01", resolution="tidied docs"),                 # t4
        _mk("exact", repo=REPO, files=my_files,
            done_at="2026-06-01", resolution="fixed by PR #15 (commit 2c708a5)"),  # t1
        _mk("topic", repo="other/repo", topic="dedup-core",
            done_at="2026-05-20", resolution="topic-level fix"),             # t3
    ]
    out = solved_matches(cands, REPO, "dedup-core", None, my_files)

    # Flat list ordered by tier strength: t1, then t3, then t4.
    assert _ids(out) == ["exact", "topic", "repo_only"]
    assert [e["match_tier"] for e in out] == [
        "t1_file_overlap", "t3_topic", "t4_repo",
    ]
    # Resolution layer is surfaced for the "solved by X: <how>" message.
    top = out[0]
    assert top["resolution"] == "fixed by PR #15 (commit 2c708a5)"
    assert top["person"] == "exact@finder.com"
    assert top["done_at"] == "2026-06-01"
    assert top["overlap_files"] == my_files


def test_solved_respects_limit():
    cands = [_mk(f"d{i}", repo=REPO, files=["a/b.py"]) for i in range(8)]
    out = solved_matches(cands, REPO, None, None, ["a/b.py"], limit=3)
    assert len(out) == 3


def test_solved_empty_when_no_overlap():
    # A done session sharing neither repo nor topic is never a "solved before".
    cands = [_mk("unrelated", repo="x/y", files=["p/q.py"])]
    assert solved_matches(cands, REPO, "mine", None, ["a/b.py"]) == []
