"""Unit tests for the topics registrar gate — the security-sensitive bit.

The DB-touching list/register SQL is exercised by the staging deploy smoke
test (same as the other routes); here we pin the secret-gate logic that decides
who may bypass the emergent-taxonomy promotion gate.
"""

import os

import pytest
from fastapi import HTTPException

from app.routes.topics import _require_registrar


def _clear(monkeypatch):
    monkeypatch.delenv("CLAUSTRUM_REGISTRAR_SECRET", raising=False)


def test_disabled_when_secret_unset(monkeypatch):
    _clear(monkeypatch)
    with pytest.raises(HTTPException) as e:
        _require_registrar(x_claustrum_registrar_secret="anything")
    assert e.value.status_code == 403


def test_rejects_wrong_secret(monkeypatch):
    monkeypatch.setenv("CLAUSTRUM_REGISTRAR_SECRET", "right")
    with pytest.raises(HTTPException) as e:
        _require_registrar(x_claustrum_registrar_secret="wrong")
    assert e.value.status_code == 403


def test_rejects_missing_secret_header(monkeypatch):
    monkeypatch.setenv("CLAUSTRUM_REGISTRAR_SECRET", "right")
    with pytest.raises(HTTPException) as e:
        _require_registrar(x_claustrum_registrar_secret=None)
    assert e.value.status_code == 403


def test_accepts_matching_secret(monkeypatch):
    monkeypatch.setenv("CLAUSTRUM_REGISTRAR_SECRET", "right")
    assert _require_registrar(x_claustrum_registrar_secret="right") is True


def test_app_includes_topics_routes():
    # Importing the app wires the router; assert the new paths are registered.
    from app.main import app

    paths = {r.path for r in app.routes}
    assert "/v1/topics" in paths
    assert "/v1/topics/register" in paths


def test_etag_stable_and_change_sensitive():
    from app.routes.topics import _etag

    rows_a = [("bigquery", "d", None, "bootstrap"), ("gateway", "d", "mcp-gateway", "merged")]
    rows_b = [("bigquery", "d", None, "bootstrap"), ("gateway", "d", "mcp-gateway", "merged")]
    rows_c = [("bigquery", "CHANGED", None, "bootstrap"), ("gateway", "d", "mcp-gateway", "merged")]

    assert _etag(rows_a) == _etag(rows_b)        # stable for identical input
    assert _etag(rows_a) != _etag(rows_c)        # busts on any field change
    assert _etag(rows_a).startswith('"') and _etag(rows_a).endswith('"')  # strong ETag
