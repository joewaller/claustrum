"""Pure unit tests for the /v1/list dedup engine — no DB, no Docker.

These cover the part that actually carries the dedup logic: how candidate
peers are bucketed into overlap tiers. The DB query that *produces* the
candidate set is exercised by the optional HTTP integration pass in
test-local.sh (gated on CLAUSTRUM_DB_URL) and, for real, by the staging deploy.
"""

from app.routes.list_peers import (
    _parents,
    alias_group,
    bucket_related,
    bucket_tiers,
    solved_matches,
)

REPO = "joewaller/claustrum"


def _mk(uid, repo=None, topic=None, pr=None, files=None, done_at=None, resolution=None,
        domain=None):
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
        "domain": domain,
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


def test_solved_matches_topic_across_an_approved_alias():
    cands = [_mk("old", repo="x/y", topic="artifacts", done_at="2026-05-01", resolution="r")]
    aliases = {"artifacts": "artefacts"}
    assert solved_matches(cands, REPO, "artefacts", None, []) == []
    out = solved_matches(cands, REPO, "artefacts", None, [], aliases=aliases)
    assert [(e["uid"], e["match_tier"]) for e in out] == [("old", "t3_topic")]


def test_alias_group_spans_every_name_for_the_canonical():
    aliases = {"artifacts": "artefacts", "artefact": "artefacts", "wp": "wordpress"}
    assert alias_group("artifacts", aliases) == ["artefact", "artefacts", "artifacts"]
    assert alias_group("solo", aliases) == ["solo"]
    assert alias_group(None, aliases) == []


def test_solved_empty_when_no_overlap():
    # A done session sharing neither repo nor topic is never a "solved before".
    cands = [_mk("unrelated", repo="x/y", files=["p/q.py"])]
    assert solved_matches(cands, REPO, "mine", None, ["a/b.py"]) == []


# --- alias-aware t3 + t3b related tier --------------------------------------
# Approved merges (taxonomy_aliases) and legacy parents resolve at read time, and
# t3b catches the overlap exact-topic t3 misses when the taxonomy fragments.

ALIASES = {"artefacts": "artifacts", "artifacts-auto-refresh": "artifacts"}


def _tiers(cands, my_topic, my_domain=None, aliases=None, domain_aliases=None):
    tiers = bucket_tiers(cands, REPO, my_topic, None, [], aliases=aliases)
    t3b, t4 = bucket_related(cands, tiers, my_topic, my_domain, aliases, domain_aliases)
    return tiers[2], t3b, t4


def test_t3_matches_across_an_approved_alias():
    cands = [_mk("kelly", repo="other/repo", topic="artefacts")]
    t3, t3b, _ = _tiers(cands, "artifacts-auto-refresh", aliases=ALIASES)
    assert _ids(t3) == ["kelly"] and t3b == []
    # Without the alias it is only related, not the same topic.
    t3, t3b, _ = _tiers(cands, "artifacts-auto-refresh")
    assert t3 == [] and _ids(t3b) == ["kelly"]


def test_related_tier_buckets_fragmented_topics():
    # The Kelly / Pritom / Anto case: overlapping artefacts work under different
    # topic names was never matched by exact-topic t3.
    cands = [
        _mk("kelly", repo="finder/artifacts", topic="artefacts-reviews"),
        _mk("pritom", repo="finder/other", topic="sharing-ui", domain="artifacts-site"),
        _mk("anto", repo="x/y", topic="bigquery-costs", domain="data"),
    ]
    _, t3b, _ = _tiers(cands, "artifacts-app", my_domain="artifacts-site")
    assert _ids(t3b) == ["kelly", "pritom"]           # strongest reason first
    assert t3b[0]["reason"] == "stem:artifact"
    assert t3b[1]["reason"] == "domain:artifacts-site"


def test_related_moves_peer_out_of_t4_without_double_counting():
    cands = [
        _mk("same-repo-related", repo=REPO, topic="artifacts-site"),
        _mk("same-repo-unrelated", repo=REPO, topic="gateway-deploy"),
    ]
    _, t3b, t4 = _tiers(cands, "artifacts-app")
    assert _ids(t3b) == ["same-repo-related"]
    assert _ids(t4) == ["same-repo-unrelated"]


def test_related_never_steals_from_stronger_tiers():
    my_files = ["a/b.py"]
    cands = [_mk("file", repo=REPO, topic="artifacts-site", files=my_files),
             _mk("topic", repo="o/r", topic="artifacts-app")]
    tiers = bucket_tiers(cands, REPO, "artifacts-app", None, my_files)
    t3b, _ = bucket_related(cands, tiers, "artifacts-app", None)
    assert _ids(tiers[0]) == ["file"] and _ids(tiers[2]) == ["topic"]
    assert t3b == []


def test_broad_domain_alone_is_not_related():
    cands = [_mk("p", repo="o/r", topic="something-else", domain="engineering")]
    _, t3b, _ = _tiers(cands, "artifacts-app", my_domain="engineering")
    assert t3b == []


def test_domain_alias_resolves_before_comparing():
    cands = [_mk("p", repo="o/r", topic="foo-bar", domain="artefacts")]
    _, t3b, _ = _tiers(cands, "baz-qux", my_domain="artifacts",
                       domain_aliases={"artefacts": "artifacts"})
    assert t3b and t3b[0]["reason"] == "domain:artifacts"
