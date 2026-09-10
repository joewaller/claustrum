import importlib.machinery
import importlib.util
import io
import json
import sys
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
    monkeypatch.setattr(claustrum, "DB_DIR", tmp_path)
    monkeypatch.setattr(claustrum, "DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(claustrum, "_cloud_checkin", lambda *a, **k: None)
    monkeypatch.setattr(claustrum, "_cloud_update", lambda *a, **k: {})
    monkeypatch.setattr(claustrum, "_git_info", lambda target: ("joewaller/claustrum", "feat/x"))
    monkeypatch.setattr(claustrum, "_get_pr_number", lambda target, repo, branch: None)
    monkeypatch.setattr(claustrum, "_spawn_classify_skill", lambda uid: None)
    monkeypatch.setattr(claustrum, "_host", lambda: "testhost")
    monkeypatch.setattr(claustrum, "_boot_id", lambda: "boot123")
    monkeypatch.setenv("TMUX_PANE", "%42")
    return claustrum


def _run_start(m, monkeypatch, session_id, source=None, cwd="/tmp/x"):
    payload = {"session_id": session_id, "cwd": cwd}
    if source is not None:
        payload["source"] = source
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    m.hook_start([])


def _run_stop(m, monkeypatch, session_id, reason=None, cwd="/tmp/x"):
    payload = {"session_id": session_id, "cwd": cwd}
    if reason is not None:
        payload["reason"] = reason
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    m.hook_stop([])


def test_clear_skips_topic_inheritance_on_start(cli, monkeypatch):
    m = cli
    db = m.get_db()
    now = m.time.time()
    db.execute(
        """
        INSERT INTO sessions (uid, label, status, last_seen, started_at, host, tmux_pane, boot_id,
                             topic, domain, topic_confidence, end_reason)
        VALUES ('u-old', 'claustrum', 'done', ?, ?, 'testhost', '%42', 'boot123',
                'old-topic', 'tooling', 85, 'clear')
        """,
        (now, now),
    )
    db.commit()
    db.close()

    monkeypatch.setattr(m, "_get_session_label", lambda: "claustrum")
    _run_start(m, monkeypatch, "u-new", source="clear")

    db = m.get_db()
    new_row = db.execute(
        "SELECT topic, domain, topic_confidence, turn_count, status FROM sessions WHERE uid = 'u-new'"
    ).fetchone()
    old_row = db.execute("SELECT status FROM sessions WHERE uid = 'u-old'").fetchone()
    db.close()

    assert new_row["topic"] is None
    assert new_row["domain"] is None
    assert new_row["topic_confidence"] is None
    assert new_row["turn_count"] == 0
    assert new_row["status"] == "active"
    assert old_row["status"] == "superseded"


def test_startup_inherits_topic_from_normal_predecessor(cli, monkeypatch):
    m = cli
    db = m.get_db()
    now = m.time.time()
    db.execute(
        """
        INSERT INTO sessions (uid, label, status, last_seen, started_at, host, tmux_pane, boot_id,
                             topic, domain, topic_confidence, end_reason)
        VALUES ('u-old', 'claustrum', 'done', ?, ?, 'testhost', '%42', 'boot123',
                'inherited-topic', 'engineering', 85, 'prompt_input_exit')
        """,
        (now, now),
    )
    db.commit()
    db.close()

    monkeypatch.setattr(m, "_get_session_label", lambda: "claustrum")
    _run_start(m, monkeypatch, "u-new", source="startup")

    db = m.get_db()
    new_row = db.execute(
        "SELECT topic, domain, topic_confidence, status FROM sessions WHERE uid = 'u-new'"
    ).fetchone()
    old_row = db.execute("SELECT status FROM sessions WHERE uid = 'u-old'").fetchone()
    db.close()

    assert new_row["topic"] == "inherited-topic"
    assert new_row["domain"] == "engineering"
    assert new_row["topic_confidence"] == 85
    assert old_row["status"] == "superseded"


def test_startup_does_not_inherit_from_cleared_predecessor(cli, monkeypatch):
    m = cli
    db = m.get_db()
    now = m.time.time()
    db.execute(
        """
        INSERT INTO sessions (uid, label, status, last_seen, started_at, host, tmux_pane, boot_id,
                             topic, domain, topic_confidence, end_reason)
        VALUES ('u-cleared', 'claustrum', 'done', ?, ?, 'testhost', '%42', 'boot123',
                'cleared-topic', 'tooling', 90, 'clear')
        """,
        (now, now),
    )
    db.commit()
    db.close()

    monkeypatch.setattr(m, "_get_session_label", lambda: "claustrum")
    _run_start(m, monkeypatch, "u-new", source="startup")

    db = m.get_db()
    new_row = db.execute(
        "SELECT topic, domain, topic_confidence FROM sessions WHERE uid = 'u-new'"
    ).fetchone()
    db.close()

    assert new_row["topic"] is None
    assert new_row["domain"] is None
    assert new_row["topic_confidence"] is None


def test_reused_uid_on_clear_resets_turn_and_classification(cli, monkeypatch):
    m = cli
    db = m.get_db()
    now = m.time.time()
    db.execute(
        """
        INSERT INTO sessions (uid, label, status, last_seen, started_at, host, tmux_pane, boot_id,
                             topic, domain, topic_confidence, turn_count, classify_locked,
                             classify_attempts, classify_failed)
        VALUES ('u-same', 'claustrum', 'active', ?, ?, 'testhost', '%42', 'boot123',
                'old-topic', 'tooling', 80, 15, 1, 1, 0)
        """,
        (now, now),
    )
    db.commit()
    db.close()

    monkeypatch.setattr(m, "_get_session_label", lambda: "claustrum")
    _run_start(m, monkeypatch, "u-same", source="clear")

    db = m.get_db()
    row = db.execute(
        """
        SELECT topic, domain, topic_confidence, turn_count, end_reason, classify_locked,
               classify_attempts, classify_failed
        FROM sessions WHERE uid = 'u-same'
        """
    ).fetchone()
    db.close()

    assert row["topic"] is None
    assert row["domain"] is None
    assert row["topic_confidence"] is None
    assert row["turn_count"] == 0
    assert row["end_reason"] is None
    assert row["classify_locked"] == 0
    assert row["classify_attempts"] == 0
    assert row["classify_failed"] == 0


def test_clear_tombstone_stops_reachback(cli, monkeypatch):
    m = cli
    db = m.get_db()
    now = m.time.time()
    # Older session with topic
    db.execute(
        """
        INSERT INTO sessions (uid, label, status, last_seen, started_at, host, tmux_pane, boot_id,
                             topic, domain, topic_confidence, end_reason)
        VALUES ('u-old', 'claustrum', 'done', ?, ?, 'testhost', '%42', 'boot123',
                'pre-clear-topic', 'tooling', 80, 'prompt_input_exit')
        """,
        (now - 100, now - 100),
    )
    # Newer session that was cleared
    db.execute(
        """
        INSERT INTO sessions (uid, label, status, last_seen, started_at, host, tmux_pane, boot_id,
                             topic, domain, topic_confidence, end_reason)
        VALUES ('u-cleared', 'claustrum', 'done', ?, ?, 'testhost', '%42', 'boot123',
                NULL, NULL, NULL, 'clear')
        """,
        (now - 10, now - 10),
    )
    db.commit()
    db.close()

    monkeypatch.setattr(m, "_get_session_label", lambda: "claustrum")
    _run_start(m, monkeypatch, "u-new", source="startup")

    db = m.get_db()
    new_row = db.execute(
        "SELECT topic, domain, topic_confidence FROM sessions WHERE uid = 'u-new'"
    ).fetchone()
    db.close()

    # The cleared predecessor must act as a hard stop and not reach back to u-old
    assert new_row["topic"] is None
    assert new_row["domain"] is None
    assert new_row["topic_confidence"] is None


def test_hook_stop_records_end_reason(cli, monkeypatch):
    m = cli
    db = m.get_db()
    now = m.time.time()
    db.execute(
        "INSERT INTO sessions (uid, status, last_seen, started_at) VALUES ('u-stop', 'active', ?, ?)",
        (now, now),
    )
    db.commit()
    db.close()

    _run_stop(m, monkeypatch, "u-stop", reason="clear")

    db = m.get_db()
    row = db.execute("SELECT status, end_reason FROM sessions WHERE uid = 'u-stop'").fetchone()
    db.close()

    assert row["status"] == "done"
    assert row["end_reason"] == "clear"
