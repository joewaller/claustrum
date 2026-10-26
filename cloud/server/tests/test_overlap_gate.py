"""The client-side pre-work overlap gate, open-PR peers and plan→intent, in the
repo-root `claustrum` CLI (not the server `app` package).

Invariants under test:
  1. Material overlap (live peer / intent / other people's open PR / recent
     solved work) blocks the triggering tool ONCE via exit 2 with a message
     telling Claude to ask the user; the retry passes and the same set never
     re-blocks in that session. A new overlapping thing blocks again.
  2. Edits only trigger on the first edit per (repo, branch); plan exit and
     branch/worktree creation trigger every time (deduped by what was shown).
  3. Fail-open: a cloud error, a gh failure or a broken lookup never blocks.
  4. CLAUSTRUM_OVERLAP_GATE=0 turns it off without any network call.
  5. Open PRs come from `gh` (cached, own PRs skipped) and match by shared
     file / directory.
  6. A plan becomes an intent titled from its heading with inferred paths —
     only when the gate lets ExitPlanMode through.

No cloud, no gh, no git — every external call is monkeypatched.
"""
import datetime as dt
import importlib.machinery
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

_CLI_PATH = Path(__file__).resolve().parents[3] / "claustrum"


def _load_cli():
    loader = importlib.machinery.SourceFileLoader("claustrum_cli_gate", str(_CLI_PATH))
    spec = importlib.util.spec_from_loader("claustrum_cli_gate", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["claustrum_cli_gate"] = mod
    loader.exec_module(mod)
    return mod


claustrum = _load_cli()

T1_PEER = {
    "uid": "kelly-1", "user_email": "kelly@finder.com", "machine": "kelly-mbp",
    "topic": "artefacts", "working_on": "artefacts publish flow",
    "overlap_files": ["artefacts/publish.py"],
}
INTENT = {
    "id": 12, "person": "pritom@finder.com", "title": "Artefacts sharing links",
    "paths": ["artefacts"], "match": "path", "shared": "artefacts", "pr_number": None,
}
PR = {
    "number": 431, "title": "feat(artefacts): share links", "author": "anto",
    "branch": "feat/share-links", "url": "https://github.com/o/r/pull/431",
    "files": ["artefacts/share.py", "README.md"],
}


@pytest.fixture
def gate(tmp_path, monkeypatch):
    """CLI with a tmp DB, a fake repo root, and controllable cloud + gh."""
    m = claustrum
    monkeypatch.setattr(m, "DB_DIR", tmp_path)
    monkeypatch.setattr(m, "DB_PATH", tmp_path / "state.db")
    monkeypatch.delenv("CLAUSTRUM_OVERLAP_GATE", raising=False)
    root = (tmp_path / "wa").resolve()
    (root / "artefacts").mkdir(parents=True)
    (root / "slack-bot").mkdir()

    state = {"list": {"tiers": {}}, "prs": [], "login": "joe", "calls": 0,
             "gh_calls": 0, "spawned": []}

    def _list(uid, **kw):
        state["calls"] += 1
        state["last_files"] = kw.get("files_touched")
        if isinstance(state["list"], Exception):
            raise state["list"]
        return state["list"]

    def _prs(root):
        state["gh_calls"] += 1
        if isinstance(state["prs"], Exception):
            raise state["prs"]
        return state["prs"], state["login"]

    monkeypatch.setattr(m, "_should_cloud_send", lambda *a, **k: True)
    monkeypatch.setattr(m, "_cloud_list_peers", _list)
    monkeypatch.setattr(m, "_open_prs", _prs)
    monkeypatch.setattr(m, "_git_info", lambda cwd: ("wa", "main"))
    monkeypatch.setattr(m, "_git_toplevel", lambda target: str(root))
    monkeypatch.setattr(m, "_spawn_plan_intent",
                        lambda uid, title, paths, repo, cwd: state["spawned"].append((title, paths, repo)))
    state["root"] = root
    return m, state


def _edit(state, path="artefacts/publish.py", uid="me-1", tool="Edit"):
    return {"session_id": uid, "tool_name": tool, "cwd": str(state["root"]),
            "tool_input": {"file_path": str(state["root"] / path)}}


def _run(m, data, capsys):
    """Run the gate; return (blocked, stderr)."""
    try:
        m._overlap_gate(data)
    except SystemExit as e:
        return e.code == 2, capsys.readouterr().err
    return False, capsys.readouterr().err


# --- blocking semantics ------------------------------------------------------

def test_blocks_once_then_retry_passes(gate, capsys):
    m, st = gate
    st["list"] = {"tiers": {"t1_file_overlap": [T1_PEER]}, "intents": [INTENT]}
    st["prs"] = [PR]

    blocked, err = _run(m, _edit(st), capsys)
    assert blocked
    assert "Claustrum overlap check (first edit on main)" in err
    assert "Live: kelly (kelly-mbp) editing artefacts/publish.py — artefacts publish flow" in err
    assert 'Planned: pritom — "Artefacts sharing links" (artefacts) [intent #12, shares artefacts/]' in err
    assert "Open PR #431 by anto" in err and "https://github.com/o/r/pull/431" in err
    assert "tell the user" in err and "ask whether to proceed" in err
    assert "CLAUSTRUM_OVERLAP_GATE=0" in err
    assert st["last_files"] == ["artefacts/publish.py"]

    # The retry (and every later edit on this branch) passes without a lookup.
    calls = st["calls"]
    assert _run(m, _edit(st), capsys) == (False, "")
    assert _run(m, _edit(st, "artefacts/other.py"), capsys) == (False, "")
    assert st["calls"] == calls


def test_same_set_never_reblocks_but_new_overlap_does(gate, capsys):
    m, st = gate
    st["list"] = {"tiers": {"t1_file_overlap": [T1_PEER]}}
    assert _run(m, _edit(st), capsys)[0]

    plan = {"session_id": "me-1", "tool_name": "ExitPlanMode", "cwd": str(st["root"]),
            "tool_input": {"plan": "# Add share links\nTouch artefacts/share.py"}}
    assert _run(m, plan, capsys) == (False, "")      # same peer: already shown

    st["list"] = {"tiers": {"t1_file_overlap": [T1_PEER]}, "intents": [INTENT]}
    blocked, err = _run(m, plan, capsys)               # new intent: blocks again
    assert blocked and "intent #12" in err


def test_no_overlap_does_not_block(gate, capsys):
    m, st = gate
    st["list"] = {"tiers": {"t4_repo": {"repo": "wa", "count": 4,
                                        "peers": [{"uid": "x", "user_email": "x@f"}]}},
                  "solved": [], "intents": []}
    assert _run(m, _edit(st), capsys) == (False, "")


def test_new_branch_rechecks_edit_scope(gate, capsys, monkeypatch):
    m, st = gate
    assert _run(m, _edit(st), capsys) == (False, "")
    n = st["calls"]
    monkeypatch.setattr(m, "_git_info", lambda cwd: ("wa", "feat/next"))
    _run(m, _edit(st), capsys)
    assert st["calls"] == n + 1


def test_bash_branch_creation_triggers_and_other_commands_do_not(gate, capsys):
    m, st = gate
    st["list"] = {"tiers": {"t2_path_or_pr": [dict(T1_PEER, reason="dir:artefacts")]}}
    for cmd in ("ls -la", "git status", "git checkout main"):
        data = {"session_id": "me-2", "tool_name": "Bash", "cwd": str(st["root"]),
                "tool_input": {"command": cmd}}
        assert _run(m, data, capsys) == (False, "")
    assert st["calls"] == 0

    for cmd, label in (("git checkout -b feat/x", "new branch feat/x"),
                       ("git switch -c feat/y", "new branch feat/y"),
                       ("git worktree add ../wt -b feat/z", "new branch feat/z")):
        data = {"session_id": f"me-{label}", "tool_name": "Bash", "cwd": str(st["root"]),
                "tool_input": {"command": cmd}}
        blocked, err = _run(m, data, capsys)
        assert blocked and f"({label})" in err and "same dir:artefacts" in err


# --- fail-open + opt-out -----------------------------------------------------

def test_fails_open_on_cloud_and_gh_errors(gate, capsys):
    m, st = gate
    st["list"] = RuntimeError("cloud down")
    st["prs"] = RuntimeError("gh exploded")
    assert _run(m, _edit(st), capsys) == (False, "")


def test_incomplete_check_is_retried_later_not_trusted_forever(gate, capsys, monkeypatch):
    m, st = gate
    st["list"] = None                       # cloud unreachable / session unknown
    assert _run(m, _edit(st), capsys) == (False, "")
    assert _run(m, _edit(st), capsys) == (False, "")
    assert st["calls"] == 1                 # not re-tried on every edit...

    monkeypatch.setattr(m, "OVERLAP_GATE_RETRY_SECONDS", -1)
    st["list"] = {"tiers": {"t1_file_overlap": [T1_PEER]}}
    assert _run(m, _edit(st), capsys)[0]    # ...but re-checked once the window lapses
    assert st["calls"] == 2
    assert _run(m, _edit(st), capsys) == (False, "")
    assert st["calls"] == 2                 # a complete check is trusted


def test_claims_from_other_repos_are_not_matched(gate, capsys):
    m, st = gate
    db = m.get_db()
    db.execute("INSERT INTO claims (filepath, uid, claimed_at) VALUES (?,?,0)",
               ("/elsewhere/src/auth/service.ts", "me-1"))
    db.execute("INSERT INTO claims (filepath, uid, claimed_at) VALUES (?,?,0)",
               (str(st["root"] / "slack-bot" / "x.py"), "me-1"))
    db.commit()
    db.close()
    _run(m, _edit(st), capsys)
    assert st["last_files"] == ["artefacts/publish.py", "slack-bot/x.py"]


def test_write_into_new_directory_resolves_its_repo(gate, capsys, monkeypatch):
    m, st = gate
    seen = []
    monkeypatch.setattr(m, "_git_info", lambda d: seen.append(d) or ("wa", "main"))
    _run(m, _edit(st, "newpkg/deep/mod.py", tool="Write"), capsys)
    assert seen == [str(st["root"])]
    assert st["last_files"] == ["newpkg/deep/mod.py"]


def test_hook_pre_tool_fails_open_when_gate_itself_breaks(gate, capsys, monkeypatch):
    m, st = gate
    monkeypatch.setattr(m, "_overlap_gate", lambda data: 1 / 0)
    monkeypatch.setattr(m, "_ensure_session", lambda db, uid, cwd=None: None)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_edit(st))))
    m.hook_pre_tool(None)   # no exception, no exit
    db = m.get_db()
    assert db.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 1


def test_env_opt_out(gate, capsys, monkeypatch):
    m, st = gate
    st["list"] = {"tiers": {"t1_file_overlap": [T1_PEER]}}
    monkeypatch.setenv("CLAUSTRUM_OVERLAP_GATE", "0")
    assert _run(m, _edit(st), capsys) == (False, "")
    assert st["calls"] == 0 and st["gh_calls"] == 0


# --- overlap item selection --------------------------------------------------

def test_overlap_items_weak_and_stale_signals_ignored():
    m = claustrum
    now = dt.datetime.now(dt.timezone.utc)
    resp = {
        "tiers": {
            "t3_topic": {"topic": "artefacts", "count": 1,
                         "peers": [{"uid": "t3", "user_email": "a@f", "topic": "artefacts"}]},
            "t4_repo": {"repo": "wa", "count": 1, "peers": [{"uid": "t4", "user_email": "b@f"}]},
            "t3b_related": [{"uid": "rel", "user_email": "c@f", "reason": "domain:tooling"}],
        },
        "solved": [
            {"uid": "s-new", "person": "k@f", "match_tier": "t2_path_or_pr",
             "resolution": "shipped", "done_at": now.isoformat()},
            {"uid": "s-old", "person": "k@f", "match_tier": "t1_file_overlap",
             "resolution": "shipped", "done_at": (now - dt.timedelta(days=200)).isoformat()},
            {"uid": "s-weak", "person": "k@f", "match_tier": "t3_topic",
             "resolution": "shipped", "done_at": now.isoformat()},
        ],
    }
    keys = [k for k, _ in m._overlap_items(resp, None)]
    assert keys == ["peer:t3", "peer:rel", "solved:s-new"]


# --- open PRs ----------------------------------------------------------------

def test_match_open_prs_skips_own_and_matches_file_or_dir():
    m = claustrum
    prs = [
        PR,
        dict(PR, number=1, author="joe"),                                 # mine (login)
        dict(PR, number=2, author="kev", branch="main"),                  # my branch
        dict(PR, number=3, author="kev", files=["slack-bot/a.py"]),      # unrelated
        dict(PR, number=4, author="kev", files=["artefacts/publish.py"]),  # exact
    ]
    out = m._match_open_prs(prs, ["artefacts/publish.py"], my_login="joe", my_branch="main")
    assert [(p["number"], p["reason"]) for p in out] == [
        (431, "shares artefacts/"), (4, "same file artefacts/publish.py")]


def test_open_prs_calls_gh_then_caches(tmp_path, monkeypatch):
    m = claustrum
    monkeypatch.setattr(m, "DB_DIR", tmp_path)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[1:3] == ["pr", "list"]:
            out = json.dumps([{"number": 7, "title": "t", "author": {"login": "anto"},
                               "headRefName": "b", "url": "u",
                               "files": [{"path": "artefacts/a.py", "additions": 1}]}])
        else:
            out = "joe\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    prs, login = m._open_prs("/repo")
    assert login == "joe"
    assert prs == [{"number": 7, "title": "t", "author": "anto", "branch": "b",
                    "url": "u", "files": ["artefacts/a.py"]}]
    assert m._open_prs("/repo") == (prs, "joe")
    # pr list + the local `gh config get` for the login, then served from cache
    assert [c[1:3] for c in calls] == [["pr", "list"], ["config", "get"]]


def test_open_prs_silent_when_gh_missing(tmp_path, monkeypatch):
    m = claustrum
    monkeypatch.setattr(m, "DB_DIR", tmp_path)
    calls = []

    def missing(cmd, **kw):
        calls.append(cmd)
        raise FileNotFoundError("gh")

    monkeypatch.setattr(m.subprocess, "run", missing)
    assert m._open_prs("/repo") == ([], None)
    assert m._open_prs("/repo") == ([], None)
    assert len(calls) == 2  # one attempt (pr list + login), then cached negative
    assert m._open_prs(None) == ([], None)


# --- plan -> intent ----------------------------------------------------------

PLAN = """# Plan: Artefacts share links

Context: see https://github.com/o/r/pull/1 and docs/artefacts.md.

1. Add `artefacts/share.py` and wire it into /ABS/slack-bot/lib/router.ts
2. Update nowhere/ghost.py (does not exist)
"""


def test_plan_title_and_paths(gate):
    m, st = gate
    plan = PLAN.replace("/ABS", str(st["root"]))
    assert m._plan_title(plan) == "Plan: Artefacts share links"
    assert m._plan_paths(plan, str(st["root"])) == ["artefacts", "slack-bot/lib"]
    assert m._plan_title("") is None
    long = m._plan_title("## " + "x" * 300)
    assert len(long) == 80 and long.endswith("…")


def test_plan_exit_registers_intent_only_when_not_blocked(gate, capsys):
    m, st = gate
    data = {"session_id": "me-3", "tool_name": "ExitPlanMode", "cwd": str(st["root"]),
            "tool_input": {"plan": PLAN.replace("/ABS", str(st["root"]))}}
    st["list"] = {"tiers": {}, "intents": [INTENT]}
    blocked, err = _run(m, data, capsys)
    assert blocked and "(leaving plan mode)" in err
    assert st["spawned"] == []
    assert st["last_files"][:2] == ["artefacts", "slack-bot/lib"]

    assert _run(m, data, capsys) == (False, "")
    assert st["spawned"] == [("Plan: Artefacts share links", ["artefacts", "slack-bot/lib"], "wa")]


def test_sensitive_or_opted_out_plans_are_not_auto_published(gate, monkeypatch):
    m, st = gate
    assert m._plan_intent_allowed("Plan: Artefacts share links")
    for title in ("Plan: redundancy comms for team X", "Fix payroll export",
                  "Respond to the auth breach", "Rotate leaked credentials",
                  "HR onboarding automation", "Patch CVE-2026-1234"):
        assert not m._plan_intent_allowed(title), title
    monkeypatch.setenv("CLAUSTRUM_PLAN_INTENTS", "0")
    assert not m._plan_intent_allowed("Plan: Artefacts share links")


def test_installer_hooks_gate_tools_and_prefilters_bash(tmp_path, monkeypatch):
    m = claustrum
    stub = tmp_path / "bin dir" / "claustrum"
    stub.parent.mkdir()
    seen = tmp_path / "seen.json"
    stub.write_text(f"#!/bin/sh\ncat > '{seen}'\nexit 2\n")
    stub.chmod(0o755)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", [str(stub), "install"])
    m.cmd_install(None)
    rules = json.loads((tmp_path / ".claude" / "settings.json").read_text())["hooks"]["PreToolUse"]
    by_matcher = {r["matcher"]: r["hooks"][0]["command"] for r in rules}
    assert set(by_matcher) == {"Edit|Write|MultiEdit|ExitPlanMode", "Bash"}

    def run_bash_hook(command):
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
        return subprocess.run(["sh", "-c", by_matcher["Bash"]], input=payload,
                              text=True, capture_output=True).returncode

    assert run_bash_hook("ls -la") == 0 and not seen.exists()
    assert run_bash_hook("git checkout -b feat/x") == 2
    assert json.loads(seen.read_text())["tool_input"]["command"] == "git checkout -b feat/x"
