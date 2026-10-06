"""Soma 0.10.1: the pulse reaches the model (additionalContext), holds against flapping,
and delivers per session."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS))
import soma_ctx  # noqa: E402
import soma_lib  # noqa: E402
from test_soma_ctx import _copy_hooks, _quiet_env, _run  # noqa: E402

SICK = dict(swap_total=2000000, swap_free=0)


def _proc(tmp_path, name, **kw):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    (d / "meminfo").write_text(
        f"MemTotal: 64000000 kB\nMemAvailable: {kw.get('avail', 36000000)} kB\n"
        f"SwapTotal: {kw.get('swap_total', 0)} kB\nSwapFree: {kw.get('swap_free', 0)} kB\n")
    (d / "loadavg").write_text("1.0 1.0 1.0 1/1 1\n")
    p = d / "1"
    p.mkdir(exist_ok=True)
    (p / "statm").write_text("100 10 1 1 0 1 0\n")
    (p / "comm").write_text("init\n")
    return d


def _pulse(tmp_path, proc, now, sid=None, hold=None, extra=None):
    hi = {}
    if sid:
        hi["session_id"] = sid
    hi.update(extra or {})
    return soma_lib.pulse_line(
        proc_root=str(proc), mounts=[], services=[], hwmon_root=str(tmp_path / "no-hwmon"),
        sys_root=str(tmp_path / "no-sys"), state_dir=str(tmp_path / "state"), now=now,
        hook_input=hi or None, hold_s=hold)


# --- pure decision ---------------------------------------------------------------

def test_pulse_transition_pure_hold():
    t = soma_lib.pulse_transition
    app, rec, held = t({}, {"HOT"}, 0, 300)
    assert app == {"HOT"} and not rec and held == {"HOT": None}
    app, rec, held = t(held, set(), 10, 300)          # absent, inside hold: nothing
    assert not app and not rec and held == {"HOT": 10}
    app, rec, held = t(held, {"HOT"}, 20, 300)        # reappears inside hold: not a transition
    assert not app and not rec and held == {"HOT": None}
    app, rec, held = t(held, set(), 30, 300)
    app, rec, held = t(held, set(), 329, 300)
    assert not rec
    app, rec, held = t(held, set(), 330, 300)         # absent a full hold, unbroken
    assert rec == {"HOT"} and held == {}


def test_pulse_transition_hold_zero_and_acute():
    t = soma_lib.pulse_transition
    app, rec, held = t({"HOT": None}, set(), 5, 0)
    assert rec == {"HOT"} and held == {}
    app, rec, held = t({}, {"OOM"}, 0, 300)           # acute: announced
    assert app == {"OOM"}
    app, rec, held = t(held, set(), 1, 300)           # acute clearing: never announced, no hold
    assert not app and not rec and held == {}
    app, rec, held = t(held, {"OOM"}, 2, 300)         # every appearance announced
    assert app == {"OOM"}


# --- pulse_line, per session --------------------------------------------------------

def test_flap_inside_window_announces_once(tmp_path):
    ok, sick = _proc(tmp_path, "ok"), _proc(tmp_path, "sick", **SICK)
    assert _pulse(tmp_path, ok, 0, "s1", 300) is None
    assert _pulse(tmp_path, sick, 10, "s1", 300)          # appeared
    for i, p in enumerate([ok, sick, ok, sick, ok]):
        assert _pulse(tmp_path, p, 20 + i * 10, "s1", 300) is None   # flapping, silent
    assert _pulse(tmp_path, ok, 100, "s1", 300) is None
    assert _pulse(tmp_path, ok, 359, "s1", 300) is None   # absent since t=60 (last flap), not yet 300 s
    line = _pulse(tmp_path, ok, 361, "s1", 300)
    assert line and "swap 0" in line
    assert _pulse(tmp_path, ok, 500, "s1", 300) is None


def test_hold_zero_is_old_behaviour(tmp_path):
    ok, sick = _proc(tmp_path, "ok"), _proc(tmp_path, "sick", **SICK)
    assert _pulse(tmp_path, ok, 0, "s1", 0) is None
    assert _pulse(tmp_path, sick, 10, "s1", 0)
    assert _pulse(tmp_path, ok, 20, "s1", 0)              # recovery at once
    assert _pulse(tmp_path, sick, 30, "s1", 0)            # and again


def test_hold_env_default_and_off(tmp_path, monkeypatch):
    ok, sick = _proc(tmp_path, "ok"), _proc(tmp_path, "sick", **SICK)
    assert _pulse(tmp_path, ok, 0, "s") is None
    assert _pulse(tmp_path, sick, 1, "s")
    assert _pulse(tmp_path, ok, 2, "s") is None           # default hold 300 applies
    monkeypatch.setenv("SOMA_PULSE_HOLD_S", "0")
    assert _pulse(tmp_path, sick, 3, "s") is None         # still held, never cleared
    assert soma_lib.pulse_hold_s() == 0
    monkeypatch.setenv("SOMA_PULSE_HOLD_S", "junk")
    assert soma_lib.pulse_hold_s() == 300
    monkeypatch.setenv("SOMA_PULSE_HOLD_S", "-5")
    assert soma_lib.pulse_hold_s() == 0


def test_two_sessions_each_hear_it_once(tmp_path):
    ok, sick = _proc(tmp_path, "ok"), _proc(tmp_path, "sick", **SICK)
    assert _pulse(tmp_path, ok, 0, "A", 300) is None
    assert _pulse(tmp_path, ok, 1, "B", 300) is None
    assert _pulse(tmp_path, sick, 10, "A", 300)
    assert _pulse(tmp_path, sick, 11, "B", 300)           # B was not robbed by A
    assert _pulse(tmp_path, sick, 12, "A", 300) is None
    assert _pulse(tmp_path, sick, 13, "B", 300) is None
    assert (tmp_path / "state" / "soma-pulse" / "A.json").exists()
    assert (tmp_path / "state" / "soma-pulse" / "B.json").exists()


def test_prompt_time_emission_suppresses_pulse_repeat(tmp_path):
    ok, sick = _proc(tmp_path, "ok"), _proc(tmp_path, "sick", **SICK)
    assert _pulse(tmp_path, ok, 0, "A", 300) is None
    line = soma_lib.line_for_mode(
        "pressure", proc_root=str(sick), mounts=[], services=[], hwmon_root=str(tmp_path / "h"),
        sys_root=str(tmp_path / "s"), state_dir=str(tmp_path / "state"), now=5,
        hook_input={"session_id": "A"})
    assert line
    assert _pulse(tmp_path, sick, 6, "A", 300) is None    # the prompt line already said it


def test_no_session_id_falls_back_host_wide(tmp_path):
    ok, sick = _proc(tmp_path, "ok"), _proc(tmp_path, "sick", **SICK)
    assert _pulse(tmp_path, ok, 0, None, 300) is None
    assert _pulse(tmp_path, sick, 10, None, 300)
    assert _pulse(tmp_path, sick, 11, None, 300) is None
    assert _pulse(tmp_path, ok, 12, None, 300) is None    # hold works host-wide too
    assert _pulse(tmp_path, ok, 313, None, 300)
    assert not (tmp_path / "state" / "soma-pulse").exists()


def test_first_contact_without_session_file_is_told_standing_flags_once(tmp_path):
    """Replaces test_new_session_is_not_told_chronic_condition (0.10.1 fix round): a session
    with no file has been told nothing, so it hears a standing condition once, whatever another
    session's samples left in the host-wide state."""
    ok, sick = _proc(tmp_path, "ok"), _proc(tmp_path, "sick", **SICK)
    assert _pulse(tmp_path, ok, 0, "A", 300) is None
    assert _pulse(tmp_path, sick, 1, "A", 300)
    assert "swap" in _pulse(tmp_path, sick, 2, "B", 300)  # B was never told
    assert _pulse(tmp_path, sick, 3, "B", 300) is None    # once


def test_subagent_announces_nothing_and_leaves_told_state(tmp_path):
    ok, sick = _proc(tmp_path, "ok"), _proc(tmp_path, "sick", **SICK)
    assert _pulse(tmp_path, ok, 0, "A", 300) is None
    sub = {"agent_id": "agent-abc123", "agent_type": "Explore"}
    assert _pulse(tmp_path, sick, 10, "A", 300, extra=sub) is None
    st = json.loads((tmp_path / "state" / "soma-state.json").read_text())
    assert st["last_flags"]                                # baseline still persisted
    assert _pulse(tmp_path, sick, 11, "A", 300)            # main agent hears it itself


def test_main_thread_with_agent_type_only_still_announces(tmp_path):
    ok, sick = _proc(tmp_path, "ok"), _proc(tmp_path, "sick", **SICK)
    _pulse(tmp_path, ok, 0, "A", 300)
    assert _pulse(tmp_path, sick, 1, "A", 300, extra={"agent_type": "my-agent"})


# --- hook script ------------------------------------------------------------------

def _env(sd, **over):
    return _quiet_env(sd, **over)


def _seed_session(sd, sid, held, **extra):
    d = sd / "soma-pulse"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.json").write_text(json.dumps(dict({"ts": int(time.time()), "held": held}, **extra)))


def test_hook_json_shape_and_plain(tmp_path):
    sd = tmp_path / "state"
    env = _env(sd, SOMA_PULSE_HOLD_S="300", SOMA_MODE="always")
    # seed each session as told a flag the quiet host does not have: its clearing is the transition
    for sid in ("s1", "s2", "s3"):
        _seed_session(sd, sid, {"SWAP": None})
    r = _run(HOOKS / "soma-pulse.py", json.dumps({"session_id": "s1"}), env)
    assert r.returncode == 0 and r.stderr == ""
    # SWAP is absent on the quiet host: held, not cleared inside 300 s, and no new flag => silent
    assert r.stdout == ""
    env["SOMA_PULSE_HOLD_S"] = "0"
    r = _run(HOOKS / "soma-pulse.py", json.dumps({"session_id": "s2"}), env)
    out = json.loads(r.stdout)
    assert list(out) == ["hookSpecificOutput"]
    hso = out["hookSpecificOutput"]
    assert list(hso) == ["hookEventName", "additionalContext"]
    assert hso["hookEventName"] == "PostToolUse" and hso["additionalContext"].startswith("[system-state] ")
    assert "\n" not in r.stdout.strip()
    env["SOMA_PULSE_FORMAT"] = "plain"
    r = _run(HOOKS / "soma-pulse.py", json.dumps({"session_id": "s3"}), env)
    assert r.stdout.startswith("[system-state] ") and r.stdout.count("\n") == 1


def test_hook_silent_without_transition(tmp_path):
    env = _env(tmp_path / "state")
    r = _run(HOOKS / "soma-pulse.py", json.dumps({"session_id": "s1"}), env)
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""


@pytest.mark.parametrize("stdin", ["", "not json", "[]", "null", '"x"', "[" * 200000, "{" * 5000,
                                   '{"session_id": 5}', '{"session_id": "../../etc/x"}',
                                   '{"session_id": "' + "a" * 100000 + '"}', '{"agent_id": {"a": 1}}',
                                   "\x00\xff"], ids=lambda s: str(abs(hash(s)) % 10**6))
def test_hook_hostile_stdin(tmp_path, stdin):
    sd = tmp_path / "state"
    env = _env(sd)
    r = _run(HOOKS / "soma-pulse.py", stdin, env)
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    assert not (tmp_path / "etc").exists()
    for p in sd.rglob("*"):
        assert ".." not in p.name
    # with a forced flag the output is exactly one JSON object of the documented shape
    # (or nothing, for the one payload that names a subagent)
    sd2 = tmp_path / "state2"
    r = _run(HOOKS / "soma-pulse.py", stdin, _env(sd2, SOMA_MEM_AVAIL_PCT="101"))
    assert r.returncode == 0 and r.stderr == ""
    if stdin == '{"agent_id": {"a": 1}}':
        assert r.stdout == ""
        return
    assert r.stdout.endswith("\n") and r.stdout.count("\n") == 1
    out = json.loads(r.stdout)
    assert list(out) == ["hookSpecificOutput"]
    hso = out["hookSpecificOutput"]
    assert list(hso) == ["hookEventName", "additionalContext"] and hso["hookEventName"] == "PostToolUse"
    assert hso["additionalContext"].startswith("[system-state] ") and "(LOW)" in hso["additionalContext"]
    assert not (tmp_path / "etc").exists()
    for p in sd2.rglob("*"):
        assert ".." not in p.name


def test_hook_survives_missing_soma_ctx_pulse_path(tmp_path):
    hooks = _copy_hooks(tmp_path)                           # no soma_ctx.py
    sd = tmp_path / "state"
    env = _env(sd, SOMA_PULSE_HOLD_S="0", SOMA_MODE="always")
    sd.mkdir()
    (sd / "soma-state.json").write_text(json.dumps({"last_flags": ["FAKE_OLD"]}))
    r = _run(hooks / "soma-pulse.py", json.dumps({"session_id": "s1"}), env)
    assert r.returncode == 0 and r.stderr == ""
    out = json.loads(r.stdout)                              # host-wide fallback still announces
    assert out["hookSpecificOutput"]["additionalContext"].startswith("[system-state]")
    assert not (sd / "soma-pulse").exists()
    assert "pulse_held" in json.loads((sd / "soma-state.json").read_text())


# --- shared per-session helper ----------------------------------------------------

def test_session_json_roundtrip_and_prune(tmp_path):
    sd = str(tmp_path)
    soma_ctx.write_session_json("soma-pulse", "abc", {"x": 1}, sd, now=1000.0)
    assert soma_ctx.read_session_json("soma-pulse", "abc", sd) == {"x": 1}
    assert soma_ctx.read_session_json("soma-pulse", "nope", sd) is None
    assert soma_ctx.read_session_json("soma-pulse", None, sd) is None
    old = tmp_path / "soma-pulse" / "old.json"
    old.write_text("{}")
    os.utime(old, (0, 0))
    soma_ctx.write_session_json("soma-pulse", "abc", {"x": 2}, sd, now=time.time() + 7200)
    assert not old.exists()


def _v0100_soma_ctx():
    try:
        r = subprocess.run(["git", "-C", str(HOOKS.parent), "show", "v0.10.0:hooks/soma_ctx.py"],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    return r.stdout if r.returncode == 0 and r.stdout else None


def test_older_soma_ctx_without_session_helpers(tmp_path):
    """The real v0.10.0 soma_ctx.py (no session store) beside this soma_lib.py: both hooks work,
    the context segment stays, the pulse goes host-wide."""
    old = _v0100_soma_ctx()
    if old is None:
        pytest.skip("git or the v0.10.0 tag unavailable")
    assert "read_session_json" not in old
    hooks = _copy_hooks(tmp_path, old)
    sd = tmp_path / "state"
    sd.mkdir()
    (sd / "soma-state.json").write_text(json.dumps({"last_flags": ["FAKE_OLD"]}))
    env = _env(sd, SOMA_PULSE_HOLD_S="0")
    r = _run(hooks / "soma-pulse.py", json.dumps({"session_id": "s1"}), env)
    assert r.returncode == 0 and r.stderr == ""
    assert "additionalContext" in r.stdout and not (sd / "soma-pulse").exists()
    r = _run(hooks / "soma-state.py", json.dumps({"session_id": "s1"}), dict(env, SOMA_MODE="always"))
    assert r.returncode == 0 and r.stderr == "" and "[system-state]" in r.stdout
    assert not (sd / "soma-pulse").exists()
