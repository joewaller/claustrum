"""Unit tests for the NON-BLOCKING prompt hook in the `claustrum` CLI client
(repo-root `claustrum` script, not the server `app` package).

The bug these guard: the UserPromptSubmit hook (`claustrum hook prompt`) used to
make ~4 serial cloud round-trips inline (checkin, list-peers, classify-self,
inbox-drain), each re-minting an IAP token. Under concurrent load those raced
the hardcoded 5s hook ceiling and tipped over — Claude Code discarded the hook
output, sessions went stale, and the board rotted. The hook now does LOCAL work
only and spawns a detached `cloud-sync` worker; it renders cross-machine state
from that worker's last result (the cloud cache).

Invariants under test:
  1. `hook_prompt` performs ZERO cloud I/O (all of it moved to `cmd_cloud_sync`).
  2. `hook_prompt` always spawns exactly one detached cloud-sync.
  3. `cmd_cloud_sync` writes the cache the hook renders from.
  4. A sync's cross-machine inbox is shown exactly once (tracked by synced_at
     vs inbox_shown_at), not re-shown every turn.
  5. A stale / never-synced cache suppresses cross-machine output ("cloud
     unknown"), it does not render hours-old data.
  6. cmd_cloud_sync is single-flight — a second concurrent run no-ops.

No DB server, no Docker, no git, no cloud — the CLI's local sqlite goes to a
tmp dir and every _cloud_* call is monkeypatched.
"""
import importlib.machinery
import importlib.util
import io
import json
import sys
import fcntl
import types
from pathlib import Path

import pytest

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
    Returns (module, cloud_calls_counter). The cloud is 'reachable' and returns
    a fixed roster + one inbox message; local subprocess lookups (tmux/git) are
    neutralised so timings reflect pure hook logic."""
    monkeypatch.setattr(claustrum, "DB_DIR", tmp_path)
    monkeypatch.setattr(claustrum, "DB_PATH", tmp_path / "state.db")
    calls = {"checkin": 0, "list": 0, "inbox": 0, "classify": 0}

    def _checkin(u, task=None, cwd=None, label=None, **k):
        calls["checkin"] += 1
        return {"topic": "claustrum-hooks", "topic_confidence": 90, "domain": "tooling"}

    def _list(u, **k):
        calls["list"] += 1
        return {
            "tiers": {"t3_topic": {"topic": "claustrum-hooks", "count": 2,
                                   "peers": [{"user_email": "nicole@finder.com"}]}},
            "solved": [],
        }

    def _inbox(u):
        calls["inbox"] += 1
        return [{"id": "m1", "type": "info", "from_uid": "peer-9",
                 "body": "heads up from another machine"}]

    monkeypatch.setattr(claustrum, "_should_cloud_send", lambda *a, **k: True)
    monkeypatch.setattr(claustrum, "_cloud_checkin", _checkin)
    monkeypatch.setattr(claustrum, "_cloud_list_peers", _list)
    monkeypatch.setattr(claustrum, "_cloud_inbox_drain", _inbox)
    monkeypatch.setattr(claustrum, "_cloud_classify_self",
                        lambda u, t, **k: calls.__setitem__("classify", calls["classify"] + 1) or {})
    # Local liveness lookups shell out to tmux/git — irrelevant here, and slow /
    # flaky in CI. Stub to keep the hook pure-logic.
    monkeypatch.setattr(claustrum, "_reap_dead", lambda db, now: [])
    monkeypatch.setattr(claustrum, "_get_session_label", lambda: "fix-claustrum-hook-timeout")
    return claustrum, calls


def _run_prompt(m, monkeypatch, uid, prompt="do the thing", cwd="/tmp/x"):
    """Run hook_prompt with a captured stdout and a stubbed detached spawn.
    Returns (stdout, spawn_count)."""
    payload = {"session_id": uid, "cwd": cwd, "prompt": prompt, "transcript_path": None}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    spawned = {"n": 0}
    monkeypatch.setattr(m, "_spawn_cloud_sync",
                        lambda u, c: spawned.__setitem__("n", spawned["n"] + 1))
    try:
        m.hook_prompt(None)
    finally:
        out = buf.getvalue()
    return out, spawned["n"]


def test_hook_does_no_cloud_io_and_spawns_sync(cli, monkeypatch):
    m, calls = cli
    out, spawned = _run_prompt(m, monkeypatch, "u-1")
    # The whole point: nothing cloud-bound runs inside the hook.
    assert calls == {"checkin": 0, "list": 0, "inbox": 0, "classify": 0}
    # ...and it always kicks off exactly one detached sync for next turn.
    assert spawned == 1
    # Cold cache → no cross-machine block yet.
    assert "Cross-machine" not in out


def test_sync_writes_cache_then_hook_renders_it(cli, monkeypatch):
    m, calls = cli
    _run_prompt(m, monkeypatch, "u-2")            # turn 1 (cold)
    m.cmd_cloud_sync(types.SimpleNamespace(uid="u-2", cwd="/tmp/x"))
    assert calls["checkin"] == calls["list"] == calls["inbox"] == 1
    cache = m._read_cloud_cache("u-2")
    assert cache.get("synced_at") and cache.get("tiers")

    out, _ = _run_prompt(m, monkeypatch, "u-2")   # turn 2 (warm)
    assert "Cross-machine" in out
    assert "claustrum-hooks" in out
    assert "heads up from another machine" in out


def test_inbox_shown_exactly_once(cli, monkeypatch):
    m, _ = cli
    _run_prompt(m, monkeypatch, "u-3")
    m.cmd_cloud_sync(types.SimpleNamespace(uid="u-3", cwd="/tmp/x"))
    out2, _ = _run_prompt(m, monkeypatch, "u-3")
    assert "heads up from another machine" in out2
    # Same cache, no new sync → the inbox message must NOT repeat, but the
    # (still-current) cross-machine roster should still show.
    out3, _ = _run_prompt(m, monkeypatch, "u-3")
    assert "heads up from another machine" not in out3
    assert "Cross-machine" in out3


def test_stale_cache_suppresses_cross_machine(cli, monkeypatch):
    m, _ = cli
    _run_prompt(m, monkeypatch, "u-4")
    m.cmd_cloud_sync(types.SimpleNamespace(uid="u-4", cwd="/tmp/x"))
    # Age the cache well past the freshness window.
    cache = m._read_cloud_cache("u-4")
    cache["synced_at"] = m.time.time() - (m.CLOUD_CACHE_FRESH + 60)
    m._write_cloud_cache("u-4", cache)
    out, _ = _run_prompt(m, monkeypatch, "u-4")
    assert "Cross-machine" not in out
    assert "heads up from another machine" not in out


def test_cloud_sync_single_flight(cli, monkeypatch):
    m, calls = cli
    _run_prompt(m, monkeypatch, "u-5")
    # Hold the per-uid lock, then attempt a sync — it must skip (no cloud calls).
    lock_path = m._cloud_cache_path("u-5").with_name("u-5.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    held = open(lock_path, "w")
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        before = dict(calls)
        m.cmd_cloud_sync(types.SimpleNamespace(uid="u-5", cwd="/tmp/x"))
        assert calls == before
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()


def test_sync_skips_cache_write_when_cloud_unreachable(cli, monkeypatch):
    m, _ = cli
    monkeypatch.setattr(m, "_cloud_checkin", lambda *a, **k: None)  # cloud down
    m.cmd_cloud_sync(types.SimpleNamespace(uid="u-6", cwd="/tmp/x"))
    # No successful checkin → no cache written → hook treats cloud as unknown.
    assert m._read_cloud_cache("u-6") == {}
