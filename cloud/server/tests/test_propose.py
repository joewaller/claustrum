"""Unit tests for the propose similarity guard.

The DB-touching inline-promote SQL is exercised by the staging deploy smoke test
(same convention as the other routes); here we pin the pure dedup logic that
decides whether a proposed name is a near-duplicate of an existing canonical one
— the backstop that keeps promote-at-1 from bloating the taxonomy.
"""

from app.routes.propose import _near_duplicate, _norm_tokens


# --- _norm_tokens -----------------------------------------------------------

def test_norm_tokens_splits_and_lowercases():
    assert _norm_tokens("Gateway-Deploy") == frozenset({"gateway", "deploy"})
    assert _norm_tokens("MCP_gateway") == frozenset({"mcp", "gateway"})
    assert _norm_tokens("") == frozenset()
    assert _norm_tokens(None) == frozenset()


# --- _near_duplicate --------------------------------------------------------

def test_exact_token_set_matches_ignoring_case_punct_order():
    # Same tokens, different surface form -> duplicate.
    assert _near_duplicate("Gateway Deploy", ["gateway-deploy"]) == "gateway-deploy"
    assert _near_duplicate("gateway-mcp", ["mcp-gateway"]) == "mcp-gateway"


def test_high_jaccard_is_a_duplicate():
    # 4 of 5 tokens shared -> 0.8 >= 0.7 -> duplicate.
    assert _near_duplicate(
        "one two three four", ["one two three four five"]
    ) == "one two three four five"


def test_below_threshold_is_not_a_duplicate():
    # 2 of 3 shared -> 0.667 < 0.7 -> NOT a duplicate (conservative: let it mint).
    assert _near_duplicate("data analytics pipeline", ["data analytics"]) is None
    # Entirely unrelated.
    assert _near_duplicate("secession-site", ["mcp-gateway", "bigquery"]) is None


def test_picks_first_matching_existing():
    out = _near_duplicate("gateway deploy", ["bigquery", "gateway-deploy", "slack"])
    assert out == "gateway-deploy"


def test_empty_name_never_duplicates():
    assert _near_duplicate("", ["gateway"]) is None
    assert _near_duplicate("   ", ["gateway"]) is None


def test_no_existing_names_means_no_duplicate():
    assert _near_duplicate("anything", []) is None
