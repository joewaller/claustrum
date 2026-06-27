"""Unit tests for the domains taxonomy routes.

Mirrors test_topics.py: the DB-touching list/register/propose SQL is exercised
by the staging deploy smoke test; here we pin the wiring + the ETag logic. The
registrar secret-gate is shared with topics (domains reuses
topics._require_registrar) and is already covered in test_topics.py.
"""


def test_app_includes_domain_routes():
    # Importing the app wires the router; assert the new paths are registered.
    # Read from the OpenAPI schema (stable across FastAPI versions) rather than
    # walking app.routes (whose object shape changed in newer FastAPI).
    from app.main import app

    paths = set(app.openapi()["paths"].keys())
    assert "/v1/domains" in paths
    assert "/v1/domains/register" in paths
    assert "/v1/propose_domain" in paths


def test_domains_reuse_topics_registrar_gate():
    # The domain register route must be gated by the SAME secret as topics, so a
    # one-off domain can't pollute the taxonomy without the registrar secret.
    from app.routes import domains, topics

    assert domains._require_registrar is topics._require_registrar


def test_etag_stable_and_change_sensitive():
    from app.routes.domains import _etag

    rows_a = [("data", "d", None, "bootstrap"), ("gateway", "d", None, "bootstrap")]
    rows_b = [("data", "d", None, "bootstrap"), ("gateway", "d", None, "bootstrap")]
    rows_c = [("data", "CHANGED", None, "bootstrap"), ("gateway", "d", None, "bootstrap")]

    assert _etag(rows_a) == _etag(rows_b)        # stable for identical input
    assert _etag(rows_a) != _etag(rows_c)        # busts on any field change
    assert _etag(rows_a).startswith('"') and _etag(rows_a).endswith('"')


def test_topic_entry_requires_domain():
    # topics.domain is NOT NULL in the DB; the model should make domain required
    # so a malformed (domainless) topic row can't silently serialise.
    import pytest
    from pydantic import ValidationError

    from app.models import TopicEntry

    TopicEntry(name="bigquery", description="d", source="bootstrap", domain="data")
    with pytest.raises(ValidationError):
        TopicEntry(name="bigquery", description="d", source="bootstrap")
