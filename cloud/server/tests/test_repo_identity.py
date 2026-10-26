"""Repo identity: a session's repo comes from the checkout of the file it edits,
named after origin — not from the basename of its cwd's toplevel.

Overlap tiers t1/t2/t4 only fire when two sessions report the same `repo`
string, and t1/t2 compare repo-relative paths. Keying on cwd broke all three for
sessions started in a non-git parent folder, inside an unrelated parent repo, or
in a git worktree (which reported the worktree directory's name).

Uses real `git` against tmp repos; the CLI's sqlite goes to a tmp dir and every
cloud call is captured, never sent.
"""
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
    loader = importlib.machinery.SourceFileLoader("claustrum_repo_identity", str(_CLI_PATH))
    spec = importlib.util.spec_from_loader("claustrum_repo_identity", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["claustrum_repo_identity"] = mod
    loader.exec_module(mod)
    return mod


claustrum = _load_cli()


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
         "-c", "init.defaultBranch=main", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd, check=True, capture_output=True,
    )


def _repo(path, origin=None, branch="main"):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", branch)
    _git(path, "commit", "-q", "--allow-empty", "-m", "init")
    if origin:
        _git(path, "remote", "add", "origin", origin)
    return path


def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x\n")
    return path


@pytest.fixture
def cli(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(claustrum, "DB_DIR", state)
    monkeypatch.setattr(claustrum, "DB_PATH", state / "state.db")
    monkeypatch.setattr(claustrum, "_git_memo", {})
    return claustrum


WA_ORIGIN = "git@github.com:finderau/workspace-automation.git"


@pytest.mark.parametrize("url, expected", [
    ("git@github.com:finderau/workspace-automation.git", "workspace-automation"),
    ("https://github.com/finderau/workspace-automation.git", "workspace-automation"),
    ("https://github.com/finderau/workspace-automation/", "workspace-automation"),
    ("ssh://git@github.com:22/finderau/workspace-automation", "workspace-automation"),
    ("/srv/git/workspace-automation.git", "workspace-automation"),
    ("", None),
    (None, None),
])
def test_canonical_repo_name(url, expected):
    assert claustrum._canonical_repo_name(url) == expected


def test_non_git_cwd_file_in_repo(cli, tmp_path):
    parent = tmp_path / "projects"
    repo = _repo(parent / "wa-clone", origin=WA_ORIGIN)
    f = _touch(repo / "mcp-proxies" / "artefacts" / "server.js")

    assert cli._git_info(str(parent)) == (None, None)
    assert cli._git_info(str(f.parent)) == ("workspace-automation", "main")
    assert cli._rel_to_repo(str(f), str(parent)) == "mcp-proxies/artefacts/server.js"


def test_nested_parent_repo_resolves_inner_checkout(cli, tmp_path):
    dev = _repo(tmp_path / "dev", branch="master")
    inner = _repo(dev / "workspace-automation", origin=WA_ORIGIN, branch="feat/brand")
    f = _touch(inner / "mcp-proxies" / "artefacts" / "brand.md")

    assert cli._git_info(str(dev)) == ("dev", "master")
    assert cli._git_info(str(f.parent)) == ("workspace-automation", "feat/brand")
    assert cli._rel_to_repo(str(f), str(dev)) == "mcp-proxies/artefacts/brand.md"


def test_worktree_reports_canonical_name_and_same_rel_path(cli, tmp_path):
    main = _repo(tmp_path / "workspace-automation", origin=WA_ORIGIN)
    wt = tmp_path / "session-feat-artefacts-brand-guidance"
    _git(main, "worktree", "add", "-q", "-b", "feat/artefacts", str(wt))
    f_main = _touch(main / "mcp-proxies" / "artefacts" / "a.js")
    f_wt = _touch(wt / "mcp-proxies" / "artefacts" / "a.js")

    assert cli._git_info(str(wt)) == ("workspace-automation", "feat/artefacts")
    assert cli._rel_to_repo(str(f_wt)) == cli._rel_to_repo(str(f_main)) == "mcp-proxies/artefacts/a.js"


def test_no_origin_worktree_falls_back_to_main_checkout_name(cli, tmp_path):
    main = _repo(tmp_path / "work-platform")
    wt = tmp_path / "work-platform-wt-viewer-perms"
    _git(main, "worktree", "add", "-q", "-b", "viewer-perms", str(wt))

    assert cli._git_info(str(main)) == ("work-platform", "main")
    assert cli._git_info(str(wt)) == ("work-platform", "viewer-perms")


def test_file_in_not_yet_created_directory(cli, tmp_path):
    repo = _repo(tmp_path / "clone", origin=WA_ORIGIN)
    missing = repo / "new" / "dir" / "file.py"
    assert cli._git_info(str(missing.parent)) == ("workspace-automation", "main")
    assert cli._rel_to_repo(str(missing)) == "new/dir/file.py"


def test_lookups_are_cached_in_memory_and_on_disk(cli, tmp_path, monkeypatch):
    repo = _repo(tmp_path / "clone", origin=WA_ORIGIN)
    calls = []
    real = cli._git_lookup
    monkeypatch.setattr(cli, "_git_lookup", lambda t: calls.append(t) or real(t))

    first = cli._git_checkout(str(repo))
    assert cli._git_checkout(str(repo)) == first
    cli._git_memo.clear()  # a fresh hook process: only the disk cache remains
    assert cli._git_checkout(str(repo)) == first
    assert len(calls) == 1

    non_git = tmp_path / "plain"
    non_git.mkdir()
    cli._git_checkout(str(non_git))
    cli._git_memo.clear()
    assert cli._git_checkout(str(non_git)) == (None, None, None)
    assert len(calls) == 2


# --- session follows the edited file ------------------------------------------

def _seed(m, uid, cwd, label="artefacts-brand"):
    db = m.get_db()
    now = m.time.time()
    db.execute(
        "INSERT INTO sessions (uid, status, cwd, label, last_seen, started_at) "
        "VALUES (?, 'active', ?, ?, ?, ?)",
        (uid, cwd, label, now, now),
    )
    db.commit()
    db.close()


def _post_tool(m, monkeypatch, uid, cwd, filepath):
    payload = {"session_id": uid, "cwd": cwd, "tool_name": "Edit",
               "tool_input": {"file_path": filepath}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    m.hook_post_tool(None)


@pytest.fixture
def cloud(cli, monkeypatch):
    sent = []
    monkeypatch.setattr(cli, "_should_cloud_send", lambda *a, **k: True)
    monkeypatch.setattr(
        cli, "_cloud_request",
        lambda method, path, body=None, params=None, timeout=None:
            sent.append((path, body)) or {},
    )
    monkeypatch.setattr(cli, "_get_pr_number", lambda *a: None)
    return sent


def test_session_repo_follows_edited_file_not_cwd(cli, cloud, tmp_path, monkeypatch):
    dev = _repo(tmp_path / "dev", branch="master")
    wa = _repo(dev / "workspace-automation", origin=WA_ORIGIN, branch="feat/brand")
    f = _touch(wa / "mcp-proxies" / "artefacts" / "brand.md")
    uid = "sess-kelly"
    _seed(cli, uid, str(dev))

    cli._cloud_checkin(uid, cwd=str(dev))
    assert cloud[-1][1]["repo"] == "dev"  # cwd is only the first guess

    cloud.clear()
    _post_tool(cli, monkeypatch, uid, str(dev), str(f))
    checkin = [b for p, b in cloud if p == "/v1/checkin"]
    update = [b for p, b in cloud if p == "/v1/update"]
    assert checkin and checkin[0]["repo"] == "workspace-automation"
    assert checkin[0]["branch"] == "feat/brand"
    assert checkin[0]["label"] == "artefacts-brand"
    assert update[0]["files_touched"] == ["mcp-proxies/artefacts/brand.md"]

    # Later per-turn checkins keep the file's repo instead of reverting to cwd.
    cloud.clear()
    cli._cloud_checkin(uid, cwd=str(dev))
    assert cloud[-1][1]["repo"] == "workspace-automation"

    # Another edit in the same checkout doesn't re-checkin.
    cloud.clear()
    _post_tool(cli, monkeypatch, uid, str(dev), str(f))
    assert [p for p, _ in cloud] == ["/v1/update"]


def test_session_moves_when_work_moves_repo(cli, cloud, tmp_path, monkeypatch):
    wa = _repo(tmp_path / "workspace-automation", origin=WA_ORIGIN)
    gw = _repo(tmp_path / "gw", origin="https://github.com/finderau/mcp-gateway.git")
    uid = "sess-mover"
    _seed(cli, uid, str(tmp_path))

    _post_tool(cli, monkeypatch, uid, str(tmp_path), str(_touch(wa / "a.py")))
    cloud.clear()
    _post_tool(cli, monkeypatch, uid, str(tmp_path), str(_touch(gw / "b.py")))
    checkin = [b for p, b in cloud if p == "/v1/checkin"]
    assert checkin and checkin[0]["repo"] == "mcp-gateway"


def test_edit_outside_any_repo_keeps_last_repo(cli, cloud, tmp_path, monkeypatch):
    wa = _repo(tmp_path / "workspace-automation", origin=WA_ORIGIN)
    loose = tmp_path / "scratch"
    loose.mkdir()
    uid = "sess-loose"
    _seed(cli, uid, str(tmp_path))

    _post_tool(cli, monkeypatch, uid, str(tmp_path), str(_touch(wa / "a.py")))
    cloud.clear()
    _post_tool(cli, monkeypatch, uid, str(tmp_path), str(_touch(loose / "notes.txt")))
    assert [p for p, _ in cloud] == ["/v1/update"]
    assert cli._session_repo(uid)[0] == "workspace-automation"


def test_checkin_tracks_branch_switch_without_further_edits(cli, cloud, tmp_path, monkeypatch):
    wa = _repo(tmp_path / "workspace-automation", origin=WA_ORIGIN)
    uid = "sess-switch"
    _seed(cli, uid, str(tmp_path))
    _post_tool(cli, monkeypatch, uid, str(tmp_path), str(_touch(wa / "a.py")))

    _git(wa, "checkout", "-q", "-b", "feat/next")
    cli._git_memo.clear()
    (cli.DB_DIR / "git-cache.json").unlink()
    cloud.clear()
    cli._cloud_checkin(uid, cwd=str(tmp_path))
    assert cloud[-1][1]["repo"] == "workspace-automation"
    assert cloud[-1][1]["branch"] == "feat/next"


def test_removed_checkout_keeps_stored_repo(cli, cloud, tmp_path, monkeypatch):
    main = _repo(tmp_path / "workspace-automation", origin=WA_ORIGIN)
    wt = main / ".claude" / "worktrees" / "wt"
    _git(main, "worktree", "add", "-q", "-b", "feat/wt", str(wt))
    uid = "sess-gone"
    _seed(cli, uid, str(tmp_path))
    _post_tool(cli, monkeypatch, uid, str(tmp_path), str(_touch(wt / "a.py")))

    _git(main, "worktree", "remove", "--force", str(wt))
    cli._git_memo.clear()
    (cli.DB_DIR / "git-cache.json").unlink()
    cloud.clear()
    cli._cloud_checkin(uid, cwd=str(tmp_path))
    assert cloud[-1][1]["branch"] == "feat/wt"


def test_corrupt_disk_cache_is_ignored(cli, tmp_path):
    repo = _repo(tmp_path / "clone", origin=WA_ORIGIN)
    cli.DB_DIR.mkdir(parents=True, exist_ok=True)
    (cli.DB_DIR / "git-cache.json").write_text(
        json.dumps({str(repo): {"ts": "bad"}, "/x": {"ts": None}, "/y": 3})
    )
    assert cli._git_checkout(str(repo))[1] == "workspace-automation"
