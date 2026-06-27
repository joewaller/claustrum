"""Unit tests for the CLI's classification helpers (P2.1).

The `claustrum` CLI is a single extensionless script at the repo root, not part
of the `app` package, so we load it via importlib. These pin the pure functions
that decide the one-step directive shape and the pluggable floor — the DB/hook
wiring around them is exercised by the staging deploy + hook simulations.
"""

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest

_CLI_PATH = Path(__file__).resolve().parents[3] / "claustrum"


def _load_cli():
    # The CLI is an extensionless script, so spec_from_file_location can't infer
    # a loader — point a SourceFileLoader at it explicitly.
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
    {"name": "e2e", "description": "e", "domain": "engineering"},
    {"name": "bigquery", "description": "b", "domain": "data"},
]


# --- _format_compact_taxonomy ----------------------------------------------

def test_compact_taxonomy_groups_by_domain_names_only():
    lines = cli._format_compact_taxonomy(TAXONOMY)
    # One line per domain, sorted; names sorted within; no descriptions present.
    assert lines == [
        "    data: bigquery",
        "    engineering: app, e2e",
        "    projects: games",
    ]


def test_compact_taxonomy_defaults_missing_domain_to_general():
    lines = cli._format_compact_taxonomy([{"name": "loose"}])
    assert lines == ["    general: loose"]


def test_compact_taxonomy_empty():
    assert cli._format_compact_taxonomy([]) == []


# --- _build_classify_block -------------------------------------------------

def test_classify_block_is_one_step_with_taxonomy():
    block = cli._build_classify_block("uid123", TAXONOMY)
    text = "\n".join(block)
    assert "ONE command" in text
    assert "claustrum classify-self uid123" in text
    assert "    data: bigquery" in text  # inline taxonomy present


def test_classify_block_falls_back_without_taxonomy():
    block = cli._build_classify_block("uid123", [])
    text = "\n".join(block)
    assert block  # never empty
    assert "propose-topic uid123" in text
    assert "classify-self uid123" in text


def test_classify_block_renudge_is_terse_no_taxonomy_dump():
    # Re-nudges (first=False) must NOT re-dump the taxonomy — token control.
    block = cli._build_classify_block("uid123", TAXONOMY, first=False)
    text = "\n".join(block)
    assert "classify-self uid123" in text
    assert "projects: games" not in text   # no inline taxonomy
    assert "data: bigquery" not in text
    assert len(block) <= 3                  # genuinely terse


# --- _classify_cmd_topic (pluggable floor) ---------------------------------

def test_cmd_floor_unset_returns_none(monkeypatch):
    monkeypatch.delenv("CLAUSTRUM_CLASSIFY_CMD", raising=False)
    assert cli._classify_cmd_topic(TAXONOMY, "build a game") == (None, None)


def test_cmd_floor_plain_name_maps(monkeypatch):
    monkeypatch.setenv("CLAUSTRUM_CLASSIFY_CMD", "python3 -c \"print('games')\"")
    assert cli._classify_cmd_topic(TAXONOMY, "build a game") == ("games", 50)


def test_cmd_floor_json_output_parses(monkeypatch):
    monkeypatch.setenv(
        "CLAUSTRUM_CLASSIFY_CMD",
        "python3 -c \"print('{\\\"topic\\\": \\\"bigquery\\\"}')\"",
    )
    assert cli._classify_cmd_topic(TAXONOMY, "run a query") == ("bigquery", 50)


def test_cmd_floor_case_insensitive_match(monkeypatch):
    monkeypatch.setenv("CLAUSTRUM_CLASSIFY_CMD", "python3 -c \"print('GAMES')\"")
    assert cli._classify_cmd_topic(TAXONOMY, "x") == ("games", 50)


def test_cmd_floor_off_taxonomy_falls_through(monkeypatch):
    monkeypatch.setenv("CLAUSTRUM_CLASSIFY_CMD", "python3 -c \"print('nonexistent')\"")
    assert cli._classify_cmd_topic(TAXONOMY, "x") == (None, None)


def test_cmd_floor_nonzero_exit_is_silent(monkeypatch):
    monkeypatch.setenv("CLAUSTRUM_CLASSIFY_CMD", "python3 -c \"import sys; sys.exit(1)\"")
    assert cli._classify_cmd_topic(TAXONOMY, "x") == (None, None)


def test_cmd_floor_empty_output_is_silent(monkeypatch):
    monkeypatch.setenv("CLAUSTRUM_CLASSIFY_CMD", "python3 -c \"pass\"")
    assert cli._classify_cmd_topic(TAXONOMY, "x") == (None, None)


def test_cmd_floor_missing_binary_is_silent(monkeypatch):
    monkeypatch.setenv("CLAUSTRUM_CLASSIFY_CMD", "definitely-not-a-real-binary-xyz")
    assert cli._classify_cmd_topic(TAXONOMY, "x") == (None, None)


# --- _floor_classify (wrapper) ---------------------------------------------

def test_floor_classify_uses_heuristic_when_cmd_unset(monkeypatch):
    monkeypatch.delenv("CLAUSTRUM_CLASSIFY_CMD", raising=False)
    # 'bigquery' signal overlaps the bigquery topic name -> heuristic picks it.
    topic, conf = cli._floor_classify(TAXONOMY, "bigquery dataset work")
    assert topic == "bigquery"
    assert conf and conf > 0


def test_floor_classify_prefers_cmd_over_heuristic(monkeypatch):
    monkeypatch.setenv("CLAUSTRUM_CLASSIFY_CMD", "python3 -c \"print('games')\"")
    topic, conf = cli._floor_classify(TAXONOMY, "bigquery dataset work")
    assert (topic, conf) == ("games", 50)  # command wins over the keyword match


# --- _build_drift_block ----------------------------------------------------

def test_drift_block_shows_topic_domain_and_files():
    block = cli._build_drift_block("uid9", "games", "projects", ["main.py", "README.md"])
    text = "\n".join(block)
    assert 'topic="games"' in text and 'domain="projects"' in text
    assert "main.py, README.md" in text
    assert "classify-self uid9" in text


def test_drift_block_handles_no_files_and_no_domain():
    block = cli._build_drift_block("uid9", "games", None, [])
    text = "\n".join(block)
    assert 'domain="?"' in text
    assert "Recent files" not in text
