"""Unit test for the /v1/send target-required guard.

The DB-touching insert (and the from_email server-stamping) is exercised by the
staging deploy round-trip, same as the other routes. Here we pin the one piece
of pure policy: a directed message must name at least one target, else 400.
"""
import asyncio

import pytest
from fastapi import HTTPException

from app.models import SendRequest
from app.routes.send import send


def _call(req):
    # The guard raises before any DB access, so no pool is needed.
    return asyncio.run(send(req, user_email="sender@finder.com"))


def test_rejects_no_target():
    with pytest.raises(HTTPException) as e:
        _call(SendRequest(body="hello"))
    assert e.value.status_code == 400


def test_allows_to_uid_or_to_email():
    # These pass the guard, then hit the DB — which isn't available in a unit
    # test — so we only assert the guard did NOT raise a 400 first.
    for req in (SendRequest(to_uid="abc", body="hi"),
                SendRequest(to_email="nicole@finder.com", body="hi")):
        try:
            _call(req)
        except HTTPException as e:  # pragma: no cover - guard must not fire
            assert e.status_code != 400
        except Exception:
            pass  # DB/connection error past the guard is expected here
