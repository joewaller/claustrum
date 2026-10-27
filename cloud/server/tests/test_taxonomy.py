"""Pure unit tests for app/taxonomy.py — name normalisation, relatedness, alias
resolution — plus the merge-plan formatting and approval guard. No DB."""

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

from app import taxonomy as tx
from app.merge_cli import check_decision
from app.merge_plan import entries_from_rows, format_plan

_CLI_PATH = Path(__file__).resolve().parents[3] / "claustrum"


def _load_cli():
    loader = importlib.machinery.SourceFileLoader("claustrum_cli_tx", str(_CLI_PATH))
    spec = importlib.util.spec_from_loader("claustrum_cli_tx", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["claustrum_cli_tx"] = mod
    loader.exec_module(mod)
    return mod


cli = _load_cli()


# --- normalisation ------------------------------------------------------------

def test_en_au_and_en_us_spellings_meet():
    pairs = [
        ("artefacts", "artifact"),
        ("artefact", "artifacts"),
        ("colour", "color"),
        ("colours", "color"),
        ("optimise", "optimize"),
        ("organisation", "organization"),
        ("organisations", "organization"),
        ("analyse", "analyze"),
        ("behaviour", "behavior"),
        ("catalogue", "catalog"),
        ("centre", "center"),
        ("licence", "license"),
    ]
    for a, b in pairs:
        assert tx.norm_token(a) == tx.norm_token(b), (a, b)


def test_plural_and_singular_meet():
    for a, b in [("pages", "page"), ("caches", "cache"), ("policies", "policy"),
                 ("reviews", "review"), ("patches", "patch")]:
        assert tx.norm_token(a) == tx.norm_token(b), (a, b)


def test_words_that_only_look_plural_are_kept():
    assert tx.norm_token("status") == "status"
    assert tx.norm_token("class") == "class"
    assert tx.norm_token("analysis") == "analysis"
    assert tx.norm_token("ads") == "ads"          # too short to strip safely


def test_suffix_rules_do_not_collide_distinct_words():
    for a, b in [("state", "stats"), ("plane", "plans"), ("canvas", "canva"),
                 ("news", "new")]:
        assert tx.norm_token(a) != tx.norm_token(b), (a, b)
    assert not tx.is_near_duplicate("session-state", "session-stats")
    assert tx.norm_token("statistics") == tx.norm_token("stats")
    assert tx.norm_token("aliases") == tx.norm_token("alias")


def test_abbreviations_expand():
    assert tx.norm_tokens("wp-mcp") == tx.norm_tokens("wordpress-mcp")
    assert tx.norm_tokens("bq-export") == tx.norm_tokens("bigquery-export")


def test_client_normaliser_matches_server():
    # The CLI ranks candidates with its own stdlib copy; it must never drift.
    words = ["artefacts", "Colours", "optimisation", "analyses", "pages", "wp",
             "status", "policies", "behaviour", "theatre", "k8s", "deployments",
             "artifacts-auto-refresh", "local-dev-infrastructure", "x",
             "stats", "state", "canvas", "aliases", "planes", "news"]
    for w in words:
        assert cli._norm_tokens(w) == tx.norm_tokens(w), w
    assert cli._NORM_GENERIC == tx.GENERIC_TOKENS


# --- near-duplicates ------------------------------------------------------------

def test_spelling_variants_are_near_duplicates():
    assert tx.is_near_duplicate("artefacts-reviews", "artifact-review")
    assert tx.is_near_duplicate("colour-palette", "color-palettes")


def test_generic_suffix_is_a_near_duplicate():
    # Same subject once kind-of-work tokens are dropped.
    assert tx.is_near_duplicate("artifacts-app", "artefacts")
    assert tx.is_near_duplicate("artifacts-site", "artifacts")


def test_distinct_subjects_are_not_near_duplicates():
    assert not tx.is_near_duplicate("artifacts-publish", "artifacts")
    assert not tx.is_near_duplicate("wordpress-mcp", "wordpress")   # Joe's call (0009)
    assert not tx.is_near_duplicate("data-analytics-pipeline", "data-analytics")
    assert not tx.is_near_duplicate("app", "site")                  # all-generic


# --- relatedness (t3b) -----------------------------------------------------------

def test_topic_relation_reasons():
    assert tx.topic_relation("artifacts-app", None, "artefacts-reviews", None) == "stem:artifact"
    assert tx.topic_relation("artifacts-app", None, "artefacts", None) == "topic~artefacts"
    assert tx.topic_relation("x-one", "artifacts", "y-two", "artifacts") == "domain:artifacts"


def test_broad_domains_and_identical_topics_are_not_related():
    assert tx.topic_relation("x-one", "engineering", "y-two", "engineering") is None
    assert tx.topic_relation("same", None, "same", None) is None
    assert tx.topic_relation("gateway-deploy", None, "bigquery-costs", None) is None


# --- aliases ----------------------------------------------------------------------

def test_resolve_follows_chains_and_survives_cycles():
    m = {"a": "b", "b": "c"}
    assert tx.resolve("a", m) == "c"
    assert tx.resolve("z", m) == "z"
    assert tx.resolve(None, m) is None
    loop = {"a": "b", "b": "a"}
    assert tx.resolve("a", loop) in ("a", "b")    # terminates


def test_alias_map_combines_parent_and_approved_alias():
    m = tx.build_alias_map(
        parent_rows=[("gateway", "mcp-gateway"), ("artefacts", "old-target")],
        alias_rows=[("artefacts", "artifacts"), ("artifacts-site", "artifacts")],
    )
    assert m["gateway"] == "mcp-gateway"
    assert m["artefacts"] == "artifacts"          # approved alias wins over parent
    assert tx.alias_group("artifacts", m) == ["artefacts", "artifacts", "artifacts-site"]


# --- merge plan output + approval guard ----------------------------------------------

def test_entries_from_rows_includes_unpromoted_names():
    entries = entries_from_rows(
        [("artifacts", "proposed")],
        [("artifacts", ["a@x", "b@x"], 5), ("artefacts-free-text", ["c@x"], 1)],
    )
    by = {e["name"]: e for e in entries}
    assert by["artifacts"]["adopters"] == {"a@x", "b@x"}
    assert by["artefacts-free-text"]["source"] is None


def test_format_plan_groups_by_canonical():
    out = format_plan({
        "domain": [],
        "topic": [
            {"alias": "artefacts", "canonical": "artifacts", "score": 1.5,
             "reasons": ["name"], "alias_adopters": 1, "canonical_adopters": 4},
            {"alias": "artifacts-site", "canonical": "artifacts", "score": 1.2,
             "reasons": ["name"], "alias_adopters": 2, "canonical_adopters": 4},
        ],
    })
    assert "== topic merges proposed: 2 ==" in out
    assert "artifacts  (4 adopters)" in out
    assert out.index("<- artefacts") < out.index("<- artifacts-site")


def test_check_decision_guards():
    known = {"a", "b", "c", "gateway", "mcp-gateway"}
    assert check_decision("topic", "a", "b", {}, known) is None
    assert check_decision("bogus", "a", "b", {}, known) is not None
    assert check_decision("topic", "a", "a", {}, known) is not None
    assert "not in the topic taxonomy" in check_decision("topic", "a", "free-text", {}, known)
    assert "loop" in check_decision("topic", "a", "b", {"b": "a"}, known)
    # Loops through a legacy parent, or a longer chain, are caught too.
    assert "loop" in check_decision("topic", "mcp-gateway", "gateway",
                                    {"gateway": "mcp-gateway"}, known)
    assert "loop" in check_decision("topic", "a", "c", {"c": "b", "b": "a"}, known)
    # Re-pointing an alias that already exists is not a loop.
    assert check_decision("topic", "a", "c", {"a": "b"}, known) is None


def test_qualified_variants_of_one_subject_are_not_auto_collapsed():
    assert not tx.is_near_duplicate("checkout-api", "checkout-ui")
    assert tx.is_near_duplicate("checkout-api", "checkout")
    assert tx.norm_tokens("k8s-deploy") == tx.norm_tokens("kubernetes-deploy")
    assert tx.core_tokens("news-feed") == frozenset({"news", "feed"})


def test_weak_edges_do_not_chain():
    # A~B by adopters, B~C by stem: C must not be folded into A.
    e = lambda n, a, s=1: {"name": n, "adopters": set(a), "sessions": s, "source": "proposed"}
    out = tx.propose_merges([
        e("alpha-thing", ["p", "q", "r"], 9),
        e("beta-thing", ["p", "q", "r"], 3),
        e("beta-other", ["p", "q"], 1),
    ])
    pairs = {(r["alias"], r["canonical"]) for r in out}
    assert ("beta-thing", "alpha-thing") in pairs
    assert all(c != "alpha-thing" for a, c in pairs if a == "beta-other")


def test_free_text_names_never_become_canonical():
    out = tx.propose_merges([
        {"name": "artifacts", "adopters": {"a"}, "sessions": 1, "source": "proposed"},
        {"name": "artefacts", "adopters": {"a", "b", "c"}, "sessions": 9, "source": None},
    ])
    assert [(r["alias"], r["canonical"]) for r in out] == [("artefacts", "artifacts")]


def test_all_free_text_names_get_no_unapprovable_proposal():
    free = lambda n, a, s: {"name": n, "adopters": set(a), "sessions": s, "source": None}
    assert tx.propose_merges([
        free("artifacts", ["a"], 1), free("artefacts", ["a", "b"], 9),
    ]) == []
    assert tx.propose_merges([
        free("alpha-thing", ["p", "q", "r"], 9), free("beta-thing", ["p", "q", "r"], 3),
    ]) == []


# --- en-AU spelling wins a usage tie (Joe's rule) ---------------------------------

AU_US_PAIRS = sorted(tx._SPELLING.items()) + [
    ("optimise", "optimize"),
    ("organisation", "organization"),
    ("standardised", "standardized"),
    ("analyse", "analyze"),
    ("paralysing", "paralyzing"),
    ("favour", "favor"),
    ("honour", "honor"),
]


def _entry(name, adopters=("a", "b"), sessions=5, source="emergent"):
    return {"name": name, "source": source, "adopters": set(adopters), "sessions": sessions}


def test_au_us_pairs_are_spelling_variants():
    for au, us in AU_US_PAIRS:
        assert tx.norm_token(au) == tx.norm_token(us), (au, us)
        assert tx.en_au_score(au) > tx.en_au_score(us), (au, us)


def test_merge_canonical_is_en_au_on_a_usage_tie():
    for au, us in AU_US_PAIRS:
        out = tx.propose_merges([_entry(us), _entry(au)])
        assert [(p["alias"], p["canonical"]) for p in out] == [(us, au)], (au, us)


def test_merge_canonical_colour_beats_color_despite_alphabetical_and_length():
    assert sorted(["color", "colour"])[0] == "color"      # what a name sort would pick
    out = tx.propose_merges([_entry("color", source="bootstrap"), _entry("colour")])
    assert out[0]["canonical"] == "colour" and out[0]["alias"] == "color"
    out = tx.propose_merges([_entry("artifacts-site"), _entry("artefacts-site")])
    assert out[0]["canonical"] == "artefacts-site"


def test_usage_beats_en_au_spelling():
    out = tx.propose_merges([_entry("color", adopters=("a", "b", "c")),
                             _entry("colour", adopters=("a",))])
    assert out[0]["canonical"] == "color"
    out = tx.propose_merges([_entry("optimize", sessions=9), _entry("optimise", sessions=2)])
    assert out[0]["canonical"] == "optimize"


def test_propose_dedup_prefers_en_au_among_equal_matches():
    for au, us in AU_US_PAIRS:
        assert tx.near_duplicate(f"{us}-tool", [us, au]) == au, (au, us)
    assert tx.near_duplicate("colors-picker", ["color-picker", "colour-picker"]) == "colour-picker"


def test_client_pick_prefers_en_au_on_tie_and_usage_otherwise():
    for au, us in AU_US_PAIRS:
        entries = [{"name": f"{us}-hub", "adopters": 2}, {"name": f"{au}-hub", "adopters": 2}]
        assert cli._taxonomy_pick(f"{us}-hubs", entries)["name"] == f"{au}-hub", (au, us)
        assert cli._en_au_score(au) == tx.en_au_score(au)
        assert cli._en_au_score(us) == tx.en_au_score(us)
    entries = [{"name": "color-hub", "adopters": 5}, {"name": "colour-hub", "adopters": 1}]
    assert cli._taxonomy_pick("colors-hub", entries)["name"] == "color-hub"
