"""Unit tests for the CLI's classification helpers (P2.2: sub-agent directive +
transcript-fed headless backstop).

The `claustrum` CLI is a single extensionless script at the repo root, not part
of the `app` package, so we load it via importlib. These pin the pure functions;
the DB/hook/tick wiring is exercised by hook simulations + the live board.
"""

import importlib.machinery
import importlib.util
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


cli = _load_cli()


# --- _build_classify_block: short sub-agent recipe (no inline taxonomy) --------

def test_classify_block_is_a_subagent_recipe_with_brief():
    block = cli._build_classify_block("uid123")
    text = "\n".join(block)
    assert "sub-agent" in text                       # spawn a sub-agent
    assert "brief" in text                           # parent must brief it (anti-starvation)
    assert "NOT the session" in text                 # draft from work, not the slug
    assert "claustrum classify-self uid123" in text
    assert "propose-topic uid123" in text
    assert "tmux rename-session" in text             # bonus: also fixes the stale name


def test_classify_block_points_subagent_at_full_transcript():
    # With a known transcript path, the directive hands the sub-agent the WHOLE
    # chat (read the transcript) instead of a thin brief — best context.
    block = cli._build_classify_block("uid123", "/tmp/sess/uid123.jsonl")
    text = "\n".join(block)
    assert "/tmp/sess/uid123.jsonl" in text
    assert "whole transcript" in text
    assert "brief" not in text                       # transcript supersedes the brief
    assert "claustrum classify-self uid123" in text  # recipe still intact
    assert "NOT the session name" in text


def test_classify_block_does_not_inline_the_taxonomy():
    # The whole point: the ~280-token taxonomy stays OUT of the main context (the
    # sub-agent fetches it via `claustrum topics`).
    block = cli._build_classify_block("uid123")
    text = "\n".join(block)
    assert "claustrum domains" in text and "claustrum topics" in text  # it fetches them
    assert "projects: games" not in text             # but does NOT dump the list inline
    assert "data: bigquery" not in text


# --- _read_transcript_text ----------------------------------------------------

def _write_transcript(tmp_path):
    import json
    p = tmp_path / "t.jsonl"
    lines = [
        {"type": "summary", "leafUuid": "x"},                                  # skipped
        {"type": "user", "message": {"role": "user", "content": "fix the findershopping signup page"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Looking at the signup flow."},
            {"type": "tool_use", "name": "Read", "input": {}},               # dropped (not text)
        ]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "..."},                        # dropped
            {"type": "text", "text": "yes the mobile layout"},
        ]}},
    ]
    p.write_text("\n".join(json.dumps(o) for o in lines) + "\n")
    return p


def test_read_transcript_extracts_user_assistant_text(tmp_path):
    out = cli._read_transcript_text(str(_write_transcript(tmp_path)))
    assert "findershopping signup page" in out
    assert "Looking at the signup flow" in out
    assert "yes the mobile layout" in out
    assert "tool_result" not in out and "tool_use" not in out   # noise dropped


def test_read_transcript_tail_truncates(tmp_path):
    out = cli._read_transcript_text(str(_write_transcript(tmp_path)), max_chars=20)
    assert len(out) <= 20
    assert out.endswith("mobile layout")            # keeps the RECENT tail


def test_read_transcript_missing_or_garbage(tmp_path):
    assert cli._read_transcript_text(None) == ""
    assert cli._read_transcript_text("/no/such/file.jsonl") == ""
    bad = tmp_path / "b.jsonl"; bad.write_text("not json\n{also not\n")
    assert cli._read_transcript_text(str(bad)) == ""


# --- codex transcript (rollout JSONL: response_item + payload.role) -----------

def test_read_transcript_codex_rollout(tmp_path):
    import json
    p = tmp_path / "rollout-x.jsonl"
    lines = [
        {"type": "session_meta", "payload": {"cwd": "/work/proj"}},
        {"type": "response_item", "payload": {"role": "user",
            "content": [{"type": "input_text", "text": "set up the youtube mcp oauth"}]}},
        {"type": "response_item", "payload": {"role": "assistant",
            "content": [{"type": "output_text", "text": "wiring the youtube oauth flow"}]}},
        {"type": "event_msg", "payload": {"foo": "bar"}},                    # skipped
        {"type": "response_item", "payload": {"role": "tool", "content": "noise"}},  # skipped (role)
    ]
    p.write_text("\n".join(json.dumps(o) for o in lines) + "\n")
    out = cli._read_transcript_text(str(p), "codex")
    assert "youtube mcp oauth" in out and "youtube oauth flow" in out
    assert "noise" not in out and "session_meta" not in out


def test_find_codex_rollout_by_cwd(tmp_path, monkeypatch):
    import json, os
    base = tmp_path / ".codex" / "sessions" / "2026" / "06" / "28"
    base.mkdir(parents=True)
    def mk(name, cwd):
        f = base / name
        f.write_text(json.dumps({"type": "session_meta", "payload": {"cwd": cwd}}) + "\n")
        return f
    mk("rollout-a.jsonl", "/other/place")
    want = mk("rollout-b.jsonl", "/work/proj")
    monkeypatch.setenv("HOME", str(tmp_path))            # so ~/.codex resolves here
    got = cli._find_codex_rollout_by_cwd("/work/proj")
    assert got and os.path.samefile(got, str(want))
    assert cli._find_codex_rollout_by_cwd("/nope") is None


# --- antigravity transcript (locked sqlite .db, protobuf step_payload scrape) --

def test_read_transcript_antigravity_sqlite(tmp_path):
    import sqlite3
    p = tmp_path / "conv.db"
    con = sqlite3.connect(str(p))
    con.execute("CREATE TABLE steps (idx INTEGER, step_payload BLOB)")
    # Simulate protobuf-ish binary with embedded readable text + a UUID to drop.
    blob = (b"\x00\x01\x02 6c0ca224-ad6c-4cb4-a51b-02c6fcfcf03b \x10"
            b"investigate the joewaller.com server outage \x00 run_command nginx \x07")
    con.execute("INSERT INTO steps VALUES (?, ?)", (0, blob))
    con.commit(); con.close()
    out = cli._read_transcript_text(str(p), "antigravity")
    assert "investigate the joewaller.com server outage" in out
    assert "run_command nginx" in out
    assert "6c0ca224" not in out          # UUID dropped


def test_read_transcript_antigravity_locked_db_is_safe(tmp_path):
    # A missing / non-sqlite .db must degrade to '' (never raise).
    assert cli._read_transcript_text(str(tmp_path / "nope.db"), "antigravity") == ""
    junk = tmp_path / "j.db"; junk.write_text("not a database")
    assert cli._read_transcript_text(str(junk), "antigravity") == ""


# --- _classify_signal (fed a real sqlite3.Row, not a dict) --------------------

def _session_row(**cols):
    """A real sqlite3.Row over the exact columns run_classification_skill SELECTs.
    sqlite3.Row supports row["k"] but NOT row.get("k") — so any dict-ism in the
    signal builder crashes here the same way it does in production."""
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    keys = ["uid", "topic_confidence", "classify_locked", "transcript_path",
            "cwd", "label", "task", "private", "agent"]
    con.execute(f"CREATE TABLE s ({', '.join(keys)})")
    con.execute(
        f"INSERT INTO s ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})",
        [cols.get(k) for k in keys],
    )
    row = con.execute(f"SELECT {', '.join(keys)} FROM s").fetchone()
    con.close()
    return row


def test_classify_signal_leads_with_label_for_adopted_pane():
    # An adopted tmux pane has no transcript_path — the signal must still build
    # from its curated name, and must NOT crash on the sqlite3.Row (regression:
    # `row.get("agent")` raised 'sqlite3.Row' has no attribute 'get', wedging
    # every not-yet-classified session in Unclassified).
    row = _session_row(uid="tmux-x-%42", label="Fix-the-gateway-oauth-route",
                        task="", transcript_path=None, agent="claude")
    signal = cli._classify_signal(row)
    assert "SESSION NAME: Fix-the-gateway-oauth-route" in signal


def test_classify_signal_uses_agent_for_transcript_kind(tmp_path, monkeypatch):
    # The agent column drives which transcript reader is used; a codex rollout
    # must be read as codex, not defaulted to claude.
    captured = {}
    monkeypatch.setattr(cli, "_read_transcript_text",
                        lambda p, k, max_chars=0: captured.update(kind=k) or "work")
    row = _session_row(uid="s1", label="session07", task="",
                       transcript_path="/tmp/rollout.jsonl", agent="codex")
    cli._classify_signal(row)
    assert captured["kind"] == "codex"


# --- _classify_cmd_judge (match-first LLM judge: pick existing OR propose) -----

JUDGE_DOMAINS = [
    {"name": "data", "description": "analytics, BigQuery, revenue"},
    {"name": "gateway", "description": "MCP gateway proxy"},
]


def test_judge_picks_existing(monkeypatch):
    # A choice that is already a candidate is a PICK — is_new is forced False even
    # if the model flagged it, so it can never fork the taxonomy on an existing name.
    monkeypatch.setenv(
        "CLAUSTRUM_CLASSIFY_CMD",
        "python3 -c \"import json;print(json.dumps({'choice':'data','is_new':True}))\"",
    )
    out = cli._classify_cmd_judge("domain", "analyse revenue", JUDGE_DOMAINS)
    assert out["choice"] == "data"
    assert out["is_new"] is False


def test_judge_proposes_new_on_genuine_miss(monkeypatch):
    monkeypatch.setenv(
        "CLAUSTRUM_CLASSIFY_CMD",
        "python3 -c \"import json;print(json.dumps({'choice':'marketing','is_new':True,'description':'ad campaigns'}))\"",
    )
    out = cli._classify_cmd_judge("domain", "facebook ad campaign scaling", JUDGE_DOMAINS)
    assert out["choice"] == "marketing" and out["is_new"] is True
    assert out["description"] == "ad campaigns"


def test_judge_bare_name_line_is_a_pick(monkeypatch):
    # Some CLIs wrap output; a bare name line is tolerated and treated as a pick.
    monkeypatch.setenv("CLAUSTRUM_CLASSIFY_CMD", "python3 -c \"print('gateway')\"")
    out = cli._classify_cmd_judge("domain", "x", JUDGE_DOMAINS)
    assert out["choice"] == "gateway" and out["is_new"] is False


def test_judge_runs_with_private_env_recursion_guard(monkeypatch):
    # The judge subprocess must see CLAUSTRUM_PRIVATE=1 so a headless `claude -p`
    # can't spawn a phantom claustrum session. Stub picks 'data' ONLY if it does.
    monkeypatch.setenv(
        "CLAUSTRUM_CLASSIFY_CMD",
        "python3 -c \"import os,json;print(json.dumps({'choice':'data' if os.environ.get('CLAUSTRUM_PRIVATE')=='1' else 'gateway','is_new':False}))\"",
    )
    assert cli._classify_cmd_judge("domain", "x", JUDGE_DOMAINS)["choice"] == "data"


def test_judge_failures_raise_not_swallowed(monkeypatch):
    # Unlike the old best-effort backstop, the primary path must NOT swallow a
    # blank/failed judge — it raises so the skill's retry/fail-loud logic runs.
    with pytest.raises(cli.ClassifyJudgeError):
        monkeypatch.delenv("CLAUSTRUM_CLASSIFY_CMD", raising=False)
        cli._classify_cmd_judge("domain", "x", JUDGE_DOMAINS)
    monkeypatch.setenv("CLAUSTRUM_CLASSIFY_CMD", "python3 -c \"print('data')\"")
    with pytest.raises(cli.ClassifyJudgeError):
        cli._classify_cmd_judge("domain", "   ", JUDGE_DOMAINS)   # empty signal
    for stub in (
        "python3 -c \"import sys;sys.exit(1)\"",   # non-zero exit
        "python3 -c \"pass\"",                       # no output
        "definitely-not-a-real-binary-xyz",         # missing
    ):
        monkeypatch.setenv("CLAUSTRUM_CLASSIFY_CMD", stub)
        with pytest.raises(cli.ClassifyJudgeError):
            cli._classify_cmd_judge("domain", "x", JUDGE_DOMAINS)



# --- config pins: the skill is primary, fires at turn 2; directive is fallback -

def test_skill_fires_at_turn_two_above_the_floor():
    # A new session starts Unclassified (no load-time keyword guess) and the skill
    # classifies it from real transcript once it has had this many turns.
    assert cli.CLASSIFY_TRIGGER_TURN == 2
    # The skill writes a pick above the floor (so it self-terminates re-triggering)
    # but below a deliberate classify-self (80), so a deliberate user reclassify
    # always supersedes an early headless guess.
    assert cli.CLASSIFY_CONF_FLOOR < cli.CLASSIFY_SKILL_CONF < 80


def test_mirror_cloud_topic_mirrors_existing_only(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_set_local_topic",
                        lambda db, uid, topic, confidence=None, domain=None:
                        calls.append((uid, topic, domain)))
    # An existing cloud classification is mirrored straight down.
    cli._mirror_cloud_topic(None, "u1", {"topic": "mcp-gateway",
                                         "topic_confidence": 80, "domain": "gateway"})
    assert calls == [("u1", "mcp-gateway", "gateway")]
    # A session the cloud wants classified (topic_required + taxonomy) but with NO
    # topic yet is NOT minted from the on-machine signal — it stays Unclassified.
    calls.clear()
    cli._mirror_cloud_topic(None, "u2", {"topic": None, "topic_required": True,
                                         "taxonomy": [{"name": "data-warehouse",
                                                       "description": "bigquery"}]})
    assert calls == []


# --- _tick_classify_covers: the heartbeat's turn-window gate (pure) ------------

def test_tick_classify_covers_turn_window():
    claude = "3f2a1b8c-0000-4000-8000-000000000000"  # real hook uid
    pane = "tmux-host-%23"                            # adopted pane uid
    # A fresh hook-registered Claude session is LEFT ALONE before CLASSIFY_TRIGGER_TURN
    # — its own prompt hook classifies it then. It must not be classified seconds in,
    # before any prompt.
    assert cli._tick_classify_covers(0, "claude", claude) is False
    assert cli._tick_classify_covers(1, "claude", claude) is False
    # At the trigger turn (2) and beyond it's eligible — real transcript exists, and
    # the tick also covers a Claude session whose own hook somehow missed.
    assert cli._tick_classify_covers(2, "claude", claude) is True
    assert cli._tick_classify_covers(3, "claude", claude) is True
    # An adopted / non-Claude pane must not be insta-classified at turn 0 with zero
    # transcript volume (prevent premature classification on empty context).
    # Once it has accumulated dialogue volume or turns, it is covered.
    assert cli._tick_classify_covers(0, "codex", pane) is False
    assert cli._tick_classify_covers(0, None, pane) is False
    assert cli._tick_classify_covers(0, "codex", pane, transcript_lines=20) is True
    assert cli._tick_classify_covers(5, "codex", pane) is True
    # A tmux-<pane> placeholder for a CLAUDE pane is a churn stand-in during a
    # relaunch/startup gap — never classify it (the real hook session classifies
    # itself). This is what stopped a live conductor pane's placeholder from being
    # misfired to a wrong domain and briefly flipping the board.
    assert cli._tick_classify_covers(0, "claude", pane) is False
    assert cli._tick_classify_covers(5, "claude", pane) is False


def test_tick_classify_covers_volume_threshold():
    claude = "3f2a1b8c-0000-4000-8000-000000000000"  # real hook uid
    pane = "tmux-host-%23"                            # adopted pane uid

    # Turn 1 with thin transcript is not covered:
    assert cli._tick_classify_covers(1, "claude", claude, transcript_lines=5, transcript_chars=200) is False

    # Turn 1 that reaches 20 lines is immediately covered:
    assert cli._tick_classify_covers(1, "claude", claude, transcript_lines=20) is True
    assert cli._tick_classify_covers(1, "claude", claude, transcript_lines=19) is False

    # Turn 1 that reaches 1200 chars (e.g. dense single paragraph) is covered:
    assert cli._tick_classify_covers(1, "claude", claude, transcript_chars=1200) is True
    assert cli._tick_classify_covers(1, "claude", claude, transcript_chars=1199) is False

    # Turn 0 (before user prompt) with no volume is not covered:
    assert cli._tick_classify_covers(0, "claude", claude) is False

    # Placeholder for Claude is never covered even if volume was supplied:
    assert cli._tick_classify_covers(0, "claude", pane, transcript_lines=50) is False
    assert cli._tick_classify_covers(1, "claude", pane, transcript_lines=50) is False


def test_transcript_volume_and_parsing(tmp_path):
    import json

    # 1. Claude JSONL
    claude_file = tmp_path / "claude.jsonl"
    lines = [
        {"type": "user", "message": {"role": "user", "content": "Line 1\nLine 2\nLine 3"}},
        {"type": "assistant", "message": {"role": "assistant", "content": "Line 4\nLine 5"}},
    ]
    claude_file.write_text("\n".join(json.dumps(l) for l in lines))
    lcount, ccount = cli._transcript_volume(str(claude_file), "claude")
    assert lcount == 5
    assert ccount > 0

    # 2. Antigravity JSONL
    agy_file = tmp_path / "agy.jsonl"
    agy_lines = [
        {"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "Help me with Claustrum\nLine B"},
        {"type": "GENERIC", "source": "MODEL", "content": "Sure, here is the plan:\nStep 1\nStep 2"},
    ]
    agy_file.write_text("\n".join(json.dumps(l) for l in agy_lines))
    lcount, ccount = cli._transcript_volume(str(agy_file), "antigravity")
    assert lcount == 5
    assert ccount > 0

    # 3. Missing / empty file
    assert cli._transcript_volume(None) == (0, 0)
    assert cli._transcript_volume("/nonexistent/file") == (0, 0)


def test_fallback_directive_reasserts_not_fire_once():
    # When no classify CLI is available the in-session directive is the fallback;
    # it must re-assert (not fire-once) so an ignoring agent still self-classifies.
    assert cli.CLASSIFY_MAX_NUDGES > 1
    assert cli.CLASSIFY_MAX_NUDGES <= 5
    assert cli.CLASSIFY_MIN_TURN == 2


# --- _classify_skill_due: the (re-)fire gate (pure) ---------------------------

def test_classify_skill_due_gate():
    now = 10_000
    ok = dict(conf=0, attempts=0, failed=0, spawned_at=None, now=now, private=0)
    assert cli._classify_skill_due(**ok) is True
    assert cli._classify_skill_due(**{**ok, "private": 1}) is False
    assert cli._classify_skill_due(**{**ok, "conf": cli.CLASSIFY_CONF_FLOOR}) is False
    assert cli._classify_skill_due(**{**ok, "failed": 1}) is False
    assert cli._classify_skill_due(**{**ok, "attempts": cli.CLASSIFY_SKILL_ATTEMPTS}) is False
    # cooling down vs cooldown elapsed
    assert cli._classify_skill_due(**{**ok, "spawned_at": now - 1}) is False
    assert cli._classify_skill_due(
        **{**ok, "spawned_at": now - cli.CLASSIFY_SKILL_COOLDOWN - 1}) is True


# --- run_classification_skill: match-first orchestration (mocked cloud+judge) --

class _FakeDB:
    """Minimal stand-in for the local sqlite handle: the SELECT returns a preset
    session row; UPDATEs are recorded; commit/close are no-ops."""
    def __init__(self, row):
        self._row = row
        self.updates = []

    def execute(self, sql, params=()):
        recorded_row = self._row
        if not sql.strip().upper().startswith("SELECT"):
            self.updates.append((sql, params))
            recorded_row = None

        class _Cur:
            def fetchone(self_inner):
                return recorded_row

            def fetchall(self_inner):
                return [recorded_row] if recorded_row else []
        return _Cur()

    def commit(self):
        pass

    def close(self):
        pass


def _wire_skill(monkeypatch, row, judge, dom_names=("data",), topics=()):
    monkeypatch.setenv("CLAUSTRUM_CLASSIFY_CMD", "stub")
    monkeypatch.setattr(cli, "get_db", lambda: _FakeDB(row))
    monkeypatch.setattr(cli, "_classify_signal", lambda r: "SIGNAL")
    monkeypatch.setattr(cli, "_cloud_domains",
                        lambda: [{"name": n, "description": n} for n in dom_names])
    monkeypatch.setattr(cli, "_cloud_topics", lambda: list(topics))
    monkeypatch.setattr(cli, "_classify_cmd_judge",
                        lambda mode, signal, cands: judge(mode, cands))
    calls = {"propose_domain": [], "propose_topic": [], "classify_self": None}
    monkeypatch.setattr(cli, "_cloud_propose_domain",
                        lambda uid, name, desc, **k: calls["propose_domain"].append(name) or {"name": name})
    monkeypatch.setattr(cli, "_cloud_propose_topic",
                        lambda uid, name, desc, domain=None, **k: calls["propose_topic"].append((name, domain)) or {"name": name, "domain": domain})
    monkeypatch.setattr(cli, "_cloud_classify_self",
                        lambda uid, topic, **k: calls.__setitem__("classify_self", (uid, topic, k.get("domain"), k.get("confidence"))) or {})
    return calls


def _row(**kw):
    base = {"uid": "u", "topic_confidence": 0, "transcript_path": None,
            "cwd": None, "label": "L", "private": 0, "classify_locked": 0,
            "agent": "claude"}
    base.update(kw)
    return base


def test_skill_picks_existing_domain_and_topic(monkeypatch):
    calls = _wire_skill(
        monkeypatch, _row(),
        judge=lambda mode, cands: {"choice": "data" if mode == "domain" else "revenue-analysis",
                                   "is_new": False, "description": ""},
        dom_names=("data", "gateway"),
        topics=[{"name": "revenue-analysis", "domain": "data", "description": "t"}],
    )
    domain, topic = cli.run_classification_skill("u")
    assert (domain, topic) == ("data", "revenue-analysis")
    assert calls["propose_domain"] == [] and calls["propose_topic"] == []  # match-first: nothing minted
    uid, ctopic, cdomain, conf = calls["classify_self"]
    assert cdomain == "data" and conf == cli.CLASSIFY_SKILL_CONF


def test_skill_mints_new_domain_then_topic(monkeypatch):
    calls = _wire_skill(
        monkeypatch, _row(),
        judge=lambda mode, cands: ({"choice": "marketing", "is_new": True, "description": "ads"}
                                   if mode == "domain"
                                   else {"choice": "paid-social", "is_new": True, "description": "fb"}),
        dom_names=("data",),
        topics=[],
    )
    domain, topic = cli.run_classification_skill("u")
    assert domain == "marketing" and topic == "paid-social"
    assert calls["propose_domain"] == ["marketing"]
    assert calls["propose_topic"] == [("paid-social", "marketing")]  # topic scoped to the new domain


def test_skill_uses_the_mapped_name_when_propose_dedupes(monkeypatch):
    # The judge proposed 'data-analytics' but the cloud dedup mapped it onto the
    # existing 'data' — the skill must use the MAPPED canonical, not its proposal.
    monkeypatch.setenv("CLAUSTRUM_CLASSIFY_CMD", "stub")
    monkeypatch.setattr(cli, "get_db", lambda: _FakeDB(_row()))
    monkeypatch.setattr(cli, "_classify_signal", lambda r: "SIGNAL")
    monkeypatch.setattr(cli, "_cloud_domains", lambda: [{"name": "data", "description": "d"}])
    monkeypatch.setattr(cli, "_cloud_topics", lambda: [])
    monkeypatch.setattr(cli, "_classify_cmd_judge",
                        lambda mode, s, c: {"choice": "data-analytics" if mode == "domain" else "cpc",
                                            "is_new": True, "description": "x"})
    monkeypatch.setattr(cli, "_cloud_propose_domain", lambda *a, **k: {"name": "data"})  # mapped
    monkeypatch.setattr(cli, "_cloud_propose_topic", lambda uid, name, desc, domain=None, **k: {"name": name, "domain": domain})
    seen = {}
    monkeypatch.setattr(cli, "_cloud_classify_self", lambda uid, topic, **k: seen.update(domain=k.get("domain")) or {})
    domain, topic = cli.run_classification_skill("u")
    assert domain == "data"          # the mapped canonical, not 'data-analytics'
    assert seen["domain"] == "data"


def test_skill_not_ready_when_no_transcript(monkeypatch):
    monkeypatch.setenv("CLAUSTRUM_CLASSIFY_CMD", "stub")
    monkeypatch.setattr(cli, "get_db", lambda: _FakeDB(_row()))
    monkeypatch.setattr(cli, "_classify_signal", lambda r: "")   # nothing readable yet
    with pytest.raises(cli.ClassifySkillNotReady):
        cli.run_classification_skill("u")


def test_skill_skips_private_session(monkeypatch):
    monkeypatch.setenv("CLAUSTRUM_CLASSIFY_CMD", "stub")
    monkeypatch.setattr(cli, "get_db", lambda: _FakeDB(_row(private=1)))
    with pytest.raises(cli.ClassifySkillNotReady):
        cli.run_classification_skill("u")


def test_skill_noop_when_already_confident(monkeypatch):
    monkeypatch.setenv("CLAUSTRUM_CLASSIFY_CMD", "stub")
    monkeypatch.setattr(cli, "get_db", lambda: _FakeDB(_row(topic_confidence=90)))
    assert cli.run_classification_skill("u") == (None, None)


# --- _classify_signal: LABEL-FIRST so adopted (transcript-less) panes classify --

def _sigrow(**kw):
    base = {"uid": "tmux-Mac-%1", "label": "", "task": "",
            "transcript_path": None, "cwd": None, "agent": "claude"}
    base.update(kw)
    return base


def test_signal_from_label_only_when_no_transcript(monkeypatch):
    # The 85%-of-untagged case: an adopted tmux-pane with a descriptive label and
    # NO transcript. Must still produce a signal (from the name), not "" — which is
    # what left the board flooded with (untagged)/(untagged).
    monkeypatch.setattr(cli, "_find_transcript", lambda uid, cwd: (None, None))
    sig = cli._classify_signal(_sigrow(
        label="Compare-campaign-period-to-3-month-average",
        task="compare campaign vs 3mo avg"))
    assert "SESSION NAME: Compare-campaign-period-to-3-month-average" in sig
    assert "TASK: compare campaign vs 3mo avg" in sig
    assert sig.strip()  # non-empty => skill will classify, not bail "not ready"


def test_signal_skips_correlated_transcript_when_label_is_descriptive(monkeypatch):
    # A descriptive label must NOT be overridden by an unrelated same-cwd rollout
    # (the codex-by-cwd misclassification). _find_transcript should not even be
    # consulted when the label can lead.
    called = {"n": 0}
    def spy(uid, cwd):
        called["n"] += 1
        return ("/some/other.jsonl", "codex")
    monkeypatch.setattr(cli, "_find_transcript", spy)
    sig = cli._classify_signal(_sigrow(label="Headline-variants"))
    assert called["n"] == 0
    assert sig == "SESSION NAME: Headline-variants"


def test_signal_falls_back_to_correlated_transcript_when_label_is_generic(monkeypatch, tmp_path):
    # A generic 'session01' pane has no usable label — THEN a correlated transcript
    # is the only signal, so we do consult _find_transcript.
    p = _write_transcript(tmp_path)
    monkeypatch.setattr(cli, "_find_transcript", lambda uid, cwd: (str(p), "claude"))
    sig = cli._classify_signal(_sigrow(label="session01", cwd="/work"))
    assert "SESSION NAME" not in sig
    assert "findershopping" in sig  # came from the correlated transcript


def test_signal_empty_when_nothing_to_go_on(monkeypatch):
    # Generic label, no task, no transcript anywhere => "" => stays not-ready.
    monkeypatch.setattr(cli, "_find_transcript", lambda uid, cwd: (None, None))
    assert cli._classify_signal(_sigrow(label="session01")) == ""


# --- cmd_classify_skill failure bookkeeping (detached entrypoint) --------------

class _Args:
    def __init__(self, uid):
        self.uid = uid


def _raise(exc):
    def _f(uid):
        raise exc
    return _f


def test_cmd_classify_skill_marks_failed_at_cap_without_crashing(monkeypatch):
    # Regression: the `except ... as e` name is cleared once the block exits, so
    # the failure recording MUST run inside the except — else UnboundLocalError
    # kills the detached run and the session is never marked classify_failed.
    monkeypatch.setattr(cli, "run_classification_skill", _raise(cli.ClassifyJudgeError("boom")))
    fake = _FakeDB({"classify_attempts": cli.CLASSIFY_SKILL_ATTEMPTS - 1})
    monkeypatch.setattr(cli, "get_db", lambda: fake)
    cli.cmd_classify_skill(_Args("u"))   # must NOT raise
    ups = [u for u in fake.updates if "classify_failed" in u[0]]
    assert ups, "expected a classify_failed UPDATE"
    _, params = ups[-1]
    assert params[0] == cli.CLASSIFY_SKILL_ATTEMPTS   # attempts incremented to the cap
    assert params[1] == 1                             # failed flag set
    assert "boom" in params[2]                        # reason recorded


def test_cmd_classify_skill_counts_attempt_below_cap(monkeypatch):
    monkeypatch.setattr(cli, "run_classification_skill", _raise(cli.ClassifyJudgeError("blip")))
    fake = _FakeDB({"classify_attempts": 0})
    monkeypatch.setattr(cli, "get_db", lambda: fake)
    cli.cmd_classify_skill(_Args("u"))
    _, params = [u for u in fake.updates if "classify_failed" in u[0]][-1]
    assert params[0] == 1 and params[1] == 0          # attempt 1, not failed yet


def test_cmd_classify_skill_not_ready_spends_no_attempt(monkeypatch):
    monkeypatch.setattr(cli, "run_classification_skill",
                        _raise(cli.ClassifySkillNotReady("no transcript")))
    fake = _FakeDB({"classify_attempts": 0})
    monkeypatch.setattr(cli, "get_db", lambda: fake)
    cli.cmd_classify_skill(_Args("u"))   # must NOT raise, must NOT record an attempt
    assert not [u for u in fake.updates if "classify" in u[0].lower()]


# --- Context gating, agent isolation & classification locking tests ------------

def test_is_generic_label():
    assert cli._is_generic_label("")
    assert cli._is_generic_label(None)
    assert cli._is_generic_label("session01")
    assert cli._is_generic_label("session7")
    assert cli._is_generic_label("session12")
    assert cli._is_generic_label("terminal")
    assert cli._is_generic_label("terminal-1")
    assert cli._is_generic_label("Terminal 2")
    assert cli._is_generic_label("Terminal — -zsh — 114×52")
    assert cli._is_generic_label("projects")
    assert cli._is_generic_label("workspace-automation")
    assert cli._is_generic_label("zsh")
    assert cli._is_generic_label("bash")
    # Machine hostnames / nodenames must be flagged as generic
    assert cli._is_generic_label("Joe-Waller-M1-Macbook-Max")
    assert cli._is_generic_label("Joe Waller M1 Macbook Max")
    assert cli._is_generic_label(cli._host())

    # Real, curated descriptive names must NOT be flagged as generic
    assert not cli._is_generic_label("claustrum-classification-analysis")
    assert not cli._is_generic_label("fix-auth-tokens")
    assert not cli._is_generic_label("Headline-variants")
    assert not cli._is_generic_label("compare-campaign-period")


def test_find_antigravity_by_pid(monkeypatch, tmp_path):
    cid = "12345678-1234-1234-1234-123456789abc"
    brain_dir = tmp_path / ".gemini" / "antigravity-cli" / "brain" / cid / ".system_generated" / "logs"
    brain_dir.mkdir(parents=True)
    transcript = brain_dir / "transcript.jsonl"
    transcript.write_text('{"step": 1}\n')

    # Mock subprocess.run for lsof
    class FakeProc:
        returncode = 0
        stdout = f"p1234\nn/Users/user/.gemini/antigravity-cli/brain/{cid}/.system_generated/logs/transcript.jsonl\n"

    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: FakeProc())
    assert cli._find_antigravity_by_pid(1234) == cid

    # Test _find_transcript uses pid for antigravity
    monkeypatch.setattr(cli.os.path, "expanduser", lambda p: str(p).replace("~", str(tmp_path)))
    path, kind = cli._find_transcript("tmux-host-%1", agent="agy-bin", pid=1234)
    assert path == str(transcript)
    assert kind == "antigravity"


def test_classify_skill_due_respects_locked():
    now = 1000.0
    # Eligible session without lock -> True
    assert cli._classify_skill_due(
        conf=0.3, attempts=0, failed=0, spawned_at=0, now=now, private=0, locked=0
    )
    # Locked session -> False, regardless of low confidence or remaining attempts
    assert not cli._classify_skill_due(
        conf=0.3, attempts=0, failed=0, spawned_at=0, now=now, private=0, locked=1
    )


def test_find_transcript_claude_isolation(tmp_path, monkeypatch):
    # Setup a dummy codex rollout in tmp_path
    codex_rollout = tmp_path / "rollout-1.jsonl"
    codex_rollout.write_text('{"payload": {"cwd": "' + str(tmp_path) + '"}}\n')

    monkeypatch.setattr(cli, "_find_codex_rollout_by_cwd", lambda cwd, min_mtime=None: str(codex_rollout))

    # A Claude session without its own uid.jsonl must NOT adopt codex rollouts from its cwd
    tpath, tkind = cli._find_transcript("nonexistent-uid", cwd=str(tmp_path), agent="claude")
    assert tpath is None
    assert tkind is None

    # But an adopted non-Claude pane (codex) can correlate
    tpath, tkind = cli._find_transcript("tmux-Mac-%1", cwd=str(tmp_path), agent="codex")
    assert tpath == str(codex_rollout)
    assert tkind == "codex"


def test_find_codex_rollout_by_cwd_respects_min_mtime(tmp_path, monkeypatch):
    codex_dir = tmp_path / ".codex" / "sessions"
    codex_dir.mkdir(parents=True)
    rollout = codex_dir / "rollout-old.jsonl"
    rollout.write_text('{"payload": {"cwd": "' + str(tmp_path) + '"}}\n')

    # Mock home to tmp_path
    monkeypatch.setenv("HOME", str(tmp_path))

    # Setting min_mtime into the future must filter out the old rollout
    future_mtime = rollout.stat().st_mtime + 1000
    res = cli._find_codex_rollout_by_cwd(str(tmp_path), min_mtime=future_mtime)
    assert res is None

    # Setting min_mtime in the past should allow finding it
    past_mtime = rollout.stat().st_mtime - 100
    res = cli._find_codex_rollout_by_cwd(str(tmp_path), min_mtime=past_mtime)
    assert res == str(rollout)


def test_ensure_session_suppressed_when_disabled(monkeypatch):
    monkeypatch.setenv("CLAUSTRUM_DISABLED", "1")
    fake = _FakeDB({})
    # If CLAUSTRUM_DISABLED is set, _ensure_session returns immediately without touching DB
    cli._ensure_session(fake, "test-uid")
    assert len(fake.updates) == 0


def test_retire_pane_predecessors_skips_generic_label():
    db = cli.sqlite3.connect(":memory:")
    db.row_factory = cli.sqlite3.Row
    db.execute(
        "CREATE TABLE sessions ("
        "uid TEXT PRIMARY KEY, label TEXT, domain TEXT, topic TEXT, "
        "topic_confidence REAL, status TEXT, last_seen REAL, end_reason TEXT, "
        "tmux_pane TEXT, host TEXT, boot_id TEXT, classify_locked INTEGER)"
    )
    db.execute("CREATE TABLE claims (uid TEXT, path TEXT)")
    # Predecessor with classified topic
    db.execute(
        "INSERT INTO sessions (uid, label, domain, topic, topic_confidence, status, "
        "last_seen, end_reason, tmux_pane, host, boot_id, classify_locked) "
        "VALUES ('old-1', 'session01', 'engineering', 'gateway', 0.9, 'done', 100.0, 'exit', '%1', 'host1', 'boot1', 1)"
    )
    # New session with generic label 'session01'
    db.execute(
        "INSERT INTO sessions (uid, label, tmux_pane, host, boot_id, status, last_seen) "
        "VALUES ('new-1', 'session01', '%1', 'host1', 'boot1', 'active', 200.0)"
    )
    db.commit()

    # Call _retire_pane_predecessors
    cli._retire_pane_predecessors(db, 'new-1', 'host1', 'boot1', '%1', inherit_classification=True)
    db.commit()

    # Generic label session01 must NOT inherit the topic
    row = db.execute("SELECT domain, topic, classify_locked FROM sessions WHERE uid = 'new-1'").fetchone()
    assert row["domain"] is None
    assert row["topic"] is None
    assert row["classify_locked"] is None

    # Now test with a descriptive label that matches
    db.execute(
        "INSERT INTO sessions (uid, label, domain, topic, topic_confidence, status, "
        "last_seen, end_reason, tmux_pane, host, boot_id, classify_locked) "
        "VALUES ('old-desc', 'fix-auth-tokens', 'engineering', 'auth', 0.9, 'done', 300.0, 'exit', '%2', 'host1', 'boot1', 1)"
    )
    db.execute(
        "INSERT INTO sessions (uid, label, tmux_pane, host, boot_id, status, last_seen) "
        "VALUES ('new-desc', 'fix-auth-tokens', '%2', 'host1', 'boot1', 'active', 400.0)"
    )
    db.commit()

    cli._retire_pane_predecessors(db, 'new-desc', 'host1', 'boot1', '%2', inherit_classification=True)
    db.commit()

    row_desc = db.execute("SELECT domain, topic, classify_locked FROM sessions WHERE uid = 'new-desc'").fetchone()
    assert row_desc["domain"] == "engineering"
    assert row_desc["topic"] == "auth"
    assert row_desc["classify_locked"] == 1
    db.close()

