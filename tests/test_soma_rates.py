"""Soma 0.12.0 part A: context fill rate, quota projection, model-change notice, cost."""
import json
import subprocess
import sys
import time
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS))
import soma_ctx  # noqa: E402
import soma_lib  # noqa: E402
from test_pulse import _proc  # noqa: E402
from test_soma_ctx import SAMPLE, _quiet_env, _run, _run_writer  # noqa: E402

SID = SAMPLE["session_id"]
N = 1791320000.0
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _hm(t):
    return time.strftime("%H:%M", time.localtime(t))


def _ctxfile(sd):
    return json.loads((Path(sd) / "soma-ctx" / f"{SID}.json").read_text())


def _put(sd, **over):
    doc = {"ts": N, "used_pct": 71, "used_tokens": 710000, "window": 1000000,
           "five_hour": {"used_pct": 7, "resets_at": N + 3600},
           "seven_day": {"used_pct": 19, "resets_at": N + 86400}}
    doc.update(over)
    d = Path(sd) / "soma-ctx"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{SID}.json").write_text(json.dumps(doc))


def _seg(sd, now=N, **hook):
    return soma_ctx.context_segment({"session_id": SID, **hook}, str(sd), now)


def _sl(pct=71, model=None, cost=None, rl=True, sid=SID):
    d = {"session_id": sid, "context_window": {"used_percentage": pct, "context_window_size": 1000000,
                                               "current_usage": {"input_tokens": pct * 10000}}}
    if rl:
        d["rate_limits"] = {"five_hour": {"used_percentage": 7, "resets_at": N + 3600}}
    if model is not None:
        d["model"] = {"id": model, "display_name": "x"}
    if cost is not None:
        d["cost"] = {"total_cost_usd": cost}
    return d


# --- bridge: samples, model, cost ------------------------------------------------

def test_samples_dedup_and_cap(tmp_path):
    soma_ctx.write_from_statusline(_sl(50), str(tmp_path), N)
    soma_ctx.write_from_statusline(_sl(50), str(tmp_path), N + 5)
    s = _ctxfile(tmp_path)["samples"]
    assert s == [{"ts": int(N), "used_tokens": 500000, "used_pct": 50, "five_pct": 7, "seven_pct": None}]
    for i in range(40):
        soma_ctx.write_from_statusline(_sl(i), str(tmp_path), N + 10 + i)
    s = _ctxfile(tmp_path)["samples"]
    assert len(s) == 24 and s[-1]["used_pct"] == 39 and s[0]["used_pct"] == 16


def test_old_format_file_reads_and_upgrades(tmp_path):
    _put(tmp_path)  # 0.10/0.11 file: no samples, no model
    assert _seg(tmp_path) == ("ctx 71% (710k/1000k) · 5h 7% · 7d 19%", False)
    soma_ctx.write_from_statusline(_sl(72, model="m-a"), str(tmp_path), N + 1)
    doc = _ctxfile(tmp_path)
    assert len(doc["samples"]) == 1 and doc["model"] == "m-a" and "model_prev" not in doc


def test_junk_in_every_new_field(tmp_path):
    _put(tmp_path, samples="x", model=5, model_prev=[1], model_changed_ts="no", cost={"a": 1}, rl_seen="yes")
    assert _seg(tmp_path)[0] == "ctx 71% (710k/1000k) · 5h 7% · 7d 19%"
    assert soma_ctx.take_model_notice(SID, str(tmp_path), N) is None
    _put(tmp_path, samples=[1, None, {"ts": "a"}, {"ts": N, "five_pct": "x"}])
    assert _seg(tmp_path)[0] == "ctx 71% (710k/1000k) · 5h 7% · 7d 19%"
    for junk in ({"id": 7}, {"id": "a" * 500}, "m", {"id": "bad\nid"}, {"id": ""}):
        d = _sl(60)
        d["model"], d["cost"] = junk, {"total_cost_usd": "lots"}
        soma_ctx.write_from_statusline(d, str(tmp_path), N + 2)
        doc = _ctxfile(tmp_path)
        assert doc.get("model") is None and doc.get("cost") is None
    r = _run_writer(json.dumps({**_sl(61), "model": [1], "cost": None, "rate_limits": 3}), tmp_path)
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""


def test_model_change_recorded_once(tmp_path):
    soma_ctx.write_from_statusline(_sl(50, model="m-a"), str(tmp_path), N)
    soma_ctx.write_from_statusline(_sl(51, model="m-a"), str(tmp_path), N + 5)
    assert "model_prev" not in _ctxfile(tmp_path)
    soma_ctx.write_from_statusline(_sl(52), str(tmp_path), N + 6)  # no model field: keep the stored one
    assert _ctxfile(tmp_path)["model"] == "m-a"
    soma_ctx.write_from_statusline(_sl(53, model="m-b"), str(tmp_path), N + 60)
    doc = _ctxfile(tmp_path)
    assert (doc["model"], doc["model_prev"], doc["model_changed_ts"]) == ("m-b", "m-a", int(N + 60))
    soma_ctx.write_from_statusline(_sl(54, model="m-b"), str(tmp_path), N + 70)
    assert _ctxfile(tmp_path)["model_changed_ts"] == int(N + 60)


# --- context fill per turn -------------------------------------------------------

def _turn(sd, pct, ts, **hook):
    _put(sd, ts=ts, used_pct=pct, used_tokens=pct * 10000)
    return _seg(sd, now=ts + 1, **hook)[0]


def test_turn_rate_sequence(tmp_path):
    assert _turn(tmp_path, 53, N).startswith("ctx 53% (530k/1000k) ·")
    assert _turn(tmp_path, 59, N + 60).startswith("ctx 59% (590k/1000k) ·")  # one turn: not enough
    assert _turn(tmp_path, 65, N + 120).startswith("ctx 65% (650k/1000k, +6%/turn, ~5 turns left) ·")
    assert _turn(tmp_path, 71, N + 180).startswith("ctx 71% (710k/1000k, +6%/turn, ~4 turns left) ·")
    # mean over the last 3 turns only: 59,65,71,89 -> (6+6+18)/3 = 10
    assert _turn(tmp_path, 89, N + 240).startswith("ctx 89% (890k/1000k, +10%/turn, ~1 turn left)(HIGH)")


def test_turn_rate_only_when_it_says_something(tmp_path):
    for i, p in enumerate((10, 11, 12)):  # slow: 83 turns left
        seg = _turn(tmp_path, p, N + 60 * i)
    assert seg.startswith("ctx 12% (120k/1000k) ·")
    sd = tmp_path / "b"
    for i, p in enumerate((90, 95, 97)):  # at or over the full mark: the level says it all
        seg = _turn(sd, p, N + 60 * i)
    assert seg.startswith("ctx 97% (970k/1000k)(HIGH)")


def test_turn_rate_env_full_pct(tmp_path, monkeypatch):
    monkeypatch.setenv("SOMA_CTX_FULL_PCT", "80")
    for i, p in enumerate((53, 59, 65)):
        seg = _turn(tmp_path, p, N + 60 * i)
    assert "+6%/turn, ~3 turns left" in seg


def test_compaction_resets_turns(tmp_path):
    for i, p in enumerate((53, 59, 65)):
        _turn(tmp_path, p, N + 60 * i)
    assert _turn(tmp_path, 30, N + 200).startswith("ctx 30% (300k/1000k) ·")
    assert _turn(tmp_path, 36, N + 260).startswith("ctx 36% (360k/1000k) ·")
    assert _turn(tmp_path, 62, N + 320).startswith("ctx 62% (620k/1000k, +16%/turn, ~3 turns left)")


def test_same_reading_not_a_turn_and_clock_backwards(tmp_path):
    _turn(tmp_path, 53, N)
    _turn(tmp_path, 59, N + 60)
    assert _turn(tmp_path, 59, N + 60).startswith("ctx 59% (590k/1000k) ·")  # same statusline reading
    assert "+6%/turn" in _turn(tmp_path, 65, N + 120)
    assert _turn(tmp_path, 71, N + 30).startswith("ctx 71% (710k/1000k) ·")  # clock went back: reset
    assert _turn(tmp_path, 77, N + 90).startswith("ctx 77% (770k/1000k) ·")


def test_subagent_records_no_turn(tmp_path):
    _turn(tmp_path, 53, N)
    _turn(tmp_path, 59, N + 60, agent_id="a1")
    _turn(tmp_path, 65, N + 120, agent_id="a1")
    assert not (tmp_path / "soma-turns").exists() or len(
        json.loads((tmp_path / "soma-turns" / f"{SID}.json").read_text())["fills"]) == 1


def test_junk_turns_file(tmp_path):
    d = tmp_path / "soma-turns"
    d.mkdir()
    for junk in ('{"fills": "x"}', '{"fills": [[1], ["a", 2], null]}', "[]", "garbage"):
        (d / f"{SID}.json").write_text(junk)
        assert _turn(tmp_path, 60, N).startswith("ctx 60% (600k/1000k) ·")


# --- quota projection --------------------------------------------------------------

def _samples(*pts, key="five_pct"):
    return [{"ts": int(t), "used_tokens": 1000 * i, "used_pct": 50, "five_pct": None, "seven_pct": None,
             key: p} for i, (t, p) in enumerate(pts)]


def _quota(sd, pct=62, reset=N + 3 * 3600, samples=None, now=N):
    _put(sd, used_pct=40, used_tokens=400000, five_hour={"used_pct": pct, "resets_at": reset}, seven_day=None,
         samples=samples if samples is not None else _samples((N - 1800, 50), (N - 900, 56), (N, 62)))
    return soma_ctx.context_reading({"session_id": SID}, str(sd), now)


def test_projection_before_reset_and_flag(tmp_path):
    r = _quota(tmp_path)  # 12 points per 30 min -> 38 more in 95 min, reset in 3 h
    assert r["seg"] == f"ctx 40% (400k/1000k) · 5h 62% (out ~{_hm(N + 5700)}, resets {_hm(N + 3 * 3600)})"
    assert r["quota"] is True and r["high"] is False


def test_projection_after_reset_is_plain(tmp_path):
    r = _quota(tmp_path, reset=N + 3600)
    assert r["seg"] == "ctx 40% (400k/1000k) · 5h 62%" and r["quota"] is False


def test_projection_doubtful_cases_plain(tmp_path):
    cases = [
        _samples((N - 300, 56), (N, 62)),                       # span under 10 min
        _samples((N, 62)),                                      # one point
        _samples((N - 1800, 50), (N - 900, 64), (N, 62)),       # not monotonic
        _samples((N - 1800, 50), (N - 2000, 56), (N, 62)),      # timestamps out of order
        _samples((N - 3 * 3600, 20), (N - 900, 56), (N, 62)),   # spans the previous reset
        _samples((N - 1800, 62), (N, 62)),                      # flat
        _samples((N - 1800, 50), (N - 900, 56), (N + 4000, 62)),  # a sample from the future
    ]
    for s in cases:
        r = _quota(tmp_path, samples=s)
        assert r["seg"] == "ctx 40% (400k/1000k) · 5h 62%" and r["quota"] is False, s
    r = _quota(tmp_path, reset=None)
    assert r["seg"] == "ctx 40% (400k/1000k) · 5h 62%" and r["quota"] is False


def test_projection_below_half_has_no_flag(tmp_path):
    r = _quota(tmp_path, pct=30, samples=_samples((N - 1800, 10), (N - 900, 20), (N, 30)))
    assert "5h 30% (out ~" in r["seg"] and r["quota"] is False


def test_quota_off_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("SOMA_QUOTA", "0")
    r = _quota(tmp_path)
    assert r["seg"] == "ctx 40% (400k/1000k) · 5h 62%" and r["quota"] is False


def test_projection_seven_day_weekday(tmp_path):
    reset = N + 4 * 86400
    _put(tmp_path, used_pct=40, used_tokens=400000, five_hour=None, seven_day={"used_pct": 70, "resets_at": reset},
         samples=_samples((N - 86400, 40), (N - 43200, 55), (N, 70), key="seven_pct"))
    r = soma_ctx.context_reading({"session_id": SID}, str(tmp_path), N)
    out = N + 86400  # 30 points per day, 30 left
    w = lambda t: DAYS[time.localtime(t).tm_wday] + " " + _hm(t)  # noqa: E731
    assert r["seg"] == f"ctx 40% (400k/1000k) · 7d 70% (out ~{w(out)}, resets {w(reset)})" and r["quota"]


# --- cost ----------------------------------------------------------------------------

def test_cost_only_without_rate_limits(tmp_path):
    soma_ctx.write_from_statusline(_sl(40, cost=41.2, rl=False), str(tmp_path), N)
    assert _seg(tmp_path, now=N + 1)[0] == "ctx 40% (400k/1000k) · cost $41.20"
    sd = tmp_path / "sub"
    soma_ctx.write_from_statusline(_sl(40, cost=41.2), str(sd), N)
    assert _seg(sd, now=N + 1)[0] == "ctx 40% (400k/1000k) · 5h 7%"
    soma_ctx.write_from_statusline(_sl(41, cost=42.0, rl=False), str(sd), N + 2)  # rate_limits seen once: never cost
    assert "cost" not in _seg(sd, now=N + 3)[0]


# --- the hooks: QUOTA flag, model notice -------------------------------------------

def _prompt(tmp_path, now, extra=None, mode="pressure"):
    return soma_lib.line_for_mode(
        mode, proc_root=str(_proc(tmp_path, "p")), mounts=[], services=[], hwmon_root=str(tmp_path / "no-hwmon"),
        sys_root=str(tmp_path / "no-sys"), state_dir=str(tmp_path / "state"), now=now,
        hook_input={"session_id": SID, **(extra or {})})


def _pulse(tmp_path, now, extra=None):
    return soma_lib.pulse_line(
        proc_root=str(_proc(tmp_path, "p")), mounts=[], services=[], hwmon_root=str(tmp_path / "no-hwmon"),
        sys_root=str(tmp_path / "no-sys"), state_dir=str(tmp_path / "state"), now=now,
        hook_input={"session_id": SID, **(extra or {})})


def _log(tmp_path):
    f = tmp_path / "state" / "soma-log.jsonl"
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


def test_quota_forces_line_logged_not_in_last_flags(tmp_path):
    _quota(tmp_path / "state")
    line = _prompt(tmp_path, N)
    assert line and "5h 62% (out ~" in line
    assert "QUOTA" in _log(tmp_path)[-1]["flags"]
    st = json.loads((tmp_path / "state" / "soma-state.json").read_text())
    assert "QUOTA" not in st["last_flags"]


def test_model_notice_said_once(tmp_path):
    sd = tmp_path / "state"
    soma_ctx.write_from_statusline(_sl(30, model="claude-fable-5-1"), str(sd), N - 600)
    soma_ctx.write_from_statusline(_sl(31, model="claude-opus-5-5"), str(sd), N - 60)
    assert _prompt(tmp_path, N, {"agent_id": "a1"}) is None  # a subagent never consumes it
    line = _prompt(tmp_path, N)
    assert line.endswith(f" · model claude-opus-5-5 (was claude-fable-5-1 until {_hm(N - 60)})")
    assert "MODEL" in _log(tmp_path)[-1]["flags"]
    assert _prompt(tmp_path, N + 1) is None and _pulse(tmp_path, N + 2) is None


def test_model_notice_through_pulse_once(tmp_path):
    sd = tmp_path / "state"
    soma_ctx.write_from_statusline(_sl(30, model="m-a"), str(sd), N - 600)
    soma_ctx.write_from_statusline(_sl(31, model="m-b"), str(sd), N - 60)
    assert _pulse(tmp_path, N, {"agent_id": "a1"}) is None
    assert _pulse(tmp_path, N).endswith(" · model m-b (was m-a until " + _hm(N - 60) + ")")
    assert _pulse(tmp_path, N + 1) is None and _prompt(tmp_path, N + 2) is None


def test_no_model_change_no_segment(tmp_path):
    sd = tmp_path / "state"
    soma_ctx.write_from_statusline(_sl(30, model="m-a"), str(sd), N - 60)
    assert _prompt(tmp_path, N) is None
    assert "model" not in _prompt(tmp_path, N + 1, mode="always")


def test_hooks_with_v011_soma_ctx_end_to_end(tmp_path):
    """A deployment with the 0.11.0 soma_ctx.py still gives the plain line through both hooks."""
    d = tmp_path / "hooks"
    d.mkdir()
    for p in HOOKS.glob("*.py"):
        (d / p.name).write_text(p.read_text())
    old = subprocess.run(["git", "-C", str(HOOKS.parent), "show", "v0.11.0:hooks/soma_ctx.py"],
                         capture_output=True, text=True)
    assert old.returncode == 0
    (d / "soma_ctx.py").write_text(old.stdout)
    sd = tmp_path / "state"
    _put(sd, ts=time.time(), model="m-b", model_prev="m-a", model_changed_ts=time.time())
    hi = json.dumps({"session_id": SID})
    r = _run(d / "soma-state.py", hi, _quiet_env(sd, SOMA_MODE="always"))
    assert r.returncode == 0 and r.stdout.startswith("[system-state]") and "ctx 71%" in r.stdout
    assert "model" not in r.stdout
    r = _run(d / "soma-pulse.py", hi, _quiet_env(sd))
    assert r.returncode == 0 and "model" not in r.stdout


def test_compaction_inside_a_turn_resets(tmp_path):
    """A compaction followed by regrowth past the old fill looks like a small delta: not a rate."""
    for i, p in enumerate((53, 59, 65)):
        _turn(tmp_path, p, N + 60 * i)
    d = tmp_path / "soma-compact"
    d.mkdir()
    (d / f"{SID}.json").write_text(json.dumps({"ts": N + 150, "count": 1, "announced": True}))
    assert _turn(tmp_path, 71, N + 180).startswith("ctx 71% (710k/1000k) ·")
    assert _turn(tmp_path, 77, N + 240).startswith("ctx 77% (770k/1000k) ·")
    assert "+6%/turn" in _turn(tmp_path, 83, N + 300)
    sd = tmp_path / "b"  # no compaction hook, but the bridge's samples saw the drop
    for i, p in enumerate((53, 59, 65)):
        _turn(sd, p, N + 60 * i)
    _put(sd, ts=N + 180, used_pct=71, used_tokens=710000,
         samples=[{"ts": int(N + 130), "used_tokens": 680000}, {"ts": int(N + 150), "used_tokens": 200000},
                  {"ts": int(N + 180), "used_tokens": 710000}])
    assert _seg(sd, now=N + 181)[0].startswith("ctx 71% (710k/1000k) ·")


def test_projection_counts_quiet_time_since_last_sample(tmp_path):
    """No change for 50 min since the last sample: over the whole span it no longer runs out."""
    s = _samples((N - 3600, 50), (N - 3000, 56))  # alone: out ~N+1400, before the reset
    r = _quota(tmp_path, pct=56, reset=N + 1800, samples=s)
    assert r["seg"] == "ctx 40% (400k/1000k) · 5h 56%" and r["quota"] is False


def test_pulse_takes_model_notice_only_when_told_state_written(tmp_path, monkeypatch):
    sd = tmp_path / "state"
    soma_ctx.write_from_statusline(_sl(30, model="m-a"), str(sd), N - 600)
    soma_ctx.write_from_statusline(_sl(31, model="m-b"), str(sd), N - 60)
    monkeypatch.setattr(soma_lib, "_write_session", lambda *a, **k: False)
    assert _pulse(tmp_path, N) is None
    monkeypatch.undo()
    assert "model m-b (was m-a" in _pulse(tmp_path, N + 1)


def test_second_session_and_old_change_and_clock(tmp_path):
    sd = tmp_path / "state"
    other = "aaaa-bbbb"
    soma_ctx.write_from_statusline(_sl(30, model="m-a"), str(sd), N - 600)
    soma_ctx.write_from_statusline(_sl(31, model="m-b"), str(sd), N - 60)
    soma_ctx.write_from_statusline(_sl(30, model="m-b", sid=other), str(sd), N - 60)
    assert soma_ctx.take_model_notice(other, str(sd), N) is None   # its own file: no change there
    assert soma_ctx.take_model_notice(SID, str(sd), N - 3600) is None  # change "after" now: clock went back
    assert soma_ctx.take_model_notice(SID, str(sd), N + 2 * 86400) is None  # older than a day (and stale)
    assert soma_ctx.take_model_notice(SID, str(sd), N) is not None
    assert soma_ctx.take_model_notice(SID, str(sd), N) is None
    soma_ctx.write_from_statusline(_sl(32, model="m-a"), str(sd), N + 60)  # switched back: a new change
    assert soma_ctx.take_model_notice(SID, str(sd), N + 61) == f"model m-a (was m-b until {_hm(N + 60)})"


def test_turns_are_per_session(tmp_path):
    for i, p in enumerate((53, 59, 65)):
        _turn(tmp_path, p, N + 60 * i)
    other = "aaaa-bbbb"
    d = tmp_path / "soma-ctx"
    (d / f"{other}.json").write_text(json.dumps({"ts": N + 200, "used_pct": 71, "used_tokens": 710000,
                                                 "window": 1000000}))
    assert soma_ctx.context_segment({"session_id": other}, str(tmp_path), N + 201)[0] == "ctx 71% (710k/1000k)"


def test_off_switch_ctx_silences_new_parts(tmp_path, monkeypatch):
    sd = tmp_path / "state"
    soma_ctx.write_from_statusline(_sl(30, model="m-a"), str(sd), N - 600)
    soma_ctx.write_from_statusline(_sl(31, model="m-b"), str(sd), N - 60)
    monkeypatch.setenv("SOMA_CTX", "off")
    assert soma_ctx.take_model_notice(SID, str(sd), N) is None
    assert _prompt(tmp_path, N) is None
