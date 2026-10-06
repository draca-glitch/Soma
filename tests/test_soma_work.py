"""Workspace senses (0.12.0 part B): peer sessions, HEAD moved, own leftovers."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS))
import soma_lib  # noqa: E402
import soma_work  # noqa: E402

HZ = 100
ROOT_START = 1000 * HZ       # the session root started at uptime 1000 s
UPTIME = 10000.0             # now: uptime 10000 s


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("SOMA_PEERS", "SOMA_HEAD", "SOMA_BG", "SOMA_BG_AGE_S", "SOMA_SELF_COMM", "SOMA_MODE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(soma_work, "CLK_TCK", HZ)


def _proc(tmp_path, procs, uptime=UPTIME):
    """procs: (pid, ppid, comm, start_ticks, cwd or None, env dict or None, state)."""
    root = tmp_path / "proc"
    root.mkdir(exist_ok=True)
    (root / "uptime").write_text(f"{uptime} 1.0\n")
    for p in procs:
        pid, ppid, comm, start, cwd, env = p[:6]
        st = p[6] if len(p) > 6 else "S"
        d = root / str(pid)
        d.mkdir()
        rest = [st, str(ppid)] + ["0"] * 17 + [str(start), "0"]
        (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(rest) + "\n")
        (d / "statm").write_text("1000 100 10 1 0 100 0\n")
        (d / "comm").write_text(comm + "\n")
        if cwd:
            os.symlink(cwd, d / "cwd")
        if env is not None:
            (d / "environ").write_bytes(b"".join(f"{k}={v}".encode() + b"\0" for k, v in env.items()))
    return str(root)


def _git(path, head="ref: refs/heads/main\n"):
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir(exist_ok=True)
    (path / ".git" / "HEAD").write_text(head)
    return path


def _base(repo, other):
    """This session: tmux bash 10 -> claude 100 -> hook bash 900 -> hook python 901 (self)."""
    return [
        (1, 0, "systemd", 0, None, None),
        (10, 1, "bash", 100, None, None),
        (100, 10, "claude", ROOT_START, str(repo), None),
        (101, 100, "python3", ROOT_START + 50, str(repo), {"CLAUDECODE": "1"}),  # MCP server
        (900, 100, "bash", 9999 * HZ, str(repo), {"CLAUDE_PID": "100"}),
        (901, 900, "python3", 9999 * HZ, str(repo), {"CLAUDE_PID": "100"}),
    ]


def _reading(root, repo, sdir, now=5000.0, pulse=False, extra=None):
    table = soma_lib.proc_table(root)
    hi = {"session_id": "s1", "cwd": str(repo)}
    hi.update(extra or {})
    return soma_work.work_reading(hi, table, proc_root=root, sdir=str(sdir), now=now, pulse=pulse, self_pid=901)


# ---- the shared walk -------------------------------------------------------------------

def test_one_walk_feeds_top_and_self(tmp_path):
    root = _proc(tmp_path, _base(tmp_path, tmp_path))
    table = soma_lib.proc_table(root)
    assert table[100]["comm"] == "claude" and table[901]["ppid"] == 900 and table[100]["start"] == ROOT_START
    assert soma_lib.top_rss(root, table=table)["name"]
    assert soma_lib.self_tree_rss(root, self_pid=901, table=table)["name"] == "claude"
    assert soma_lib.self_tree_rss(root, self_pid=901, table=table) == soma_lib.self_tree_rss(root, self_pid=901)


def test_gather_walks_proc_once(tmp_path, monkeypatch):
    root = _proc(tmp_path, _base(tmp_path, tmp_path))
    calls = []
    real = soma_lib.proc_table
    monkeypatch.setattr(soma_lib, "proc_table", lambda r: calls.append(r) or real(r))
    state = soma_lib.gather(root, mounts=[], services=[], hwmon_root=str(tmp_path / "x"), sys_root=str(tmp_path / "x"))
    assert len(calls) == 1 and state["top"] and "_procs" in state


# ---- peers ---------------------------------------------------------------------------------

def _peer_tree(tmp_path):
    repo = _git(tmp_path / "repo")
    (repo / "sub").mkdir()
    other = _git(tmp_path / "other")
    procs = _base(repo, other) + [
        (200, 10, "claude", 5000 * HZ, str(repo / "sub"), None),       # same toplevel, other cwd
        (300, 10, "claude", 5000 * HZ, str(other), None),              # elsewhere
        (400, 10, "claude", 5000 * HZ, None, None),                    # cwd unreadable
        (500, 900, "claude", 9000 * HZ, str(repo), None),              # headless child of ours
        (600, 200, "claude", 9000 * HZ, str(repo), None),              # headless child of a peer
        (700, 10, "claude", 5000 * HZ, str(repo), None, "Z"),          # zombie
        (800, 10, "claude", 5000 * HZ, str(repo), None, "T"),          # stopped
    ]
    return repo, other, _proc(tmp_path, procs)


def test_peers_count_same_toplevel_excluding_own_children_and_dead(tmp_path):
    repo, _, root = _peer_tree(tmp_path)
    p = soma_work.peers(soma_lib.proc_table(root), root, 901, str(repo))
    assert p == {"here": 1, "total": 4}  # 200 here; sessions: self, 200, 300, 400


def test_peers_same_cwd_without_git(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    procs = _base(plain, plain) + [(200, 10, "claude", 5000 * HZ, str(plain), None),
                                   (300, 10, "claude", 5000 * HZ, str(tmp_path), None)]
    root = _proc(tmp_path, procs)
    assert soma_work.peers(soma_lib.proc_table(root), root, 901, str(plain)) == {"here": 1, "total": 3}


def test_peers_none_when_hook_not_under_a_session(tmp_path):
    root = _proc(tmp_path, [(1, 0, "systemd", 0, None, None), (901, 1, "python3", 5, None, None)])
    assert soma_work.peers(soma_lib.proc_table(root), root, 901, "/") is None


def test_peer_announced_once_then_quiet_then_again_after_leaving(tmp_path):
    repo, _, root = _peer_tree(tmp_path)
    sd = tmp_path / "state"
    r = _reading(root, repo, sd)
    assert r["force"] and "PEER" in r["flags"] and "peers 1 here (4 sessions)" in r["segs"]
    r = _reading(root, repo, sd, now=5010.0)
    assert not r["force"] and r["segs"] == ["peers 1 here (4 sessions)"]
    lone = tmp_path / "lone"
    lone.mkdir()
    root2 = _proc(lone, _base(repo, repo))
    r = _reading(root2, repo, sd, now=5020.0)
    assert not r["force"] and r["segs"] == []
    r = _reading(root, repo, sd, now=5030.0)
    assert r["force"] and "PEER" in r["flags"]


def test_peer_claim_is_exclusive(tmp_path):
    repo, _, root = _peer_tree(tmp_path)
    sd = tmp_path / "state"
    soma_work._write(sd and str(sd), "s1", {"ts": 4000, "peers": 0})
    first = _reading(root, repo, sd)
    soma_work._write(str(sd), "s1", {"ts": 4000, "peers": 0})  # a racing caller saw the same record
    second = _reading(root, repo, sd)
    assert first["force"] and not second["force"]


def test_peers_off(tmp_path, monkeypatch):
    repo, _, root = _peer_tree(tmp_path)
    monkeypatch.setenv("SOMA_PEERS", "0")
    r = _reading(root, repo, tmp_path / "state")
    assert not r["force"] and not any(s.startswith("peers") for s in r["segs"])


# ---- HEAD moved ------------------------------------------------------------------------------

def _solo(tmp_path, repo):
    return _proc(tmp_path, _base(repo, repo))


def test_head_change_said_once_at_prompt(tmp_path):
    repo = _git(tmp_path / "repo")
    root, sd = _solo(tmp_path, repo), tmp_path / "state"
    assert _reading(root, repo, sd)["segs"] == []  # first contact records silently
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/plan-06\n")
    r = _reading(root, repo, sd, now=5010.0)
    assert r["force"] and "HEAD" in r["flags"] and "HEAD main→plan-06 since last prompt" in r["segs"]
    assert _reading(root, repo, sd, now=5020.0)["segs"] == []


def test_head_own_git_command_absorbed_in_pulse(tmp_path):
    repo = _git(tmp_path / "repo")
    root, sd = _solo(tmp_path, repo), tmp_path / "state"
    _reading(root, repo, sd)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/plan-06\n")
    r = _reading(root, repo, sd, now=5010.0, pulse=True,
                 extra={"tool_name": "Bash", "tool_input": {"command": "cd x && git checkout plan-06"}})
    assert r["segs"] == [] and not r["force"]
    assert _reading(root, repo, sd, now=5020.0)["segs"] == []


def test_head_foreign_change_announced_in_pulse(tmp_path):
    repo = _git(tmp_path / "repo")
    root, sd = _solo(tmp_path, repo), tmp_path / "state"
    _reading(root, repo, sd)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/plan-06\n")
    r = _reading(root, repo, sd, now=5010.0, pulse=True, extra={"tool_name": "Read", "tool_input": {}})
    assert r["force"] and "HEAD main→plan-06 since last tool call" in r["segs"]


def test_head_subagent_consumes_nothing(tmp_path):
    repo = _git(tmp_path / "repo")
    root, sd = _solo(tmp_path, repo), tmp_path / "state"
    _reading(root, repo, sd)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/plan-06\n")
    sub = _reading(root, repo, sd, now=5005.0, pulse=True,
                   extra={"agent_id": "a1", "tool_name": "Bash", "tool_input": {"command": "git checkout plan-06"}})
    assert sub == {"segs": [], "force": False, "flags": set()}
    assert _reading(root, repo, sd, now=5010.0)["force"]


def test_head_worktree_gitdir_file_and_detached(tmp_path):
    main = _git(tmp_path / "main")
    wt_git = main / ".git" / "worktrees" / "wt"
    wt_git.mkdir(parents=True)
    (wt_git / "HEAD").write_text("ref: refs/heads/feature\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {wt_git}\n")
    assert soma_work.git_head(str(wt)) == (str(wt), "feature")
    (wt_git / "HEAD").write_text("0123456789abcdef0123456789abcdef01234567\n")
    assert soma_work.git_head(str(wt)) == (str(wt), "0123456")
    (wt / ".git").write_text("gitdir: ../main/.git/worktrees/wt\n")  # relative form
    assert soma_work.git_head(str(wt)) == (str(wt), "0123456")


def test_head_same_ref_new_commit_silent_and_no_git_silent(tmp_path):
    repo = _git(tmp_path / "repo")
    root, sd = _solo(tmp_path, repo), tmp_path / "state"
    _reading(root, repo, sd)
    (repo / ".git" / "refs" / "heads").mkdir(parents=True)
    (repo / ".git" / "refs" / "heads" / "main").write_text("f" * 40 + "\n")  # a commit on main
    assert _reading(root, repo, sd, now=5010.0)["segs"] == []
    plain = tmp_path / "plain"
    plain.mkdir()
    assert soma_work.git_head(str(plain)) is None or soma_work.git_head(str(plain))[0] != str(plain)
    r = _reading(root, plain, tmp_path / "state2")
    assert r["segs"] == [] and not r["force"]


def test_head_other_toplevel_records_silently(tmp_path):
    a, b = _git(tmp_path / "a"), _git(tmp_path / "b", "ref: refs/heads/dev\n")
    root, sd = _solo(tmp_path, a), tmp_path / "state"
    _reading(root, a, sd)
    assert _reading(root, b, sd, now=5010.0)["segs"] == []


def test_head_clock_backwards_and_old_state(tmp_path):
    repo = _git(tmp_path / "repo")
    root, sd = _solo(tmp_path, repo), tmp_path / "state"
    soma_work._write(str(sd), "s1", {"ts": "junk", "top": 7, "head": ["x"], "peers": True})
    assert _reading(root, repo, sd)["segs"] == []
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/plan-06\n")
    assert _reading(root, repo, sd, now=10.0)["force"]  # clock went back: still said, once
    assert not _reading(root, repo, sd, now=11.0)["force"]


def test_head_off(tmp_path, monkeypatch):
    repo = _git(tmp_path / "repo")
    root, sd = _solo(tmp_path, repo), tmp_path / "state"
    monkeypatch.setenv("SOMA_HEAD", "0")
    _reading(root, repo, sd)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/plan-06\n")
    assert not _reading(root, repo, sd, now=5010.0)["force"]


# ---- own leftovers ---------------------------------------------------------------------------

def _bg_tree(tmp_path, repo):
    mine = {"CLAUDE_PID": "100"}
    return _base(repo, repo) + [
        (1100, 1, "php", 2000 * HZ, None, mine),                      # orphaned dev server, 8000 s old
        (1101, 1100, "php", 2000 * HZ, None, mine),                   # its worker: same leftover
        (1200, 100, "bash", 8000 * HZ, None, mine),                   # background Bash task, 2000 s
        (1201, 1200, "node", 8000 * HZ, None, mine),
        (1300, 1, "sleep", 9900 * HZ, None, mine),                    # too young (100 s)
        (1400, 1, "php", 2000 * HZ, None, {"CLAUDE_PID": "555"}),     # another session's
        (1500, 1, "php", 500 * HZ, None, {"CLAUDE_PID": "100"}),      # older than our root: pid reuse
        (1600, 1, "nginx", 2000 * HZ, None, {}),                      # a service
        (1700, 100, "python3", 3000 * HZ, None, {"CLAUDECODE": "1"}),  # MCP server reconnected later
    ]


def test_leftover_shell_named_by_old_child_not_by_passing_sleep(tmp_path):
    repo = _git(tmp_path / "repo")
    mine = {"CLAUDE_PID": "100"}
    procs = _base(repo, repo) + [(1200, 100, "bash", 3000 * HZ, None, mine),
                                 (1201, 1200, "sleep", 9995 * HZ, None, mine)]
    root = _proc(tmp_path, procs)
    assert soma_work.leftovers(soma_lib.proc_table(root), root, 901, 600)["name"] == "bash"


def test_leftovers_counts_bash_started_work_only(tmp_path):
    repo = _git(tmp_path / "repo")
    root = _proc(tmp_path, _bg_tree(tmp_path, repo))
    bg = soma_work.leftovers(soma_lib.proc_table(root), root, 901, 600)
    assert bg == {"count": 2, "oldest_s": 8000, "name": "php"}
    assert soma_work.bg_segment(bg) == "bg 2 (oldest 2h php)"
    assert soma_work.bg_segment({"count": 1, "oldest_s": 2820, "name": "node"}) == "bg 1 (oldest 47m node)"


def test_leftovers_mcp_servers_are_not_leftovers(tmp_path):
    repo = _git(tmp_path / "repo")
    root = _proc(tmp_path, _base(repo, repo))
    assert soma_work.leftovers(soma_lib.proc_table(root), root, 901, 600) is None


def test_leftovers_never_force_and_off(tmp_path, monkeypatch):
    repo = _git(tmp_path / "repo")
    root = _proc(tmp_path, _bg_tree(tmp_path, repo))
    r = _reading(root, repo, tmp_path / "state")
    assert not r["force"] and r["flags"] == set() and r["segs"] == ["bg 2 (oldest 2h php)"]
    monkeypatch.setenv("SOMA_BG_AGE_S", "9000")
    assert _reading(root, repo, tmp_path / "state")["segs"] == []
    monkeypatch.setenv("SOMA_BG_AGE_S", "600")
    monkeypatch.setenv("SOMA_BG", "0")
    assert _reading(root, repo, tmp_path / "state")["segs"] == []


# ---- junk and the hooks end to end ------------------------------------------------------------

@pytest.mark.parametrize("payload", [None, [], "x", {}, {"session_id": 5}, {"session_id": "s1", "cwd": 7},
                                     {"session_id": "s1", "cwd": "rel/path"},
                                     {"session_id": "s1", "cwd": "/", "tool_input": "x", "tool_name": 3}])
def test_junk_payload_never_raises(tmp_path, payload):
    root = _proc(tmp_path, _base(tmp_path, tmp_path))
    r = soma_work.work_reading(payload, soma_lib.proc_table(root), proc_root=root, sdir=str(tmp_path), pulse=True)
    assert r["force"] is False
    assert soma_work.work_reading({"session_id": "s1", "cwd": "/"}, None, proc_root=root, sdir=str(tmp_path))


def test_hooks_without_work_module_give_plain_line(tmp_path):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for f in HOOKS.glob("*.py"):
        if f.name != "soma_work.py":
            (hooks / f.name).write_text(f.read_text())
    env = dict(os.environ, SOMA_STATE_DIR=str(tmp_path / "st"), SOMA_MODE="always")
    pay = json.dumps({"session_id": "s1", "cwd": str(tmp_path)})
    out = subprocess.run([sys.executable, str(hooks / "soma-state.py")], input=pay, text=True,
                         capture_output=True, env=env)
    assert out.returncode == 0 and "[system-state]" in out.stdout and not out.stderr


def test_line_for_mode_forces_on_head_and_is_unchanged_without_data(tmp_path, monkeypatch):
    repo = _git(tmp_path / "repo")
    sd = str(tmp_path / "state")
    proc = Path(_solo(tmp_path, repo))
    for name, text in (("meminfo", "MemTotal: 64000000 kB\nMemAvailable: 36000000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n"),
                       ("loadavg", "1.0 1.0 1.0 1/100 1\n")):
        (proc / name).write_text(text)
    monkeypatch.setattr(soma_work, "_self_pid", lambda: 901)
    kw = dict(proc_root=str(proc), mounts=[], services=[], hwmon_root=str(tmp_path / "x"),
              sys_root=str(tmp_path / "x"), state_dir=sd)
    hi = {"session_id": "s1", "cwd": str(repo)}
    assert soma_lib.line_for_mode("pressure", now=5000.0, hook_input=hi, **kw) is None
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/plan-06\n")
    line = soma_lib.line_for_mode("pressure", now=5010.0, hook_input=hi, **kw)
    assert line and line.endswith(" · HEAD main→plan-06 since last prompt")
    plain = soma_lib.line_for_mode("always", now=5020.0, hook_input=None, **kw)
    monkeypatch.setenv("SOMA_HEAD", "0")
    assert soma_lib.line_for_mode("always", now=5030.0, hook_input=hi, **kw) == plain


def _host(tmp_path, repo):
    proc = Path(_solo(tmp_path, repo))
    (proc / "meminfo").write_text("MemTotal: 64000000 kB\nMemAvailable: 36000000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n")
    (proc / "loadavg").write_text("1.0 1.0 1.0 1/100 1\n")
    return dict(proc_root=str(proc), mounts=[], services=[], hwmon_root=str(tmp_path / "x"),
                sys_root=str(tmp_path / "x"), state_dir=str(tmp_path / "state"))


def test_pulse_line_says_foreign_head_once_and_absorbs_own(tmp_path, monkeypatch):
    repo = _git(tmp_path / "repo")
    kw = _host(tmp_path, repo)
    monkeypatch.setattr(soma_work, "_self_pid", lambda: 901)
    hi = {"session_id": "s1", "cwd": str(repo), "tool_name": "Read", "tool_input": {}}
    assert soma_lib.pulse_line(now=5000.0, hook_input=hi, hold_s=0, **kw) is None
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/plan-06\n")
    sub = dict(hi, agent_id="a1")
    assert soma_lib.pulse_line(now=5005.0, hook_input=sub, hold_s=0, **kw) is None
    line = soma_lib.pulse_line(now=5010.0, hook_input=hi, hold_s=0, **kw)
    assert line and line.endswith(" · HEAD main→plan-06 since last tool call")
    assert soma_lib.pulse_line(now=5020.0, hook_input=hi, hold_s=0, **kw) is None
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    own = dict(hi, tool_name="Bash", tool_input={"command": "git switch main"})
    assert soma_lib.pulse_line(now=5030.0, hook_input=own, hold_s=0, **kw) is None
    assert soma_lib.line_for_mode("pressure", now=5040.0, hook_input=hi, **kw) is None


# ---- 0.12.0 pre-release fixes: no walk on a network mount, cache, own children, FIFOs ----------

import builtins  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402


def _record_fs(monkeypatch):
    seen = []
    for name in ("stat", "lstat", "open", "readlink", "listdir", "scandir", "statvfs", "access"):
        real = getattr(os, name)

        def wrap(*a, _real=real, **k):
            if a and isinstance(a[0], (str, bytes, os.PathLike)):
                seen.append(os.fsdecode(a[0]))
            return _real(*a, **k)
        monkeypatch.setattr(os, name, wrap)
    real_open = builtins.open

    def bopen(f, *a, **k):
        if isinstance(f, (str, bytes, os.PathLike)):
            seen.append(os.fsdecode(f))
        return real_open(f, *a, **k)
    monkeypatch.setattr(builtins, "open", bopen)
    return seen


def test_no_os_call_touches_a_path_under_a_network_mount(tmp_path, monkeypatch):
    nas = tmp_path / "nas"
    repo = _git(nas / "share" / "repo")
    local = _git(tmp_path / "local")
    procs = _base(repo, None) + [(300, 1, "claude", ROOT_START + 5, str(repo), None)]
    root = _proc(tmp_path, procs)
    table = soma_lib.proc_table(root)
    seen = _record_fs(monkeypatch)
    for cwd in (str(repo), str(local)):
        out = soma_work.work_reading({"session_id": "s1", "cwd": cwd}, table, proc_root=root,
                                     sdir=str(tmp_path / "st"), now=5000.0, self_pid=901, net=[str(nas)])
        assert not any("HEAD" in s for s in out["segs"])
        assert not any(s.startswith("peers") for s in out["segs"])
    assert [p for p in seen if p == str(nas) or p.startswith(str(nas) + "/")] == []


def test_a_stale_local_mount_is_also_untouched(tmp_path, monkeypatch):
    repo = _git(tmp_path / "m" / "repo")
    root = _proc(tmp_path, _base(repo, None))
    table = soma_lib.proc_table(root)
    seen = _record_fs(monkeypatch)
    soma_work.work_reading({"session_id": "s1", "cwd": str(repo)}, table, proc_root=root,
                           sdir=str(tmp_path / "st"), now=5000.0, self_pid=901, net=[str(tmp_path / "m")])
    assert [p for p in seen if p.startswith(str(tmp_path / "m"))] == []


def test_toplevel_walked_once_per_cwd_then_every_60s(tmp_path, monkeypatch):
    repo = _git(tmp_path / "repo")
    sub = repo / "a"
    sub.mkdir()
    root = _proc(tmp_path, _base(repo, None))
    calls = []
    real = soma_work.toplevel
    monkeypatch.setattr(soma_work, "toplevel", lambda *a, **k: calls.append(a[0]) or real(*a, **k))
    for i in range(10):
        _reading(root, repo, tmp_path / "st", now=5000.0 + i)
    assert len(calls) == 1
    _reading(root, sub, tmp_path / "st", now=5011.0)
    assert len(calls) == 2
    _reading(root, sub, tmp_path / "st", now=5072.0)
    assert len(calls) == 3


def test_mine_not_computed_without_a_candidate_peer(tmp_path, monkeypatch):
    monkeypatch.setenv("SOMA_HEAD", "0")
    repo = _git(tmp_path / "repo")
    root = _proc(tmp_path, _base(repo, None))
    calls = []
    real = soma_work.toplevel
    monkeypatch.setattr(soma_work, "toplevel", lambda *a, **k: calls.append(a[0]) or real(*a, **k))
    _reading(root, repo, tmp_path / "st")
    assert calls == []


def test_own_detached_headless_child_is_not_a_peer(tmp_path):
    repo = _git(tmp_path / "repo")
    procs = _base(repo, None) + [(200, 1, "claude", ROOT_START + 9, str(repo), {"CLAUDE_PID": "100"})]
    root = _proc(tmp_path, procs)
    pr = soma_work.peers(soma_lib.proc_table(root), root, 901, str(repo))
    assert pr == {"here": 0, "total": 1}
    # an unreadable environ leaves the candidate a peer
    procs2 = _base(repo, None) + [(200, 1, "claude", ROOT_START + 9, str(repo), None)]
    (tmp_path / "b").mkdir()
    root2 = _proc(tmp_path / "b", procs2)
    assert soma_work.peers(soma_lib.proc_table(root2), root2, 901, str(repo))["here"] == 1


def test_symlinked_cwd_is_resolved_before_comparing(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link)
    procs = _base(real, None) + [(300, 1, "claude", ROOT_START + 5, str(real), None)]
    root = _proc(tmp_path, procs)
    assert soma_work.peers(soma_lib.proc_table(root), root, 901, str(link))["here"] == 1


def test_session_root_is_the_topmost_claude(tmp_path):
    repo = _git(tmp_path / "repo")
    procs = [(1, 0, "systemd", 0, None, None),
             (100, 1, "claude", ROOT_START, str(repo), None),
             (150, 100, "claude", ROOT_START + 10, str(repo), None),
             (900, 150, "bash", 9999 * HZ, str(repo), None),
             (901, 900, "python3", 9999 * HZ, str(repo), None),
             (300, 1, "claude", ROOT_START + 5, str(tmp_path), None)]
    root = _proc(tmp_path, procs)
    # 150 is under 100, so it is neither the root nor a peer; 300 is the one other session
    assert soma_work.peers(soma_lib.proc_table(root), root, 901, str(repo)) == {"here": 0, "total": 2}


def _finishes(fn, limit=2.0):
    box = []
    t = threading.Thread(target=lambda: box.append(fn()), daemon=True)
    t.start()
    t.join(limit)
    return not t.is_alive(), box


def test_fifo_at_dot_git_does_not_hang(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    os.mkfifo(repo / ".git")
    done, box = _finishes(lambda: soma_work.git_head(str(repo)))
    assert done and box == [None]


def test_fifo_at_head_does_not_hang(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    os.mkfifo(repo / ".git" / "HEAD")
    done, box = _finishes(lambda: soma_work.git_head(str(repo)))
    assert done and box == [None]


def test_empty_head_keeps_the_record_and_a_later_move_is_said(tmp_path):
    repo = _git(tmp_path / "repo")
    root = _proc(tmp_path, _base(repo, None))
    sd = tmp_path / "st"
    _reading(root, repo, sd, now=5000.0)
    (repo / ".git" / "HEAD").write_text("")
    assert not any("HEAD" in s for s in _reading(root, repo, sd, now=5001.0)["segs"])
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/other\n")
    assert any("HEAD main→other" in s for s in _reading(root, repo, sd, now=5002.0)["segs"])


@pytest.mark.parametrize("tool,cmd,absorbed", [
    ("Bash", "cat .gitignore", False),
    ("Bash", "ls; git checkout x", True),
    ("Bash", "git", True),
    ("Bash", "(git switch y)", True),
    ("Bash", "echo legit stuff", False),
    ("Edit", "git checkout x", False),
])
def test_own_git_command_is_a_command_word_in_bash_only(tmp_path, tool, cmd, absorbed):
    repo = _git(tmp_path / "repo")
    root = _proc(tmp_path, _base(repo, None))
    sd = tmp_path / "st"
    _reading(root, repo, sd, now=5000.0, pulse=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/other\n")
    out = _reading(root, repo, sd, now=5001.0, pulse=True, extra={"tool_name": tool, "tool_input": {"command": cmd}})
    assert any("HEAD" in s for s in out["segs"]) is (not absorbed)


def test_hook_and_statusline_children_are_not_leftovers_but_detached_work_is(tmp_path):
    repo = _git(tmp_path / "repo")
    old = 2000 * HZ
    procs = _base(repo, None) + [
        (400, 100, "sh", old, str(repo), {"CLAUDE_PID": "100", "CLAUDE_PROJECT_DIR": "x"}),
        (401, 400, "sleep", old, str(repo), {"CLAUDE_PID": "100", "CLAUDE_PROJECT_DIR": "x"}),
        (500, 1, "node", old + 1, str(repo), {"CLAUDE_PID": "100"}),
    ]
    root = _proc(tmp_path, procs)
    bg = soma_work.leftovers(soma_lib.proc_table(root), root, 901, 600)
    assert bg is not None and bg["count"] == 1 and bg["name"] == "node"


def test_a_git_link_or_gitdir_into_a_network_mount_reads_as_no_repo(tmp_path, monkeypatch):
    nas = tmp_path / "nas"
    _git(nas / "real")
    a = tmp_path / "a"
    a.mkdir()
    (a / ".git").symlink_to(nas / "real" / ".git")             # a symlinked .git
    b = tmp_path / "b"
    b.mkdir()
    (b / ".git").write_text(f"gitdir: {nas / 'real' / '.git'}\n")  # an absolute gitdir: pointer
    c = tmp_path / "c"
    c.mkdir()
    (c / ".git").write_text("gitdir: ../nas/real/.git\n")      # a relative one
    d = tmp_path / "d"
    d.mkdir()
    (tmp_path / "hop").symlink_to(nas)                          # a local link that leads into it
    (d / ".git").write_text(f"gitdir: {tmp_path / 'hop' / 'real' / '.git'}\n")
    seen = _record_fs(monkeypatch)
    for top in (a, b, c, d):
        assert soma_work.git_head(str(top), [str(nas)]) is None, top
        assert soma_work.head_ref(str(top), [str(nas)]) is None, top
    assert [p for p in seen if p == str(nas) or p.startswith(str(nas) + "/")] == []
    assert soma_work.git_head(str(b), [])[1] == "main"          # the same repos read fine without the mount


def test_under_is_a_path_boundary_not_a_prefix():
    assert soma_work.under("/mnt/nas", ["/mnt/nas"]) and soma_work.under("/mnt/nas/x", ["/mnt/nas/"])
    assert not soma_work.under("/mnt/nas2", ["/mnt/nas"])
    assert not soma_work.under("/mnt/nas2/x", ["/mnt/nas"])


def test_toplevel_walk_stops_after_max_levels(tmp_path):
    repo = _git(tmp_path / "r")
    ok = repo.joinpath(*["d"] * 39)
    ok.mkdir(parents=True)
    assert soma_work.toplevel(str(ok)) == str(repo)             # 40 levels: the repo itself is the 40th
    assert soma_work.toplevel(str(ok / "d")) is None            # 41: past the limit
