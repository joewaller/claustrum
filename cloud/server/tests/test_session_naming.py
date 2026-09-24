"""Session naming: the placeholder slug from the first prompt, and the one-shot
LLM refine that replaces it once the transcript reflects the real work."""

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest

_CLI_PATH = Path(__file__).resolve().parents[3] / "claustrum"


def _load_cli():
    loader = importlib.machinery.SourceFileLoader("claustrum_naming", str(_CLI_PATH))
    spec = importlib.util.spec_from_loader("claustrum_naming", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["claustrum_naming"] = mod
    loader.exec_module(mod)
    return mod


cli = _load_cli()


# --- _slug_from_task ---------------------------------------------------------

@pytest.mark.parametrize("prompt, expected", [
    ("fix the auth bug in gateway oauth route", "fix-auth-bug-gateway"),
    ("Yo. I still think we have problems naming sessions.", "naming-sessions"),
    ('<pasted_content id="16a6">\n  Context: I\'m iterating on the mobile view of '
     "the wa Conductor UI (the phone experience at https://wa.finder.com)",
     "mobile-view-wa-conductor"),
    ("[Pasted text #1 +40 lines] migrate billing cron to cloud run", "migrate-billing-cron-cloud"),
    ("session sessions session naming", "session-naming"),
])
def test_slug_skips_filler_and_wrappers(prompt, expected):
    assert cli._slug_from_task(prompt) == expected


@pytest.mark.parametrize("prompt", [
    "<task-notification>\n<task-id>a69aab1b886b8a828</task-id>",
    "<system-reminder>something</system-reminder>",
    "hey",
    "yo, still there?",
    "",
    None,
])
def test_slug_none_when_nothing_meaningful(prompt):
    assert cli._slug_from_task(prompt) is None


def test_harness_prompt_detection():
    assert cli._is_harness_prompt("<task-notification>\n<task-id>x</task-id>")
    assert not cli._is_harness_prompt('<pasted_content id="1">real work</pasted_content>')
    assert not cli._is_harness_prompt("fix the build")


@pytest.mark.parametrize("name, ok", [
    ("conductor-mobile-ui", True),
    ("session-naming", True),
    ("naming", False),                      # one word
    ("a-b-c-d-e-f", False),                 # too many words
    ("Conductor Mobile", False),            # not kebab
    ("session02", False),                   # generic / one word
    ("x" * 30 + "-" + "y" * 15, False),     # too long
])
def test_valid_session_name(name, ok):
    assert cli._valid_session_name(name) is ok


# --- _refine_auto_name -------------------------------------------------------

class _Proc:
    def __init__(self, stdout="", returncode=0):
        self.stdout, self.returncode, self.stderr = stdout, returncode, ""


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "DB_DIR", tmp_path)
    monkeypatch.setattr(cli, "DB_PATH", tmp_path / "state.db")
    monkeypatch.setenv("CLAUSTRUM_CLASSIFY_CMD", "fake-judge")
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    db = cli.get_db()
    db.execute(
        "INSERT INTO sessions (uid, label, auto_label, tmux_pane, task, transcript_path, status, "
        "last_seen, started_at) VALUES ('u1', 'yo-still-think-have', 'yo-still-think-have', "
        "'%9', 'Yo. I still think we have problems naming sessions', ?, 'active', 0, 0)",
        (str(transcript),),
    )
    db.commit()
    db.close()

    state = {"live": "yo-still-think-have", "renamed_to": None, "judge": "claustrum-session-naming"}

    def fake_run(cmd, **_):
        if cmd[:2] == ["tmux", "display-message"]:
            return _Proc(state["live"] + "\n")
        if cmd[:2] == ["tmux", "rename-session"]:
            state["renamed_to"] = cmd[-1]
            return _Proc()
        raise AssertionError(cmd)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli, "_judge_with_retries",
                        lambda mode, signal, cands: {"choice": state["judge"], "is_new": True,
                                                     "description": ""})
    return state


def _row():
    db = cli.get_db()
    try:
        return db.execute("SELECT label, auto_label FROM sessions WHERE uid = 'u1'").fetchone()
    finally:
        db.close()


def test_refine_replaces_untouched_placeholder(env):
    assert cli._refine_auto_name("u1") == "claustrum-session-naming"
    assert env["renamed_to"] == "claustrum-session-naming"
    row = _row()
    assert row["label"] == "claustrum-session-naming"
    assert row["auto_label"] is None


def test_refine_keeps_a_name_chosen_since(env):
    env["live"] = "hand-picked-name"
    assert cli._refine_auto_name("u1") is None
    assert env["renamed_to"] is None
    assert _row()["auto_label"] is None      # never fires again


def test_refine_rejects_invalid_judge_output(env):
    env["judge"] = "naming"
    assert cli._refine_auto_name("u1") is None
    assert env["renamed_to"] is None
    assert _row()["auto_label"] == "yo-still-think-have"   # retried next run


def test_placeholder_does_not_lead_classify_signal(env):
    db = cli.get_db()
    row = db.execute("SELECT * FROM sessions WHERE uid = 'u1'").fetchone()
    db.close()
    assert "SESSION NAME" not in cli._classify_signal(row)


def test_refine_failure_does_not_spend_a_classify_attempt(env, monkeypatch):
    monkeypatch.setattr(cli, "run_classification_skill", lambda uid: ("eng", "naming"))

    def boom(uid):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(cli, "_refine_auto_name", boom)
    cli.cmd_classify_skill(type("A", (), {"uid": "u1"})())
    db = cli.get_db()
    try:
        r = db.execute("SELECT classify_attempts, classify_failed FROM sessions "
                       "WHERE uid = 'u1'").fetchone()
    finally:
        db.close()
    assert not r["classify_attempts"] and not r["classify_failed"]
