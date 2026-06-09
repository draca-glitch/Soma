"""Tests for analyze-emission-behavior.py against synthetic transcripts."""

import importlib.util
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "analyze-emission-behavior.py"
_spec = importlib.util.spec_from_file_location("emission_eval", _SCRIPT)
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)

T0 = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _emission(ts, flags, src="state"):
    return {"ts": ts, "flags": flags, "src": src}


def _write_transcript(tmp_path, events):
    proj = tmp_path / "projects" / "p1"
    proj.mkdir(parents=True)
    lines = []
    for offset_s, role, text, command in events:
        content = []
        if text:
            content.append({"type": "text", "text": text})
        if command:
            content.append({"type": "tool_use", "name": "Bash", "input": {"command": command}})
        lines.append(json.dumps({
            "type": role,
            "timestamp": _iso(T0 + timedelta(seconds=offset_s)),
            "message": {"role": role, "content": content},
        }))
    (proj / "s1.jsonl").write_text("\n".join(lines) + "\n")
    return tmp_path / "projects"


def test_acknowledgment_detected(tmp_path):
    transcripts = _write_transcript(tmp_path, [
        (10, "assistant", "the cpu temperature is high, throttling risk", None),
    ])
    stats = ev.evaluate([_emission(T0, ["HOT"])], transcripts, window_s=600)
    assert stats["HOT"]["acknowledged"] == 1
    assert stats["HOT"]["acted"] == 0


def test_action_detected_with_latency(tmp_path):
    transcripts = _write_transcript(tmp_path, [
        (5, "assistant", "checking", "echo unrelated"),
        (10, "assistant", "", "df -h /"),
    ])
    stats = ev.evaluate([_emission(T0, ["DISK"])], transcripts, window_s=600)
    assert stats["DISK"]["acted"] == 1
    assert stats["DISK"]["latencies"] == [1]  # one unrelated call before df


def test_window_closes_at_next_user_turn(tmp_path):
    transcripts = _write_transcript(tmp_path, [
        (10, "assistant", "working", None),
        (20, "user", "new prompt", None),
        (30, "assistant", "the disk is full, let me df", "df -h"),  # next turn, out of window
    ])
    stats = ev.evaluate([_emission(T0, ["DISK"])], transcripts, window_s=600)
    assert stats["DISK"]["acted"] == 0
    assert stats["DISK"]["acknowledged"] == 0


def test_healthy_control_bucketed_separately(tmp_path):
    transcripts = _write_transcript(tmp_path, [
        (10, "assistant", "just normal work", "ls"),
    ])
    stats = ev.evaluate(
        [_emission(T0, []), _emission(T0 + timedelta(seconds=1), ["HOT"])],
        transcripts, window_s=600)
    assert "<healthy-control>" in stats
    assert stats["<healthy-control>"]["emissions"] == 1
    assert stats["HOT"]["emissions"] == 1


def test_empty_log_yields_empty_stats(tmp_path):
    assert ev.evaluate([], tmp_path / "projects", window_s=600) == {}


def test_load_emissions_filters_since(tmp_path):
    log = tmp_path / "soma-log.jsonl"
    old = {"ts": _iso(T0 - timedelta(days=10)), "flags": ["HOT"], "src": "state"}
    new = {"ts": _iso(T0), "flags": [], "src": "pulse"}
    log.write_text(json.dumps(old) + "\n" + json.dumps(new) + "\n")
    ems = ev.load_emissions(log, since=T0 - timedelta(days=1))
    assert len(ems) == 1 and ems[0]["src"] == "pulse"
