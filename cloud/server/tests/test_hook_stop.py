"""Unit tests for the gated SessionEnd auto-archive in the `claustrum` CLI
client (repo-root `claustrum` script, not the server `app` package).

The bug these guard: SessionEnd fires for several reasons — a real quit, but
also /clear, a /resume switch, logout, or an incidental 'other'. The old
handler archived on ANY of them (if a PR existed), flooding the solved-archive
with look-alike "closed with PR #N" rows for work that was merely interrupted.
The handler now publishes ONLY on a deliberate quit (reason
'prompt_input_exit'), enriches the resolution with the session topic, and sets
pr_number alongside it. An absent reason (older CLI) falls through to the legacy
PR-gated behaviour so the upgrade never silently stops archiving.

No DB server, no Docker, no git, no cloud — the CLI's local sqlite goes to a
tmp dir and _cloud_update / _git_info / _get_pr_number are monkeypatched.
"""
import importlib.machinery
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

# The `claustrum` CLI is a single extensionless script at the repo root, not
# part of the `app` package, so we load it via importlib (same pattern as
# test_cli_classify.py).
_CLI_PATH = Path(__file__).resolve().parents[3] / "claustrum"


def _load_cli():
    loader = importlib.machinery.SourceFileLoader("claustrum_cli", str(_CLI_PATH))
    spec = importlib.util.spec_from_loader("claustrum_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["claustrum_cli"] = mod
    loader.exec_module(mod)
    return mod


claustrum = _load_cli()


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """Point the CLI's local DB at a tmp dir and stub out everything external.
    Returns (module, captured_cloud_calls)."""
    monkeypatch.setattr(claustrum, "DB_DIR", tmp_path)
    monkeypatch.setattr(claustrum, "DB_PATH", tmp_path / "state.db")
    calls = []
    monkeypatch.setattr(claustrum, "_cloud_update", lambda *a, **k: calls.append(k) or {})
    monkeypatch.setattr(claustrum, "_git_info", lambda target: ("joewaller/claustrum", "feat/x"))
    monkeypatch.setattr(claustrum, "_get_pr_number", lambda target, repo, branch: 999)
    return claustrum, calls


def _seed(m, uid, topic=None):
    db = m.get_db()
    now = m.time.time()
    db.execute(
        "INSERT OR REPLACE INTO sessions (uid, status, last_seen, started_at, topic) "
        "VALUES (?, 'active', ?, ?, ?)",
        (uid, now, now, topic),
    )
    db.commit()
    db.close()


def _status(m, uid):
    db = m.get_db()
    r = db.execute("SELECT status FROM sessions WHERE uid=?", (uid,)).fetchone()
    db.close()
    return r["status"] if r else None


def _run(m, monkeypatch, uid, reason=None, topic=None):
    _seed(m, uid, topic)
    payload = {"session_id": uid, "cwd": "/tmp/x"}
    if reason is not None:
        payload["reason"] = reason
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    m.hook_stop(None)


def test_deliberate_quit_publishes_enriched(cli, monkeypatch):
    m, calls = cli
    _run(m, monkeypatch, "u-quit", reason="prompt_input_exit", topic="scheduler")
    assert len(calls) == 1
    kw = calls[0]
    assert kw["resolution"] == "scheduler: closed with PR #999"
    assert kw["pr_number"] == 999
    assert kw["status"] == "done"
    assert _status(m, "u-quit") == "done"


@pytest.mark.parametrize("reason", ["clear", "resume", "logout", "other", "bypass_permissions_disabled"])
def test_non_quit_reasons_do_not_publish(cli, monkeypatch, reason):
    m, calls = cli
    _run(m, monkeypatch, f"u-{reason}", reason=reason, topic="t")
    assert calls == []
    # Local 'done' sweep still happens — the heartbeat REVIVE flips a still-live
    # session back to active, so this is harmless.
    assert _status(m, f"u-{reason}") == "done"


def test_absent_reason_falls_through_for_backcompat(cli, monkeypatch):
    m, calls = cli
    _run(m, monkeypatch, "u-noreason", reason=None, topic="memory")
    assert len(calls) == 1
    assert calls[0]["resolution"] == "memory: closed with PR #999"


def test_no_topic_gives_plain_resolution(cli, monkeypatch):
    m, calls = cli
    _run(m, monkeypatch, "u-notopic", reason="prompt_input_exit", topic=None)
    assert calls[0]["resolution"] == "closed with PR #999"


def test_no_pr_never_publishes(cli, monkeypatch):
    m, calls = cli
    monkeypatch.setattr(m, "_get_pr_number", lambda target, repo, branch: None)
    _run(m, monkeypatch, "u-nopr", reason="prompt_input_exit", topic="t")
    assert calls == []
    assert _status(m, "u-nopr") == "done"
