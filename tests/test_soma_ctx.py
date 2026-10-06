import json
import os
import subprocess
import sys
import time

import pytest
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS))
import soma_ctx  # noqa: E402
import soma_lib  # noqa: E402

WRITER = HOOKS / "soma-context.py"

SAMPLE = {
    "session_id": "3f2a9c1e-77aa-4b1d-9e0f-0123456789ab",
    "transcript_path": "/nonexistent/x.jsonl",
    "context_window": {
        "total_input_tokens": 865627, "context_window_size": 1000000,
        "current_usage": {"input_tokens": 2, "output_tokens": 16,
                          "cache_creation_input_tokens": 1135, "cache_read_input_tokens": 864490},
        "used_percentage": 87, "remaining_percentage": 13},
    "rate_limits": {"five_hour": {"used_percentage": 7, "resets_at": 1791327000},
                    "seven_day": {"used_percentage": 19, "resets_at": 1791723600}},
}


def _run_writer(stdin: str, state_dir: Path):
    env = dict(os.environ, SOMA_STATE_DIR=str(state_dir))
    return subprocess.run([sys.executable, str(WRITER)], input=stdin, capture_output=True,
                          text=True, env=env, timeout=10)


def _files(state_dir: Path) -> list:
    d = state_dir / "soma-ctx"
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


# --- writer -----------------------------------------------------------------

def test_writer_sample(tmp_path):
    r = _run_writer(json.dumps(SAMPLE), tmp_path)
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    doc = json.loads((tmp_path / "soma-ctx" / f"{SAMPLE['session_id']}.json").read_text())
    assert doc["used_pct"] == 87
    assert doc["used_tokens"] == 865627
    assert doc["window"] == 1000000
    assert doc["five_hour"] == {"used_pct": 7, "resets_at": 1791327000}
    assert doc["seven_day"] == {"used_pct": 19, "resets_at": 1791723600}
    assert abs(doc["ts"] - time.time()) < 60
    assert set(doc) == {"ts", "used_pct", "used_tokens", "window", "five_hour", "seven_day"}


def test_writer_silent_on_bad_input(tmp_path):
    for stdin in ("{}", "", "not json {", "[1,2]", "null", '{"session_id": 5}'):
        r = _run_writer(stdin, tmp_path)
        assert r.returncode == 0 and r.stdout == "" and r.stderr == "", stdin
    assert [n for n in _files(tmp_path) if n.endswith(".json")] == []


def test_writer_float_pct_and_missing_parts(tmp_path):
    doc = {"session_id": "s1", "context_window": {"used_percentage": 42.9, "context_window_size": 200000,
                                                  "current_usage": None}}
    r = _run_writer(json.dumps(doc), tmp_path)
    assert r.returncode == 0 and r.stdout == ""
    out = json.loads((tmp_path / "soma-ctx" / "s1.json").read_text())
    assert out["used_pct"] == 42 and out["used_tokens"] is None and out["window"] == 200000
    assert out["five_hour"] is None and out["seven_day"] is None


def test_writer_float_rate_limit(tmp_path):
    doc = {"session_id": "s2", "rate_limits": {"five_hour": {"used_percentage": 7.8, "resets_at": 1.5e9}}}
    _run_writer(json.dumps(doc), tmp_path)
    out = json.loads((tmp_path / "soma-ctx" / "s2.json").read_text())
    assert out["five_hour"] == {"used_pct": 7, "resets_at": 1500000000}
    assert out["used_pct"] is None


def test_writer_hostile_session_id(tmp_path):
    for sid in ("../../etc/passwd", "a/b\\c", "..", "/abs/path", "x\x00y"):
        r = _run_writer(json.dumps(dict(SAMPLE, session_id=sid)), tmp_path)
        assert r.returncode == 0 and r.stdout == "" and r.stderr == "", sid
    made = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
    assert made == sorted(f"soma-ctx/{n}" for n in
                          (".pruned", "abspath.json", "abc.json", "etcpasswd.json", "xy.json")), made
    assert soma_ctx.safe_id("../../etc/passwd") == "etcpasswd"
    assert soma_ctx.safe_id("..") is None
    assert soma_ctx.safe_id("") is None
    assert soma_ctx.safe_id(None) is None
    assert soma_ctx.safe_id(SAMPLE["session_id"]) == SAMPLE["session_id"]


def test_writer_long_session_id_still_writes(tmp_path):
    assert len(soma_ctx.safe_id("a" * 1000)) == 128
    r = _run_writer(json.dumps(dict(SAMPLE, session_id="a" * 1000)), tmp_path)
    assert r.returncode == 0 and r.stderr == ""
    assert [n for n in _files(tmp_path) if n.endswith(".json")] == ["a" * 128 + ".json"]


def test_writer_atomic(tmp_path, monkeypatch):
    soma_ctx.write_from_statusline(dict(SAMPLE, context_window=dict(SAMPLE["context_window"], used_percentage=11)),
                                   str(tmp_path))
    seen = {}
    real_replace = os.replace

    def spy(src, dst):
        seen["src"], seen["dst"] = Path(src), Path(dst)
        seen["dst_existed"] = Path(dst).exists()
        # a reader at this instant sees the complete OLD document, never a partial one
        seen["dst_doc"] = json.loads(Path(dst).read_text())
        seen["src_doc"] = json.loads(Path(src).read_text())
        real_replace(src, dst)

    monkeypatch.setattr(soma_ctx.os, "replace", spy)
    soma_ctx.write_from_statusline(SAMPLE, str(tmp_path))
    assert seen["src"].parent == seen["dst"].parent
    assert seen["dst_existed"] is True
    assert seen["dst_doc"]["used_pct"] == 11
    assert seen["src_doc"]["used_pct"] == 87
    assert not seen["src"].exists()
    assert json.loads(seen["dst"].read_text())["used_pct"] == 87


def test_writer_failed_write_leaves_nothing(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(soma_ctx.os, "replace", boom)
    soma_ctx.write_from_statusline(SAMPLE, str(tmp_path))  # must not raise
    assert _files(tmp_path) == [] or all(not n.endswith(".json") and not n.endswith(".tmp")
                                         for n in _files(tmp_path))


def test_writer_prunes_old_files(tmp_path):
    d = tmp_path / "soma-ctx"
    d.mkdir()
    old, young = d / "dead-session.json", d / "live-session.json"
    old.write_text("{}")
    young.write_text("{}")
    past = time.time() - 10 * 86400
    os.utime(old, (past, past))
    soma_ctx.write_from_statusline(SAMPLE, str(tmp_path))
    assert not old.exists() and young.exists()


# --- reader -----------------------------------------------------------------

def _write(tmp_path, sid=SAMPLE["session_id"], **over):
    doc = {"ts": time.time(), "used_pct": 87, "used_tokens": 865627, "window": 1000000,
           "five_hour": {"used_pct": 7, "resets_at": time.time() + 3600},
           "seven_day": {"used_pct": 19, "resets_at": time.time() + 86400}}
    doc.update(over)
    d = tmp_path / "soma-ctx"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.json").write_text(json.dumps(doc))


def _hook(sid=SAMPLE["session_id"], transcript=None):
    return {"session_id": sid, "transcript_path": str(transcript) if transcript else None, "prompt": "hi"}


def test_reader_fresh_file(tmp_path):
    _write(tmp_path)
    seg, high = soma_ctx.context_segment(_hook(), str(tmp_path))
    assert seg == "ctx 87% (866k/1000k)(HIGH) · 5h 7% · 7d 19%"
    assert high is True


def test_reader_below_threshold_and_at_threshold(tmp_path):
    _write(tmp_path, used_pct=84)
    seg, high = soma_ctx.context_segment(_hook(), str(tmp_path))
    assert seg.startswith("ctx 84% (866k/1000k) ·") and high is False
    _write(tmp_path, used_pct=85)
    seg, high = soma_ctx.context_segment(_hook(), str(tmp_path))
    assert seg.startswith("ctx 85% (866k/1000k)(HIGH)") and high is True


def test_reader_threshold_env(tmp_path, monkeypatch):
    _write(tmp_path, used_pct=60)
    monkeypatch.setenv("SOMA_CTX_PCT", "50")
    assert soma_ctx.context_segment(_hook(), str(tmp_path))[1] is True
    monkeypatch.setenv("SOMA_CTX_PCT", "0")
    seg, high = soma_ctx.context_segment(_hook(), str(tmp_path))
    assert high is False and "(HIGH)" not in seg


def test_reader_no_rate_limits(tmp_path):
    _write(tmp_path, five_hour=None, seven_day=None)
    assert soma_ctx.context_segment(_hook(), str(tmp_path))[0] == "ctx 87% (866k/1000k)(HIGH)"


def test_reader_drops_expired_rate_window(tmp_path):
    _write(tmp_path, five_hour={"used_pct": 93, "resets_at": time.time() - 60})
    seg, _ = soma_ctx.context_segment(_hook(), str(tmp_path))
    assert "5h" not in seg and "7d 19%" in seg


def test_reader_stale_file_falls_back(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text(_usage_line(2, 2112, 870139) + "\n")
    _write(tmp_path, ts=time.time() - 2 * 86400)
    # the stale state file is ignored entirely: no 87%, no rate windows, only the transcript tokens
    assert soma_ctx.context_segment(_hook(transcript=t), str(tmp_path)) == ("ctx 872k", False)


def test_reader_other_session(tmp_path):
    _write(tmp_path, sid="another-session")
    assert soma_ctx.context_segment(_hook(), str(tmp_path)) == (None, False)


def test_reader_corrupt_file(tmp_path):
    d = tmp_path / "soma-ctx"
    d.mkdir()
    for junk in ("{not json", "[]", '{"ts": "yesterday", "used_pct": "lots"}', ""):
        (d / f"{SAMPLE['session_id']}.json").write_text(junk)
        assert soma_ctx.context_segment(_hook(), str(tmp_path)) == (None, False), junk


def test_reader_no_session_id(tmp_path):
    _write(tmp_path)
    assert soma_ctx.context_segment({}, str(tmp_path)) == (None, False)
    assert soma_ctx.context_segment(None, str(tmp_path)) == (None, False)


def test_reader_off_switch(tmp_path, monkeypatch):
    _write(tmp_path)
    monkeypatch.setenv("SOMA_CTX", "0")
    assert soma_ctx.context_segment(_hook(), str(tmp_path)) == (None, False)


# --- transcript fallback -------------------------------------------------------

def _usage_line(inp, cc, cr, sidechain=False):
    return json.dumps({"type": "assistant", "isSidechain": sidechain, "message": {
        "role": "assistant", "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": inp, "cache_creation_input_tokens": cc,
                  "cache_read_input_tokens": cr, "output_tokens": 981}}})


def _big_line(n):
    return json.dumps({"type": "user", "message": {"role": "user", "content": "x" * n}})


def test_transcript_last_usage_behind_large_lines(tmp_path):
    t = tmp_path / "t.jsonl"
    lines = [_usage_line(1, 1, 1), _usage_line(2, 2112, 870139)]
    lines += [_big_line(300_000) for _ in range(4)]
    lines.append(_usage_line(0, 0, 0))  # synthetic zero entry is skipped
    lines.append(_usage_line(5, 5, 5, sidechain=True))
    lines.append(json.dumps({"type": "system", "note": "mentions \"usage\" but is not one"}))
    t.write_text("\n".join(lines) + "\n")
    assert soma_ctx.transcript_tokens(str(t)) == 872253
    seg, high = soma_ctx.context_segment(_hook(transcript=t), str(tmp_path))
    assert seg == "ctx 872k" and high is False


def test_transcript_no_trailing_newline(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text(_big_line(1000) + "\n" + _usage_line(10, 20, 30))
    assert soma_ctx.transcript_tokens(str(t)) == 60


def test_transcript_empty_and_missing(tmp_path):
    t = tmp_path / "empty.jsonl"
    t.write_text("")
    assert soma_ctx.transcript_tokens(str(t)) is None
    assert soma_ctx.transcript_tokens(str(tmp_path / "missing.jsonl")) is None
    assert soma_ctx.transcript_tokens(None) is None
    assert soma_ctx.context_segment(_hook(transcript=t), str(tmp_path)) == (None, False)
    assert soma_ctx.context_segment(_hook(transcript=tmp_path / "nope"), str(tmp_path)) == (None, False)


def test_transcript_scan_is_bounded(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text(_usage_line(1, 2, 3) + "\n" + _big_line(soma_ctx.TAIL_MAX_BYTES + 1000) + "\n")
    assert soma_ctx.transcript_tokens(str(t)) is None


def test_state_file_preferred_over_transcript(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text(_usage_line(1, 1, 1) + "\n")
    _write(tmp_path)
    assert soma_ctx.context_segment(_hook(transcript=t), str(tmp_path))[0] == \
        "ctx 87% (866k/1000k)(HIGH) · 5h 7% · 7d 19%"


# --- integration with the [system-state] line ------------------------------------

def _fake_proc(tmp_path):
    (tmp_path / "meminfo").write_text("MemTotal: 64000000 kB\nMemAvailable: 36000000 kB\n"
                                      "SwapTotal: 0 kB\nSwapFree: 0 kB\n")
    (tmp_path / "loadavg").write_text("1.0 1.0 1.0 1/1 1\n")
    d = tmp_path / "1"
    d.mkdir()
    (d / "statm").write_text("100 10 1 1 0 1 0\n")
    (d / "comm").write_text("init\n")
    return tmp_path


def _lfm(mode, proc, hook_input=None):
    return soma_lib.line_for_mode(
        mode, proc_root=str(proc), mounts=[], services=[],
        hwmon_root=str(proc / "no-hwmon"), sys_root=str(proc / "no-sys"),
        state_dir=str(proc / "state"), hook_input=hook_input)


def test_line_always_appends_segment(tmp_path):
    proc = _fake_proc(tmp_path)
    _write(proc / "state", used_pct=40)
    line = _lfm("always", proc, _hook())
    assert line.endswith(" · ctx 40% (866k/1000k) · 5h 7% · 7d 19%")


def test_line_pressure_silent_below_threshold(tmp_path):
    proc = _fake_proc(tmp_path)
    _write(proc / "state", used_pct=40)
    assert _lfm("pressure", proc, _hook()) is None


def test_line_pressure_emits_on_high_ctx(tmp_path):
    proc = _fake_proc(tmp_path)
    _write(proc / "state", used_pct=90)
    line = _lfm("pressure", proc, _hook())
    assert line and "ctx 90% (866k/1000k)(HIGH)" in line
    # CTX is not a body flag: it must not leak into the pulse hook's transition baseline
    doc = json.loads((proc / "state" / "soma-state.json").read_text())
    assert "CTX" not in doc["last_flags"]
    log = (proc / "state" / "soma-log.jsonl").read_text().strip().splitlines()
    assert "CTX" in json.loads(log[-1])["flags"]


def test_line_unchanged_without_context_data(tmp_path):
    proc = _fake_proc(tmp_path)
    base = _lfm("always", proc)
    with_hook = _lfm("always", proc, _hook())
    assert base == with_hook and "ctx" not in base


def test_line_off_switch(tmp_path, monkeypatch):
    proc = _fake_proc(tmp_path)
    _write(proc / "state", used_pct=95)
    monkeypatch.setenv("SOMA_CTX", "0")
    assert _lfm("pressure", proc, _hook()) is None
    assert "ctx" not in _lfm("always", proc, _hook())


def test_hook_script_end_to_end(tmp_path):
    proc = _fake_proc(tmp_path)
    _write(proc / "state", used_pct=91)
    env = dict(os.environ, SOMA_STATE_DIR=str(proc / "state"), SOMA_MODE="always",
               SOMA_MOUNTS="", SOMA_LOG="0")
    r = subprocess.run([sys.executable, str(HOOKS / "soma-state.py")], input=json.dumps(_hook()),
                       capture_output=True, text=True, env=env, timeout=10)
    assert r.returncode == 0 and "ctx 91% (866k/1000k)(HIGH)" in r.stdout
    r = subprocess.run([sys.executable, str(HOOKS / "soma-state.py")], input="not json",
                       capture_output=True, text=True, env=env, timeout=10)
    assert r.returncode == 0 and r.stdout.startswith("[system-state]") and "ctx" not in r.stdout


# --- pre-release verification round: guarded import, bounds, gaps ---------------------

def _copy_hooks(tmp_path, ctx_source=None):
    """A copy of the hooks without soma_ctx.py (or with ctx_source as its content)."""
    d = tmp_path / "hooks"
    d.mkdir()
    for p in HOOKS.glob("*.py"):
        if p.name != "soma_ctx.py":
            (d / p.name).write_text(p.read_text())
    if ctx_source is not None:
        (d / "soma_ctx.py").write_text(ctx_source)
    return d


def _quiet_env(state_dir, **over):
    env = dict(os.environ, SOMA_STATE_DIR=str(state_dir), SOMA_MOUNTS="", SOMA_SERVICES="", SOMA_LOG="0",
               SOMA_MEM_AVAIL_PCT="0", SOMA_SWAP_MB="999999999", SOMA_DISK_PCT="1000",
               SOMA_LOAD_RATIO="1000000", SOMA_TOP_RSS_PCT="0", SOMA_PSI_PCT="0", SOMA_MEM_TTE_H="0",
               SOMA_DISK_TTF_H="0", SOMA_TOP_GROWTH_GBH="0", SOMA_SELF_RSS_PCT="0", SOMA_STEAL_PCT="0",
               SOMA_TEMP_CPU="0", SOMA_TEMP_DISK="0", SOMA_TEMP_GPU="0", SOMA_TEMP_RAM="0",
               SOMA_TEMP_BOARD="0", SOMA_TEMP_WIFI="0", SOMA_TEMP_ACPI="0")
    env.pop("CLAUDE_KIT_STATE_DIR", None)
    env.update(over)
    return env


def _run(script, stdin, env, timeout=10):
    return subprocess.run([sys.executable, str(script)], input=stdin, capture_output=True, text=True,
                          env=env, timeout=timeout)


@pytest.mark.parametrize("ctx_source", [None, "raise RuntimeError('boom at import')\n",
                                        "def _oops(:\n", "state_dir = None\n"])
def test_hooks_survive_missing_or_broken_soma_ctx(tmp_path, ctx_source):
    hooks = _copy_hooks(tmp_path, ctx_source)
    sd = tmp_path / "state"
    env = _quiet_env(sd, SOMA_MODE="always")
    r = _run(hooks / "soma-state.py", json.dumps(_hook()), env)
    assert r.returncode == 0 and r.stderr == "", r.stderr
    assert r.stdout.startswith("[system-state] mem ") and "ctx" not in r.stdout
    assert (sd / "soma-state.json").exists()  # state dir resolved by SOMA_STATE_DIR as before
    r = _run(hooks / "soma-pulse.py", "{}", env)
    assert r.returncode == 0 and r.stderr == "", r.stderr


def test_fallback_state_dir_chain_without_soma_ctx(tmp_path):
    hooks = _copy_hooks(tmp_path)
    kit = tmp_path / "kit"
    env = _quiet_env(tmp_path / "unused", SOMA_MODE="always")
    env.pop("SOMA_STATE_DIR")
    env["CLAUDE_KIT_STATE_DIR"] = str(kit)
    r = _run(hooks / "soma-state.py", "{}", env)
    assert r.returncode == 0 and (kit / "soma-state.json").exists()
    home = tmp_path / "home"
    env = _quiet_env(tmp_path / "unused", SOMA_MODE="always", HOME=str(home))
    env.pop("SOMA_STATE_DIR")
    r = _run(hooks / "soma-state.py", "{}", env)
    assert r.returncode == 0 and (home / ".claude" / "state" / "soma-state.json").exists()


def test_hook_survives_deeply_nested_json(tmp_path):
    env = _quiet_env(tmp_path / "state", SOMA_MODE="always")
    r = _run(HOOKS / "soma-state.py", "[" * 200000, env)
    assert r.returncode == 0 and r.stdout.startswith("[system-state]") and r.stderr == ""


def test_transcript_fifo_does_not_block(tmp_path):
    fifo = tmp_path / "fifo.jsonl"
    os.mkfifo(fifo)
    code = ("import sys; sys.path.insert(0, %r); import soma_ctx; print(soma_ctx.transcript_tokens(%r))"
            % (str(HOOKS), str(fifo)))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=5)
    assert r.returncode == 0 and r.stdout.strip() == "None"


def test_transcript_symlink_to_regular_file_ok(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text(_usage_line(1, 2, 3) + "\n")
    link = tmp_path / "link.jsonl"
    link.symlink_to(t)
    assert soma_ctx.transcript_tokens(str(link)) == 6
    assert soma_ctx.transcript_tokens(str(tmp_path)) is None  # a directory


def test_writer_rejects_out_of_range(tmp_path):
    for bad in (1e308, -5, 101, 150):
        soma_ctx.write_from_statusline({"session_id": "r", "context_window": {"used_percentage": bad}}, str(tmp_path))
        assert _files(tmp_path) == [], bad
    soma_ctx.write_from_statusline({
        "session_id": "r", "context_window": {"used_percentage": 100, "context_window_size": -1,
                                              "current_usage": {"input_tokens": -50}},
        "rate_limits": {"five_hour": {"used_percentage": -3}, "seven_day": {"used_percentage": 1001}}},
        str(tmp_path))
    doc = json.loads((tmp_path / "soma-ctx" / "r.json").read_text())
    assert doc["used_pct"] == 100 and doc["window"] is None and doc["used_tokens"] is None
    assert doc["five_hour"] is None and doc["seven_day"] is None
    soma_ctx.write_from_statusline({"session_id": "r", "rate_limits": {"five_hour": {"used_percentage": 250}}},
                                   str(tmp_path))
    assert json.loads((tmp_path / "soma-ctx" / "r.json").read_text())["five_hour"] == \
        {"used_pct": 250, "resets_at": None}


def test_reader_rejects_out_of_range(tmp_path):
    _write(tmp_path, used_pct=150, used_tokens=None, window=None, five_hour={"used_pct": -3, "resets_at": None},
           seven_day={"used_pct": 1500, "resets_at": None})
    assert soma_ctx.context_segment(_hook(), str(tmp_path)) == (None, False)
    _write(tmp_path, used_pct=-5, used_tokens=-10, window=-100, five_hour={"used_pct": 250, "resets_at": None},
           seven_day=None)
    assert soma_ctx.context_segment(_hook(), str(tmp_path)) == ("5h 250%", False)


def test_resets_at_epoch_milliseconds(tmp_path):
    soma_ctx.write_from_statusline({"session_id": "m", "rate_limits": {
        "five_hour": {"used_percentage": 7, "resets_at": 1791327000000},
        "seven_day": {"used_percentage": 9, "resets_at": "tomorrow"}}}, str(tmp_path))
    doc = json.loads((tmp_path / "soma-ctx" / "m.json").read_text())
    assert doc["five_hour"]["resets_at"] == 1791327000 and doc["seven_day"]["resets_at"] is None
    # an old-style ms value already in a state file expires too
    past_ms = int((time.time() - 60) * 1000)
    _write(tmp_path, five_hour={"used_pct": 93, "resets_at": past_ms})
    seg, _ = soma_ctx.context_segment(_hook(), str(tmp_path))
    assert "5h" not in seg
    _write(tmp_path, five_hour={"used_pct": 93, "resets_at": 1e308})
    assert "5h 93%" in soma_ctx.context_segment(_hook(), str(tmp_path))[0]  # junk is null, not expiry


@pytest.mark.parametrize("val,on", [("0", False), ("off", False), ("FALSE", False), (" No ", False),
                                    ("", True), ("1", True), ("yes", True), ("2", True), ("on", True)])
def test_ctx_switch_values(tmp_path, monkeypatch, val, on):
    _write(tmp_path)
    monkeypatch.setenv("SOMA_CTX", val)
    assert (soma_ctx.context_segment(_hook(), str(tmp_path))[0] is not None) is on


@pytest.mark.parametrize("val", ["nan", "inf", "-inf", "-1", "junk"])
def test_ctx_pct_and_max_age_bad_values_fall_back(tmp_path, monkeypatch, val):
    _write(tmp_path, used_pct=86)
    monkeypatch.setenv("SOMA_CTX_PCT", val)
    assert soma_ctx.context_segment(_hook(), str(tmp_path))[1] is True   # default 85
    _write(tmp_path, used_pct=84)
    assert soma_ctx.context_segment(_hook(), str(tmp_path))[1] is False
    monkeypatch.delenv("SOMA_CTX_PCT")
    _write(tmp_path, ts=time.time() - 3600)
    monkeypatch.setenv("SOMA_CTX_MAX_AGE_S", val)
    assert soma_ctx.context_segment(_hook(), str(tmp_path))[0] is not None  # default 86400


def test_max_age_env_honoured(tmp_path, monkeypatch):
    _write(tmp_path, ts=time.time() - 100)
    monkeypatch.setenv("SOMA_CTX_MAX_AGE_S", "50")
    assert soma_ctx.context_segment(_hook(), str(tmp_path)) == (None, False)
    monkeypatch.setenv("SOMA_CTX_MAX_AGE_S", "200")
    assert soma_ctx.context_segment(_hook(), str(tmp_path))[0] is not None


def test_future_timestamp_clock_skew(tmp_path):
    now = time.time()
    _write(tmp_path, ts=now + 400)
    assert soma_ctx.context_segment(_hook(), str(tmp_path), now) == (None, False)
    _write(tmp_path, ts=now + 200)
    assert soma_ctx.context_segment(_hook(), str(tmp_path), now)[0] is not None


def test_prune_runs_at_most_hourly(tmp_path):
    d = tmp_path / "soma-ctx"
    soma_ctx.write_from_statusline(SAMPLE, str(tmp_path))
    marker = d / ".pruned"
    assert marker.exists()
    old = d / "dead.json"
    old.write_text("{}")
    past = time.time() - 10 * 86400
    os.utime(old, (past, past))
    soma_ctx.write_from_statusline(SAMPLE, str(tmp_path))
    assert old.exists()  # marker is fresh: no scan
    os.utime(marker, (time.time() - 7200, time.time() - 7200))
    soma_ctx.write_from_statusline(SAMPLE, str(tmp_path))
    assert not old.exists()


def test_prune_future_marker_is_due(tmp_path):
    d = tmp_path / "soma-ctx"
    d.mkdir()
    marker = d / ".pruned"
    marker.write_text("")
    future = time.time() + 30 * 86400
    os.utime(marker, (future, future))
    old = d / "dead.json"
    old.write_text("{}")
    past = time.time() - 10 * 86400
    os.utime(old, (past, past))
    soma_ctx.write_from_statusline(SAMPLE, str(tmp_path))
    assert not old.exists()
    assert marker.stat().st_mtime < time.time() + 60


def test_prune_removes_old_tmp_files(tmp_path):
    d = tmp_path / "soma-ctx"
    d.mkdir()
    stale, fresh = d / ".x.123.tmp", d / ".y.456.tmp"
    stale.write_text("{")
    fresh.write_text("{")
    past = time.time() - 10 * 86400
    os.utime(stale, (past, past))
    soma_ctx.write_from_statusline(SAMPLE, str(tmp_path))
    assert not stale.exists() and fresh.exists()


def test_writer_numbers_must_be_numbers(tmp_path):
    for bad in (True, False, float("nan"), float("inf"), "87", None):
        soma_ctx.write_from_statusline({"session_id": "b", "context_window": {
            "used_percentage": bad, "context_window_size": bad, "current_usage": {"input_tokens": bad}},
            "rate_limits": {"five_hour": {"used_percentage": bad}}}, str(tmp_path))
        assert _files(tmp_path) == [], repr(bad)
    _write(tmp_path, used_pct=True, used_tokens=True, window=True, five_hour={"used_pct": True, "resets_at": True},
           seven_day={"used_pct": float("nan"), "resets_at": None})
    assert soma_ctx.context_segment(_hook(), str(tmp_path)) == (None, False)


def test_writer_session_id_without_data_writes_nothing(tmp_path):
    for doc in ({"session_id": "only-id"},
                {"session_id": "only-id", "context_window": {"used_percentage": "x"}, "rate_limits": 5}):
        soma_ctx.write_from_statusline(doc, str(tmp_path))
        assert not (tmp_path / "soma-ctx").exists() or _files(tmp_path) == []


def test_zero_usage_entries_skipped(tmp_path):
    assert soma_ctx._usage_tokens(_usage_line(0, 0, 0).encode()) is None
    t = tmp_path / "t.jsonl"
    t.write_text(_usage_line(1, 2, 3) + "\n" + _usage_line(0, 0, 0) + "\n")
    assert soma_ctx.transcript_tokens(str(t)) == 6


def test_transcript_bound_is_four_mib_literal(tmp_path):
    t = tmp_path / "t.jsonl"
    four = 4 * 1024 * 1024
    t.write_text(_usage_line(1, 2, 3) + "\n" + "x" * (four - 5000) + "\n")
    assert soma_ctx.transcript_tokens(str(t)) == 6
    t.write_text(_usage_line(1, 2, 3) + "\n" + "x" * (four + 5000) + "\n")
    assert soma_ctx.transcript_tokens(str(t)) is None


def test_transcript_fallback_under_1000_tokens_renders_nothing(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text(_usage_line(1, 2, 400) + "\n")
    assert soma_ctx.context_segment(_hook(transcript=t), str(tmp_path)) == (None, False)
    t.write_text(_usage_line(1, 2, 1100) + "\n")
    assert soma_ctx.context_segment(_hook(transcript=t), str(tmp_path)) == ("ctx 1k", False)


def test_state_tokens_win_over_transcript(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text(_usage_line(1, 1, 100000) + "\n")
    _write(tmp_path, used_pct=None, window=None, used_tokens=500000)
    assert soma_ctx.context_segment(_hook(transcript=t), str(tmp_path)) == ("ctx 500k · 5h 7% · 7d 19%", False)


def test_pressure_mode_level_triggered_through_real_script(tmp_path):
    sd = tmp_path / "state"
    env = _quiet_env(sd, SOMA_MODE="pressure")
    script = HOOKS / "soma-state.py"
    _write(sd, used_pct=84)
    r = _run(script, json.dumps(_hook()), env)
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    _write(sd, used_pct=85)
    for _ in range(2):  # at or above on every prompt, not only the crossing
        r = _run(script, json.dumps(_hook()), env)
        assert r.returncode == 0 and "ctx 85% (866k/1000k)(HIGH)" in r.stdout, (r.stdout, r.stderr)
    _write(sd, used_pct=84)
    assert _run(script, json.dumps(_hook()), env).stdout == ""


def test_pulse_silent_after_ctx_emission_end_to_end(tmp_path):
    sd = tmp_path / "state"
    env = _quiet_env(sd, SOMA_MODE="pressure")
    _write(sd, used_pct=95)
    r = _run(HOOKS / "soma-state.py", json.dumps(_hook()), env)
    assert "(HIGH)" in r.stdout
    r = _run(HOOKS / "soma-pulse.py", "{}", env)
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
