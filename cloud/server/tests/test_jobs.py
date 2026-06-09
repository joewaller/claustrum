"""Pure unit tests for the Phase 3 housekeeping-job decision logic — no DB.

These cover the part that carries the actual policy: which proposals get
promoted / rejected, when an active session is considered stale, and which
topics are concentrated enough to alert. The SQL that produces the inputs and
applies the decisions is exercised by the staging deploy (POST each /jobs/*
endpoint as the scheduler SA and check row transitions).
"""

from datetime import datetime, timedelta, timezone

from app.routes.jobs import (
    classify_proposals,
    concentrated_topics,
    is_session_stale,
)
from app.routes.propose import PROMOTION_THRESHOLD

NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)


def _group(name, distinct_users, *, age_days=0, already_official=False, description="d"):
    return {
        "name": name,
        "distinct_users": distinct_users,
        "oldest_created_at": NOW - timedelta(days=age_days),
        "already_official": already_official,
        "description": description,
    }


# --- classify_proposals -----------------------------------------------------

def test_threshold_is_two():
    # The whole point of the 3->2 change. Two distinct proposers promote.
    assert PROMOTION_THRESHOLD == 2


def test_promotes_at_threshold_carries_description_and_count():
    groups = [_group("gateway-deploy", 2, description="deploying the gateway")]
    out = classify_proposals(groups, NOW)
    assert [p["name"] for p in out["promote"]] == ["gateway-deploy"]
    p = out["promote"][0]
    assert p["description"] == "deploying the gateway"
    assert p["distinct_users"] == 2
    assert out["reject_stale"] == []
    assert out["resolve_already_official"] == []


def test_single_proposer_stays_open_when_fresh():
    groups = [_group("lonely-topic", 1, age_days=1)]
    out = classify_proposals(groups, NOW)
    assert out == {"promote": [], "resolve_already_official": [], "reject_stale": []}


def test_single_proposer_rejected_when_stale():
    groups = [_group("old-topic", 1, age_days=8)]
    out = classify_proposals(groups, NOW)
    assert out["reject_stale"] == ["old-topic"]
    assert out["promote"] == []


def test_stale_boundary_is_inclusive_of_just_under_seven_days():
    # Exactly 7 days old is NOT yet stale (cutoff is strict <); 7d+1s is.
    assert classify_proposals([_group("a", 1, age_days=7)], NOW)["reject_stale"] == []
    older = [{
        "name": "b", "distinct_users": 1,
        "oldest_created_at": NOW - timedelta(days=7, seconds=1),
        "already_official": False, "description": "d",
    }]
    assert classify_proposals(older, NOW)["reject_stale"] == ["b"]


def test_threshold_beats_staleness():
    # An old proposal that reached the threshold is still promoted, not rejected.
    groups = [_group("old-but-popular", 2, age_days=30)]
    out = classify_proposals(groups, NOW)
    assert [p["name"] for p in out["promote"]] == ["old-but-popular"]
    assert out["reject_stale"] == []


def test_already_official_is_resolved_never_repromoted():
    # Idempotency: a name that already exists in `topics` is resolved, not
    # re-inserted — even if it has plenty of distinct proposers.
    groups = [_group("known", 5, already_official=True)]
    out = classify_proposals(groups, NOW)
    assert out["resolve_already_official"] == ["known"]
    assert out["promote"] == []
    assert out["reject_stale"] == []


def test_mixed_batch_routes_each_group():
    groups = [
        _group("promote-me", 2),
        _group("official", 3, already_official=True),
        _group("stale", 1, age_days=10),
        _group("waiting", 1, age_days=2),
    ]
    out = classify_proposals(groups, NOW)
    assert [p["name"] for p in out["promote"]] == ["promote-me"]
    assert out["resolve_already_official"] == ["official"]
    assert out["reject_stale"] == ["stale"]
    # "waiting" is fresh + below threshold -> stays open (in no bucket).


def test_null_description_coalesces_to_empty_string():
    groups = [_group("nd", 2, description=None)]
    out = classify_proposals(groups, NOW)
    assert out["promote"][0]["description"] == ""


# --- is_session_stale -------------------------------------------------------

def test_fresh_session_not_stale():
    assert is_session_stale(NOW - timedelta(minutes=5), NOW) is False


def test_old_session_stale():
    assert is_session_stale(NOW - timedelta(minutes=61), NOW) is True


def test_stale_boundary_60_min():
    assert is_session_stale(NOW - timedelta(minutes=60), NOW) is False
    assert is_session_stale(NOW - timedelta(minutes=60, seconds=1), NOW) is True


def test_missing_last_seen_counts_stale():
    assert is_session_stale(None, NOW) is True


# --- concentrated_topics ----------------------------------------------------

def test_concentration_threshold_three():
    rows = [
        {"topic": "hot", "count": 3, "uids": ["a", "b", "c"]},
        {"topic": "warm", "count": 2, "uids": ["d", "e"]},
        {"topic": "blazing", "count": 9, "uids": list("123456789")},
    ]
    out = concentrated_topics(rows)
    assert [r["topic"] for r in out] == ["hot", "blazing"]


def test_concentration_empty_when_none_reach_threshold():
    rows = [{"topic": "x", "count": 1, "uids": ["a"]}]
    assert concentrated_topics(rows) == []
