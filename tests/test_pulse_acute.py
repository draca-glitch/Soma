"""Acute events (OOM, ECC) per session, told-state always recorded, and the gaps the 0.10.1
pre-release verification found. Fake /proc trees, temp state dirs, never ~/.claude."""

import json
import math
import os
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS))
import soma_lib  # noqa: E402

SICK = dict(swap_total=2000000, swap_free=0)


def _proc(tmp_path, oom=0, name="p", **kw):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    (d / "meminfo").write_text(
        f"MemTotal: 64000000 kB\nMemAvailable: {kw.get('avail', 36000000)} kB\n"
        f"SwapTotal: {kw.get('swap_total', 0)} kB\nSwapFree: {kw.get('swap_free', 0)} kB\n")
    (d / "loadavg").write_text("1.0 1.0 1.0 1/1 1\n")
    (d / "vmstat").write_text(f"oom_kill {oom}\n")
    p = d / "1"
    p.mkdir(exist_ok=True)
    (p / "statm").write_text("100 10 1 1 0 1 0\n")
    (p / "comm").write_text("init\n")
    return d


def _kw(tmp_path, proc, sd=None):
    return dict(proc_root=str(proc), mounts=[], services=[], hwmon_root=str(tmp_path / "no-hwmon"),
                sys_root=str(tmp_path / "no-sys"), state_dir=str(sd or tmp_path / "state"))


def _pulse(tmp_path, proc, now, sid=None, hold=300, sub=False, sd=None):
    hi = {}
    if sid:
        hi["session_id"] = sid
    if sub:
        hi["agent_id"] = "agent-x"
    return soma_lib.pulse_line(now=now, hook_input=hi or None, hold_s=hold, **_kw(tmp_path, proc, sd))


def _prompt(tmp_path, proc, now, sid=None, mode="pressure", sd=None):
    return soma_lib.line_for_mode(mode, now=now, hook_input={"session_id": sid} if sid else None,
                                  **_kw(tmp_path, proc, sd))


def _sfile(tmp_path, sid, sd=None):
    return Path(sd or tmp_path / "state") / "soma-pulse" / f"{sid}.json"


def _oom(line, n=None):
    return bool(line) and ("oom-kill" in line if n is None else f"oom-kill {n}" in line)


# --- A1: acute events per session --------------------------------------------------

def test_subagent_call_does_not_consume_oom(tmp_path):
    _pulse(tmp_path, _proc(tmp_path, 0), 0, "A")
    p = _proc(tmp_path, 1)
    assert _pulse(tmp_path, p, 10, "A", sub=True) is None
    assert _oom(_pulse(tmp_path, p, 11, "A"))
    assert _pulse(tmp_path, p, 12, "A") is None


def test_two_sessions_each_hear_oom_once(tmp_path):
    p = _proc(tmp_path, 0)
    _pulse(tmp_path, p, 0, "A")
    _pulse(tmp_path, p, 1, "B")
    p = _proc(tmp_path, 1)
    assert _oom(_pulse(tmp_path, p, 10, "A"))
    assert _oom(_pulse(tmp_path, p, 11, "B"))
    assert _pulse(tmp_path, p, 12, "A") is None
    assert _pulse(tmp_path, p, 13, "B") is None


def test_three_kills_across_two_samples_both_announced(tmp_path):
    _pulse(tmp_path, _proc(tmp_path, 0), 0, "A")
    _pulse(tmp_path, _proc(tmp_path, 0), 1, "B")
    first = _pulse(tmp_path, _proc(tmp_path, 1), 10, "A")
    _pulse(tmp_path, _proc(tmp_path, 3), 11, "B")          # B samples between A's two
    second = _pulse(tmp_path, _proc(tmp_path, 3), 12, "A")
    # the late line states this session's own count (2 since its baseline), not the host delta (0)
    assert _oom(first, 1) and _oom(second, 2)


def test_prompt_hook_emits_oom_another_session_already_announced(tmp_path):
    p = _proc(tmp_path, 0)
    _prompt(tmp_path, p, 0, "A")
    _pulse(tmp_path, p, 1, "B")
    p = _proc(tmp_path, 1)
    assert _oom(_pulse(tmp_path, p, 10, "B"))
    assert _oom(_prompt(tmp_path, p, 11, "A"))
    assert _pulse(tmp_path, p, 12, "A") is None             # the prompt line advanced A's baseline


def test_counter_going_backwards_rebaselines_silently(tmp_path):
    _pulse(tmp_path, _proc(tmp_path, 5), 0, "A")
    assert _pulse(tmp_path, _proc(tmp_path, 0), 10, "A") is None   # reboot
    assert json.loads(_sfile(tmp_path, "A").read_text())["counters"] == {"oom_kill": 0}
    assert _oom(_pulse(tmp_path, _proc(tmp_path, 1), 20, "A"))


def test_first_contact_baselines_counters_without_past_events(tmp_path):
    _pulse(tmp_path, _proc(tmp_path, 0), 0, "A")
    p = _proc(tmp_path, 4)
    _pulse(tmp_path, p, 1, "A")
    assert _pulse(tmp_path, p, 2, "NEW") is None             # past kills are not news


def test_no_session_id_stays_host_wide_for_acute(tmp_path):
    _pulse(tmp_path, _proc(tmp_path, 0), 0)
    p = _proc(tmp_path, 1)
    assert _oom(_pulse(tmp_path, p, 10))
    assert _pulse(tmp_path, p, 11) is None
    assert not (tmp_path / "state" / "soma-pulse").exists()


# --- A2: acute flags never held, never seeded ----------------------------------------

def test_second_kill_at_next_sample_is_announced(tmp_path):
    _pulse(tmp_path, _proc(tmp_path, 0), 0, "A")
    assert _oom(_pulse(tmp_path, _proc(tmp_path, 1), 10, "A"))
    assert _oom(_pulse(tmp_path, _proc(tmp_path, 2), 11, "A"))
    assert "OOM" not in json.loads(_sfile(tmp_path, "A").read_text())["held"]


def test_acute_never_in_held_or_seed(tmp_path):
    a, r, h = soma_lib.pulse_transition({}, {"OOM", "SWAP"}, 10, 300)
    assert a == {"OOM", "SWAP"} and "OOM" not in h
    a, r, h = soma_lib.pulse_transition({"OOM": None}, {"OOM"}, 10, 300)
    assert a == {"OOM"} and h == {}
    _pulse(tmp_path, _proc(tmp_path, 0), 0)
    _pulse(tmp_path, _proc(tmp_path, 1), 1)
    st = json.loads((tmp_path / "state" / "soma-state.json").read_text())
    assert "OOM" not in st["pulse_held"]
    assert soma_lib._seed_held({"last_flags": ["OOM", "SWAP"]}) == {"SWAP": None}


# --- A3: the prompt hook always records; first pulse without a file -----------------

def test_silent_prompt_still_writes_session_file(tmp_path):
    ok, sick = _proc(tmp_path, 0), _proc(tmp_path, 0, "sick", **SICK)
    assert _prompt(tmp_path, ok, 0, "B") is None
    doc = json.loads(_sfile(tmp_path, "B").read_text())
    assert doc["held"] == {} and doc["counters"] == {"oom_kill": 0}
    assert _pulse(tmp_path, sick, 1, "A")
    assert _pulse(tmp_path, sick, 2, "B")                   # B was told nothing: it hears it


def test_prompt_off_writes_nothing(tmp_path):
    assert _prompt(tmp_path, _proc(tmp_path, 0), 0, "B", mode="off") is None
    assert not _sfile(tmp_path, "B").exists()


# --- A4: announce only what was recorded --------------------------------------------

@pytest.mark.parametrize("sid", ["A", None])
def test_unwritable_state_dir_announces_nothing(tmp_path, sid):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, so nothing can be created below it")
    sd = blocker / "state"
    sick = _proc(tmp_path, 0, "sick", **SICK)
    for t in range(3):
        assert _pulse(tmp_path, sick, t, sid, sd=sd) is None


# --- A5: no shared temp file -------------------------------------------------------

def test_save_state_uses_pid_unique_temp_and_cleans_up(tmp_path, monkeypatch):
    sd = tmp_path / "s"
    sd.mkdir()
    (sd / "soma-state.json.tmp").write_text("{torn")        # the old shared name is never read
    assert soma_lib.save_state({"a": 1}, str(sd)) is True
    assert soma_lib.load_state(str(sd)) == {"a": 1}
    assert not list(sd.glob(f".soma-state.json.{os.getpid()}.tmp"))

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(soma_lib.os, "replace", boom)
    assert soma_lib.save_state({"a": 2}, str(sd)) is False
    assert not list(sd.glob(".soma-state.json.*.tmp"))
    assert soma_lib.load_state(str(sd)) == {"a": 1}


# --- A6: the prompt line defines what was told -------------------------------------

def test_prompt_line_tells_cleared_flag(tmp_path):
    ok, sick = _proc(tmp_path, 0), _proc(tmp_path, 0, "sick", **SICK)
    _pulse(tmp_path, ok, 0, "A")
    assert _pulse(tmp_path, sick, 1, "A")                   # told SWAP
    assert _prompt(tmp_path, ok, 2, "A", mode="always")     # line without SWAP: told cleared
    assert json.loads(_sfile(tmp_path, "A").read_text())["held"] == {}
    assert _pulse(tmp_path, ok, 400, "A") is None           # no second recovery


# --- A7: junk in a session file ----------------------------------------------------

def test_junk_held_entries_dropped(tmp_path):
    now = 1000.0
    raw = {"BOGUS": None, "SWAP": -math.inf, "LOAD": math.nan, "HOT": now + 301, "DISK": now + 299,
           "LOW_MEM": None, "TOP": True, "SVC": False, "OOM": None, "STEAL": "x", 5: None}
    assert soma_lib._clean_held(raw, now) == {"DISK": now + 299, "LOW_MEM": None}


def test_bogus_key_gives_no_phantom_recovery(tmp_path):
    ok = _proc(tmp_path, 0)
    d = tmp_path / "state" / "soma-pulse"
    d.mkdir(parents=True)
    (d / "A.json").write_text(json.dumps({"held": {"NOT_A_FLAG": 0, "SWAP": "-Infinity"}}))
    (d / "B.json").write_text('{"held": {"SWAP": -Infinity}}')
    assert _pulse(tmp_path, ok, 10, "A", hold=0) is None
    assert _pulse(tmp_path, ok, 10, "B", hold=300) is None


def test_clock_going_backwards_does_not_recover_early():
    a, r, h = soma_lib.pulse_transition({"SWAP": 500.0}, set(), 100.0, 300)
    assert not r and h == {"SWAP": 100.0}                   # future stamp clamped to now
    a, r, h = soma_lib.pulse_transition({"SWAP": 100.0}, set(), 399.0, 300)
    assert not r
    a, r, h = soma_lib.pulse_transition({"SWAP": 100.0}, set(), 400.0, 300)
    assert r == {"SWAP"} and h == {}


def test_held_cap_64_entries():
    raw = {f"K{i}": None for i in range(100)}
    raw.update({"SWAP": None})                               # 101st key, beyond the cap
    assert soma_lib._clean_held(raw, 0) == {}
    assert len(soma_lib._clean_held(dict.fromkeys(soma_lib.KNOWN_FLAGS - soma_lib.ACUTE_FLAGS), 0)) \
        == len(soma_lib.KNOWN_FLAGS - soma_lib.ACUTE_FLAGS)


def test_session_file_over_64k_is_ignored(tmp_path):
    sick = _proc(tmp_path, 0, "sick", **SICK)
    d = tmp_path / "state" / "soma-pulse"
    d.mkdir(parents=True)
    (d / "A.json").write_text(json.dumps({"held": {"SWAP": None}, "pad": "x" * 70000}))
    assert _pulse(tmp_path, sick, 1, "A")                   # oversized = no file = told nothing
    assert _pulse(tmp_path, sick, 2, "A") is None


def test_junk_counters_rebaseline(tmp_path):
    p = _proc(tmp_path, 3)
    d = tmp_path / "state" / "soma-pulse"
    d.mkdir(parents=True)
    (d / "A.json").write_text(json.dumps({"held": {}, "counters": {"oom_kill": True, "x": 1}}))
    assert _pulse(tmp_path, p, 1, "A") is None
    assert json.loads(_sfile(tmp_path, "A").read_text())["counters"] == {"oom_kill": 3}


# --- A8: mixed adapters ------------------------------------------------------------

def test_session_write_leaves_host_pulse_held(tmp_path):
    ok, sick = _proc(tmp_path, 0), _proc(tmp_path, 0, "sick", **SICK)
    _pulse(tmp_path, ok, 0)
    assert _pulse(tmp_path, sick, 1)                        # host-wide adapter told SWAP
    assert _pulse(tmp_path, sick, 2, "A")
    st = json.loads((tmp_path / "state" / "soma-state.json").read_text())
    assert st["pulse_held"] == {"SWAP": None}
    assert _pulse(tmp_path, sick, 3) is None                # host-wide adapter not re-told
