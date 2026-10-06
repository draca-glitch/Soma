"""Soma 0.11.0: the compaction notice through the prompt hook and the pulse."""
import json
import sys
import time
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS))
import soma_compact  # noqa: E402
import soma_lib  # noqa: E402
from test_pulse import _proc, _pulse  # noqa: E402
from test_soma_compact import SID, entries_basic, write  # noqa: E402
from test_soma_ctx import _quiet_env, _run  # noqa: E402


def _compact(tmp_path, now):
    p = write(tmp_path / "t.jsonl", entries_basic())
    assert soma_compact.handle({"hook_event_name": "PostCompact", "session_id": SID, "transcript_path": p,
                                "compact_summary": ""}, str(tmp_path / "state"), now)
    return p


def _prompt(tmp_path, proc, now, extra=None, mode="pressure"):
    return soma_lib.line_for_mode(
        mode, proc_root=str(proc), mounts=[], services=[], hwmon_root=str(tmp_path / "no-hwmon"),
        sys_root=str(tmp_path / "no-sys"), state_dir=str(tmp_path / "state"), now=now,
        hook_input={"session_id": SID, **(extra or {})})


def _log(tmp_path):
    f = tmp_path / "state" / "soma-log.jsonl"
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


def test_prompt_emits_notice_once_on_healthy_box(tmp_path, monkeypatch):
    monkeypatch.setenv("SOMA_LOG", "1")
    proc = _proc(tmp_path, "p")
    now = time.time()
    assert _prompt(tmp_path, proc, now) is None  # healthy, silent
    _compact(tmp_path, now)
    line = _prompt(tmp_path, proc, now + 1)
    assert line.startswith("[system-state] mem ") and " · compacted " in line and "dropped: 3 paths" in line
    assert line.endswith(".md")
    rec = _log(tmp_path)[-1]
    assert "COMPACT" in rec["flags"]
    st = json.loads((tmp_path / "state" / "soma.json").read_text()) if (tmp_path / "state" / "soma.json").exists() else {}
    assert "COMPACT" not in st.get("last_flags", [])
    assert _prompt(tmp_path, proc, now + 2) is None  # said once
    assert soma_compact.read_state(SID, str(tmp_path / "state"))["announced"] is True


def test_second_call_byte_identical_to_no_compaction(tmp_path):
    proc = _proc(tmp_path, "p")
    now = time.time()
    plain = _prompt(tmp_path, proc, now, mode="always")
    _compact(tmp_path, now)
    with_notice = _prompt(tmp_path, proc, now, mode="always")
    assert with_notice.startswith(plain + " · compacted ")
    assert _prompt(tmp_path, proc, now, mode="always") == plain


def test_pulse_emits_pending_notice_on_its_own_and_once(tmp_path):
    proc = _proc(tmp_path, "p")
    now = time.time()
    assert _pulse(tmp_path, proc, now, sid=SID) is None
    _compact(tmp_path, now)
    line = _pulse(tmp_path, proc, now + 1, sid=SID)
    assert line and " · compacted " in line
    assert _pulse(tmp_path, proc, now + 2, sid=SID) is None
    assert _prompt(tmp_path, proc, now + 3) is None  # the prompt hook does not repeat it


def test_subagent_never_consumes_notice(tmp_path):
    proc = _proc(tmp_path, "p")
    now = time.time()
    _compact(tmp_path, now)
    assert _pulse(tmp_path, proc, now + 1, sid=SID, extra={"agent_id": "sub-1"}) is None
    assert soma_compact.read_state(SID, str(tmp_path / "state"))["announced"] is False
    assert " · compacted " in _pulse(tmp_path, proc, now + 2, sid=SID)


def test_stale_notice_not_announced(tmp_path):
    proc = _proc(tmp_path, "p")
    now = time.time()
    _compact(tmp_path, now - 25 * 3600)
    assert _prompt(tmp_path, proc, now) is None
    assert _pulse(tmp_path, proc, now, sid=SID) is None


def test_off_switch_announcer(tmp_path, monkeypatch):
    proc = _proc(tmp_path, "p")
    now = time.time()
    _compact(tmp_path, now)
    monkeypatch.setenv("SOMA_COMPACT", "0")
    assert _prompt(tmp_path, proc, now + 1) is None
    assert _pulse(tmp_path, proc, now + 1, sid=SID) is None


def test_unwritable_mark_prints_nothing(tmp_path, monkeypatch):
    proc = _proc(tmp_path, "p")
    now = time.time()
    _compact(tmp_path, now)
    monkeypatch.setattr(soma_compact, "_atomic", lambda *a, **k: False)
    assert _prompt(tmp_path, proc, now + 1) is None


def test_hooks_without_soma_compact_end_to_end(tmp_path):
    """A deployment without soma_compact.py still gives the plain line, through both hooks."""
    d = tmp_path / "hooks"
    d.mkdir()
    for p in HOOKS.glob("*.py"):
        if p.name not in ("soma_compact.py",):
            (d / p.name).write_text(p.read_text())
    sd = tmp_path / "state"
    _compact(tmp_path, time.time())
    hi = json.dumps({"session_id": SID, "transcript_path": str(tmp_path / "t.jsonl")})
    r = _run(d / "soma-state.py", hi, _quiet_env(sd, SOMA_MODE="always"))
    assert r.returncode == 0 and r.stdout.startswith("[system-state]") and "compacted" not in r.stdout
    r = _run(d / "soma-pulse.py", hi, _quiet_env(sd))
    assert r.returncode == 0 and "compacted" not in r.stdout
    r = _run(d / "soma-compact.py", json.dumps({"hook_event_name": "PostCompact", "session_id": SID,
                                                "transcript_path": str(tmp_path / "t.jsonl")}), _quiet_env(sd))
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""


def test_hooks_with_soma_compact_end_to_end(tmp_path):
    sd = tmp_path / "state"
    _compact(tmp_path, time.time())
    hi = json.dumps({"session_id": SID, "transcript_path": str(tmp_path / "t.jsonl"), "tool_name": "Bash"})
    r = _run(HOOKS / "soma-pulse.py", hi, _quiet_env(sd))
    out = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    assert " · compacted " in out


def test_pulse_marks_announced_only_when_told_state_written(tmp_path, monkeypatch):
    now = time.time()
    _compact(tmp_path, now)
    proc = _proc(tmp_path, "p")
    monkeypatch.setattr(soma_lib, "_write_session", lambda *a, **k: False)
    assert _pulse(tmp_path, proc, now + 1, sid=SID) is None
    assert soma_compact.read_state(SID, str(tmp_path / "state"))["announced"] is False
