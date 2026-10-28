"""merge_cli batch approval (`approve-plan --live` / PLAN.json) and merge-plan
quality: reason/kind filters, dry-run without --yes, check_decision on every
batch, the per-side adopter minimum, and an untruncated plan.

The DB tests run only when CLAUSTRUM_TEST_DB_URL points at a throwaway Postgres
whose role may CREATE DATABASE: each test gets its own fresh database (migrated,
seeded, dropped afterwards), because `--live` plans over the whole taxonomy.
"""

import json
import os
import uuid

import pytest

from app import merge_cli, merge_plan
from app.merge_cli import main, plan_decisions
from app.taxonomy import MIN_ADOPTERS_FOR_ADOPTER_MATCH, propose_merges

PLAN = {
    "topic": [
        {"alias": "artefacts-site", "canonical": "artifacts-site", "score": 1.0,
         "reasons": ["name", "stem:artifacts"]},
        {"alias": "bing-ads-us", "canonical": "google-ads-us", "score": 1.5,
         "reasons": ["related", "adopters_j=1.00"]},
        {"alias": "team-ai-adoption", "canonical": "artifacts-auto-refresh", "score": 1.0,
         "reasons": ["adopters", "adopters_j=1.00"]},
    ],
    "domain": [
        {"alias": "contracts-review", "canonical": "contract-review", "score": 1.0,
         "reasons": ["name"]},
    ],
}


def _pairs(decisions):
    return [(k, a, c) for k, a, c, _ in decisions]


# --- plan_decisions filters (pure) ------------------------------------------

def test_plan_decisions_filters_by_reason():
    assert _pairs(plan_decisions(PLAN, "all", ["name"])) == [
        ("topic", "artefacts-site", "artifacts-site"),
        ("domain", "contracts-review", "contract-review"),
    ]
    # Repeatable: any listed reason qualifies. Matching is exact, not prefix.
    assert [a for _, a, _, _ in plan_decisions(PLAN, "all", ["related", "adopters"])] == [
        "bing-ads-us", "team-ai-adoption",
    ]
    assert plan_decisions(PLAN, "all", ["stem"]) == []
    assert len(plan_decisions(PLAN, "all", None)) == 4


def test_plan_decisions_filters_by_kind():
    assert {k for k, *_ in plan_decisions(PLAN, "topic")} == {"topic"}
    assert _pairs(plan_decisions(PLAN, "domain", ["name"])) == [
        ("domain", "contracts-review", "contract-review"),
    ]
    assert plan_decisions(PLAN, "domain", ["related"]) == []


def test_approve_plan_arg_validation():
    with pytest.raises(SystemExit):
        main(["approve-plan", "--by", "x@finder.com"])            # neither file nor --live
    with pytest.raises(SystemExit):
        main(["approve-plan", "p.json", "--live", "--by", "x@finder.com"])
    with pytest.raises(SystemExit):
        main(["approve-plan", "--live", "--by", "nobody"])
    with pytest.raises(SystemExit):
        main(["approve-plan", "--live"])                          # --by is required
    with pytest.raises(SystemExit):
        main(["approve-plan", "--live", "--kind", "bogus", "--by", "x@finder.com"])


# --- proposal quality (pure) ------------------------------------------------

def _e(name, adopters=(), sessions=1, source="proposed"):
    return {"name": name, "adopters": set(adopters), "sessions": sessions, "source": source}


def _merge_map(rows):
    return {r["alias"]: r["canonical"] for r in rows}


def test_adopter_minimum_suppresses_tiny_sets():
    assert MIN_ADOPTERS_FOR_ADOPTER_MATCH == 3
    # Related names, 1-2 people each: Jaccard 1.0 is trivial.
    assert propose_merges([_e("bing-ads-us", ["a", "b"], 4),
                           _e("google-ads-us", ["a", "b"], 9)]) == []
    assert propose_merges([_e("nab-cards-eom", ["a"]), _e("anz-cards-eom", ["a"])]) == []
    # Unrelated names, union of 3 but one side has only 2.
    assert propose_merges([_e("team-ai-adoption", ["a", "b"]),
                           _e("artifacts-auto-refresh", ["a", "b", "c"])]) == []
    # Both sides at the minimum: still proposed.
    out = propose_merges([_e("bing-ads-us", ["a", "b", "c"], 4),
                          _e("google-ads-us", ["a", "b", "c"], 9)])
    assert _merge_map(out) == {"bing-ads-us": "google-ads-us"}
    assert out[0]["reasons"][0] == "related"


def test_adopter_minimum_keeps_name_matches():
    out = propose_merges([_e("artifacts-site", ["joe"], 4), _e("artifacts", ["joe", "kev"], 9),
                          _e("contracts-review", ["a"]), _e("contract-review", ["a", "b"], 3)])
    assert _merge_map(out) == {"artifacts-site": "artifacts",
                               "contracts-review": "contract-review"}
    assert all(r["reasons"][0] == "name" for r in out)


class _FakeCursor:
    """Answers build_merge_plan's queries from canned rows, keyed on SQL text."""

    def __init__(self, topics):
        self.topics = topics
        self._rows = []

    async def execute(self, sql, params=None):
        if "FROM topics" in sql and "parent" in sql:
            self._rows = [(n, "proposed", None) for n in self.topics]
        else:
            self._rows = []

    async def fetchall(self):
        return self._rows


async def test_merge_plan_is_not_truncated():
    names = [n for i in range(600) for n in (f"widget{i}", f"widget{i}s")]
    plan = await merge_plan.build_merge_plan(_FakeCursor(names))
    assert len(plan["topic"]) == 600
    text = merge_plan.format_plan(plan)
    assert "== topic merges proposed: 600 ==" in text
    assert text.count("    <- ") == 600


# --- DB: --live / file approval end to end ----------------------------------

DB_URL = os.environ.get("CLAUSTRUM_TEST_DB_URL")
needs_db = pytest.mark.skipif(not DB_URL, reason="CLAUSTRUM_TEST_DB_URL not set")


@pytest.fixture
def fresh_db(monkeypatch):
    import psycopg
    from psycopg.conninfo import make_conninfo

    from app import migrate

    name = f"claustrum_merge_{uuid.uuid4().hex[:10]}"
    try:
        admin = psycopg.connect(DB_URL, autocommit=True)
        admin.execute(f'CREATE DATABASE "{name}"')
    except psycopg.Error as e:
        pytest.skip(f"cannot create a throwaway database: {e}")
    url = make_conninfo(DB_URL, dbname=name)
    monkeypatch.setenv("CLAUSTRUM_DB_URL", url)
    try:
        migrate.main()
        with psycopg.connect(url) as c:
            _seed(c)
        yield url
    finally:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


SEED_DOMAINS = ("payments-zq", "payment-zq")
# topic -> (domain, adopters)
SEED_TOPICS = {
    "artifacts-zq": ("payments-zq", ["a", "b", "c"]),
    "artefacts-zq": ("payments-zq", ["a"]),                 # name -> artifacts-zq
    "alpha-zq-thing": ("payments-zq", ["p", "q", "r"]),
    "omega-zq-other": ("payments-zq", ["p", "q", "r"]),     # adopters only
    "bing-ads-zq": ("payments-zq", ["x", "y"]),
    "google-ads-zq": ("payments-zq", ["x", "y"]),           # tiny sets: no proposal
}


def _seed(c):
    for d in SEED_DOMAINS:
        c.execute("INSERT INTO domains (name, description, source) VALUES (%s, 'd', 'proposed')",
                  (d,))
    for topic, (domain, adopters) in SEED_TOPICS.items():
        c.execute("INSERT INTO topics (name, description, source, domain) "
                  "VALUES (%s, 'd', 'proposed', %s)", (topic, domain))
        for who in adopters:
            c.execute(
                "INSERT INTO sessions (uid, user_email, machine, topic, domain, last_seen, "
                "started_at) VALUES (%s, %s, 'm', %s, %s, now(), now())",
                (uuid.uuid4().hex, f"{who}@finder.com", topic, domain),
            )
    # One person in the second domain so it has usage of its own.
    c.execute("INSERT INTO sessions (uid, user_email, machine, domain, last_seen, started_at) "
              "VALUES (%s, 'a@finder.com', 'm', 'payment-zq', now(), now())",
              (uuid.uuid4().hex,))


def _approved(url):
    import psycopg

    with psycopg.connect(url) as c:
        rows = c.execute("SELECT kind, alias, canonical, decided_by FROM taxonomy_aliases "
                         "WHERE status = 'approved' ORDER BY kind, alias").fetchall()
    return [r for r in rows if "zq" in r[1]]


def _zq_lines(out):
    return [ln for ln in out.splitlines() if "zq" in ln]


@needs_db
def test_live_plan_shows_name_and_adopter_proposals_only(fresh_db, capsys):
    assert main(["plan", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    topics = {r["alias"]: (r["canonical"], r["reasons"][0]) for r in plan["topic"]}
    assert topics["artefacts-zq"] == ("artifacts-zq", "name")
    assert topics["omega-zq-other"] == ("alpha-zq-thing", "adopters")
    assert "bing-ads-zq" not in topics and "google-ads-zq" not in topics


@needs_db
def test_live_without_yes_is_a_dry_run(fresh_db, capsys):
    rc = main(["approve-plan", "--live", "--only-reason", "name", "--by", "joe@finder.com"])
    assert rc == 0
    out = capsys.readouterr().out
    lines = _zq_lines(out)
    assert any(ln.startswith("would approve topic artefacts-zq -> artifacts-zq  [name")
               for ln in lines)
    assert any(ln.startswith("would approve domain payment-zq -> payments-zq") for ln in lines)
    assert not any("omega-zq-other" in ln for ln in lines)
    assert "nothing written" in out
    assert _approved(fresh_db) == []


@needs_db
def test_live_yes_writes_only_filtered_kind_and_reason(fresh_db, capsys):
    rc = main(["approve-plan", "--live", "--only-reason", "name", "--kind", "topic",
               "--by", "joe@finder.com", "--yes"])
    assert rc == 0
    lines = _zq_lines(capsys.readouterr().out)
    assert lines == [ln for ln in lines if ln.startswith("approved topic ")]
    assert _approved(fresh_db) == [("topic", "artefacts-zq", "artifacts-zq", "joe@finder.com")]

    # The rest, unfiltered, as a second batch: the adopter-only topic + the domain.
    assert main(["approve-plan", "--live", "--by", "kev@finder.com", "--yes"]) == 0
    approved = {(k, a): c for k, a, c, _ in _approved(fresh_db)}
    assert approved[("topic", "omega-zq-other")] == "alpha-zq-thing"
    assert {k for k, _ in approved} == {"topic", "domain"}
    assert ("topic", "bing-ads-zq") not in approved


@needs_db
def test_live_enforces_check_decision(fresh_db, capsys, monkeypatch):
    async def bad_plan(cur, window_days):
        return {"domain": [], "topic": [
            {"alias": "artefacts-zq", "canonical": "artifacts-zq", "reasons": ["name"]},
            # Loops back through the approval just above in the same batch.
            {"alias": "artifacts-zq", "canonical": "artefacts-zq", "reasons": ["name"]},
            {"alias": "bing-ads-zq", "canonical": "free-text-zq", "reasons": ["name"]},
        ]}

    monkeypatch.setattr(merge_cli, "build_merge_plan", bad_plan)
    assert main(["approve-plan", "--live", "--by", "joe@finder.com"]) == 0
    dry = _zq_lines(capsys.readouterr().out)
    assert dry[0].startswith("would approve topic artefacts-zq -> artifacts-zq")
    assert "loop" in dry[1] and dry[1].startswith("would skip topic artifacts-zq")
    assert "not in the topic taxonomy" in dry[2]

    assert main(["approve-plan", "--live", "--by", "joe@finder.com", "--yes"]) == 1
    real = _zq_lines(capsys.readouterr().out)
    assert [ln.split(":")[0] for ln in real] == [
        "approved topic artefacts-zq -> artifacts-zq  [name]",
        "skip topic artifacts-zq -> artefacts-zq",
        "skip topic bing-ads-zq -> free-text-zq",
    ]
    assert _approved(fresh_db) == [("topic", "artefacts-zq", "artifacts-zq", "joe@finder.com")]


@needs_db
def test_file_plan_still_approves(fresh_db, capsys, tmp_path):
    plan_file = tmp_path / "plan.json"
    assert main(["plan", "--json"]) == 0
    plan_file.write_text(capsys.readouterr().out)
    assert main(["approve-plan", str(plan_file), "--kind", "domain",
                 "--by", "joe@finder.com"]) == 0
    assert _approved(fresh_db) == []
    assert main(["approve-plan", str(plan_file), "--kind", "domain",
                 "--by", "joe@finder.com", "--yes"]) == 0
    assert [(k, c) for k, _, c, _ in _approved(fresh_db)] == [("domain", "payments-zq")]
