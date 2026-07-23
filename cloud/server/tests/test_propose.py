"""Unit tests for the propose similarity guard.

The DB-touching inline-promote SQL is exercised by the staging deploy smoke test
(same convention as the other routes); here we pin the pure dedup logic that
decides whether a proposed name is a near-duplicate of an existing canonical one
— the backstop that keeps promote-at-1 from bloating the taxonomy.
"""

from app.routes.propose import _TOPIC_BLOCKLIST, _near_duplicate, _norm_tokens


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


def test_p21_seeded_topics_are_not_near_duplicates():
    # The 5 topics minted in 0008 must not collapse into existing canonicals via
    # the propose guard (they're seeded directly, but if anyone re-proposes them
    # they should still mint). The per-platform *-mcp names are the risky ones.
    existing = [
        "product-data-mcp", "wordpress-mcp", "mcp-gateway", "memory",
        "app", "versionista", "slack", "claude", "site",
    ]
    for name in ("games", "code-review", "youtube-mcp", "meta-mcp", "tiktok-mcp"):
        assert _near_duplicate(name, existing) is None, name


def test_empty_name_never_duplicates():
    assert _near_duplicate("", ["gateway"]) is None
    assert _near_duplicate("   ", ["gateway"]) is None


def test_no_existing_names_means_no_duplicate():
    assert _near_duplicate("anything", []) is None


# --- _TOPIC_BLOCKLIST -------------------------------------------------------
# propose_topic 422s any name in this set so a junk catch-all can never enter
# the canonical taxonomy (the 'unclassified-work' sink). The endpoint compares
# the already-lowercased name against the set; these assert the set membership
# the endpoint relies on. Kept in sync with claustrum's CLASSIFY_TOPIC_BLOCKLIST.

def test_blocklist_catches_the_known_junk_names():
    for junk in ("unclassified-work", "misc", "general", "other", "tbd",
                 "uncategorised", "none", "unknown"):
        assert junk in _TOPIC_BLOCKLIST, junk


def test_blocklist_leaves_real_topics_alone():
    for real in ("mcp-gateway", "looker-studio", "hubspot-care-rewards",
                 "personal-private-work", "bigquery"):
        assert real not in _TOPIC_BLOCKLIST, real
