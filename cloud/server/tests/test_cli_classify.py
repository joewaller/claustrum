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

TAXONOMY = [
    {"name": "games", "description": "g", "domain": "projects"},
    {"name": "app", "description": "a", "domain": "engineering"},
    {"name": "bigquery", "description": "b", "domain": "data"},
]


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


# --- _floor_classify: heuristic ONLY (the LLM runs in the detached skill) ------

def test_floor_is_heuristic_even_when_cmd_set(monkeypatch):
    # CLAUSTRUM_CLASSIFY_CMD must NOT fire on the per-turn floor (no LLM per turn).
    monkeypatch.setenv("CLAUSTRUM_CLASSIFY_CMD", "python3 -c \"print('games')\"")
    topic, conf = cli._floor_classify(TAXONOMY, "bigquery dataset work")
    assert topic == "bigquery"          # keyword heuristic, not the cmd's 'games'
    assert conf and conf < cli.CLASSIFY_CONF_FLOOR


# --- regression: weak/tied description-word match must NOT pick a specific topic
# (the 'server down -> youtube-mcp' bug: one common word, ties broken by name) ---

def test_weak_tied_match_stays_untagged_no_app_default():
    tax = [
        {"name": "youtube-mcp", "domain": "gateway", "description": "YouTube MCP server integration."},
        {"name": "meta-mcp", "domain": "gateway", "description": "Meta MCP server integration."},
        {"name": "app", "domain": "engineering", "description": "Application-level work."},
    ]
    # 'server' hits both *-mcp descriptions (tied @1). A tie is NOT committed (the
    # reverse-name tiebreak picks an arbitrary topic) AND we no longer fall back to
    # the generic 'app' bucket — both paths return None so the backstop classifies.
    assert cli._auto_classify_topic(tax, "investigate server down", floor=True) == (None, None)
    assert cli._auto_classify_topic(tax, "investigate server down", floor=False) == (None, None)


def test_no_overlap_never_defaults_to_app():
    tax = [
        {"name": "bigquery", "domain": "data", "description": "BigQuery datasets and SQL."},
        {"name": "app", "domain": "engineering", "description": "Application-level work."},
    ]
    # Zero token overlap: the old floor bucketed this into 'app' (the terrible
    # default). It must now stay untagged on both paths.
    assert cli._auto_classify_topic(tax, "refresh the secession texture loader", floor=True) == (None, None)
    assert cli._auto_classify_topic(tax, "refresh the secession texture loader", floor=False) == (None, None)


def test_unique_weak_leader_commits_on_floor_only():
    tax = [
        {"name": "figma", "domain": "gateway", "description": "Figma design files."},
        {"name": "app", "domain": "engineering", "description": "Application-level work."},
    ]
    # A single UNIQUE description-word hit ('design', score 1) is weak but grounded
    # in a real token the session contains: the floor commits it at low confidence
    # (backstop still supersedes), while the prompt path defers to the sub-agent.
    topic, conf = cli._auto_classify_topic(tax, "update the design system", floor=True)
    assert topic == "figma" and 0 < conf < cli.CLASSIFY_CONF_FLOOR
    assert cli._auto_classify_topic(tax, "update the design system", floor=False) == (None, None)


def test_name_hit_still_classifies_confidently():
    tax = [
        {"name": "youtube-mcp", "domain": "gateway", "description": "YouTube MCP server integration."},
        {"name": "app", "domain": "engineering", "description": "Application-level work."},
    ]
    topic, conf = cli._auto_classify_topic(tax, "add accounts to the youtube-mcp", floor=True)
    assert topic == "youtube-mcp" and conf >= 30


# --- variant -> canonical collapse (board/collision convergence) --------------

VARIANT_TAX = [
    {"name": "mcp-gateway", "domain": "gateway", "description": "MCP gateway proxy and routes."},
    {"name": "gateway", "domain": "gateway", "description": "load balancer gateway.", "parent": "mcp-gateway"},
    {"name": "wordpress", "domain": "gateway", "description": "WordPress content."},
    {"name": "wp", "domain": "gateway", "description": "variant.", "parent": "wordpress"},
]


def test_canonical_topic_resolves_variant_to_parent():
    assert cli._canonical_topic(VARIANT_TAX, "gateway") == "mcp-gateway"
    assert cli._canonical_topic(VARIANT_TAX, "mcp-gateway") == "mcp-gateway"  # canonical -> itself
    assert cli._canonical_topic(VARIANT_TAX, "brand-new") == "brand-new"      # emergent -> itself
    assert cli._canonical_topic(VARIANT_TAX, "") == ""
    assert cli._canonical_topic([], "gateway") == "gateway"                   # no taxonomy -> itself


def test_auto_classify_collapses_variant_pick():
    # signal hits the variant 'gateway' name + desc, which outscores the canonical
    # 'mcp-gateway' — but the returned topic must be the canonical, not the variant.
    topic, conf = cli._auto_classify_topic(VARIANT_TAX, "check the gateway load balancer", floor=True)
    assert topic == "mcp-gateway"


# (Variant->canonical collapse for the skill's picks now happens server-side in
# the cloud propose_* dedup guard, not client-side — see cloud tests test_dedup.)


# --- _build_drift_block: re-verify fit (drift OR misclassification) -----------

def test_drift_block_asks_about_fit_and_misclassification():
    block = cli._build_drift_block("uid9", "app", "engineering", ["main.py", "README.md"])
    text = "\n".join(block)
    assert 'topic="app"' in text and 'domain="engineering"' in text
    assert "main.py, README.md" in text
    assert "misclassified" in text          # not just drift
    assert "classify-self uid9" in text


def test_drift_block_handles_no_files_and_no_domain():
    text = "\n".join(cli._build_drift_block("uid9", "app", None, []))
    assert 'domain="?"' in text
    assert "Recent files" not in text


# --- config pins: the skill is primary, fires at turn 3; directive is fallback -

def test_skill_fires_at_turn_three_above_the_floor():
    assert cli.CLASSIFY_TRIGGER_TURN == 3
    # The skill writes a confident pick that self-terminates re-triggering.
    assert cli.CLASSIFY_SKILL_CONF > cli.CLASSIFY_CONF_FLOOR


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
            "cwd": None, "label": "L", "private": 0}
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
