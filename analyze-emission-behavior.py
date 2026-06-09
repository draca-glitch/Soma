#!/usr/bin/env python3
"""
Emission-versus-behavior evaluator: does body-state injection shift behavior?

Joins soma-log.jsonl (every [system-state] line Soma emitted, with flags and
src) against Claude Code session transcripts (~/.claude/projects/**/*.jsonl)
and measures, for each emission, what the agent did in the response window
that followed:

  acknowledged  the assistant's text mentions the flagged condition
  acted         a subsequent tool call plausibly investigates or responds
                to the flagged condition (flag-specific command patterns)
  latency       tool calls issued before the first acknowledgment/action

Flagged emissions are compared against the control population: healthy
always-mode emissions (empty flag set), which inject a line of identical
shape carrying no notable condition. The difference between the two
populations is the behavior shift attributable to the flag, which is the
falsifiable test of whether the orienting mechanism generalizes off the
time axis (the sibling Kairos project documents the temporal version).

Privacy: reads transcripts in place, writes nothing but aggregate counts
to stdout. No prompt or response text leaves the process.

Usage:
  ./analyze-emission-behavior.py
  ./analyze-emission-behavior.py --log /path/soma-log.jsonl --since 2026-06-01
  ./analyze-emission-behavior.py --window 600 --json
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path


DEFAULT_LOG = Path(
    os.environ.get("SOMA_STATE_DIR")
    or os.environ.get("CLAUDE_KIT_STATE_DIR", str(Path.home() / ".claude" / "state"))
) / "soma-log.jsonl"
DEFAULT_TRANSCRIPTS = Path.home() / ".claude" / "projects"

# What counts as "talking about" each flagged condition.
FLAG_KEYWORDS = {
    "HOT": ("temp", "thermal", "°c", "degc", "overheat", "cooling"),
    "LOW_MEM": ("memory", "ram", "avail"),
    "SWAP": ("swap",),
    "DRAIN": ("drain", "memory", "leak", "empty"),
    "GROW": ("grow", "leak", "rss"),
    "TOP": ("rss", "process", "memory"),
    "SELF": ("own process", "process tree", "self"),
    "STRAIN": ("psi", "stall", "pressure", "strain"),
    "LOAD": ("load",),
    "DISK": ("disk", "full", "space"),
    "FILL": ("fill", "full", "disk"),
    "OOM": ("oom", "killed", "out of memory"),
    "ECC": ("ecc", "edac", "dimm", "memory error"),
    "RAID": ("raid", "degraded", "mdadm", "mirror"),
    "NUMB": ("mount", "unresponsive", "hung", "numb"),
    "SVC": ("service", "down", "systemctl"),
}

# Bash command fragments that count as investigating each condition.
FLAG_ACTIONS = {
    "HOT": ("sensors", "smartctl", "hwmon", "nvme smart-log"),
    "LOW_MEM": ("free", "ps ", "smem", "meminfo"),
    "SWAP": ("free", "swapon", "vmstat"),
    "DRAIN": ("free", "ps ", "meminfo"),
    "GROW": ("ps ", "pmap", "smaps", "rss"),
    "TOP": ("ps ", "top", "pmap"),
    "SELF": ("ps ", "pstree"),
    "STRAIN": ("pressure", "iostat", "vmstat", "uptime", "ps "),
    "LOAD": ("uptime", "ps ", "iostat"),
    "DISK": ("df", "du ", "ncdu"),
    "FILL": ("df", "du ", "ncdu"),
    "OOM": ("dmesg", "journalctl", "vmstat", "oom"),
    "ECC": ("edac", "dmesg", "journalctl"),
    "RAID": ("mdstat", "mdadm"),
    "NUMB": ("mount", "umount", "wg ", "findmnt", "stat "),
    "SVC": ("systemctl", "journalctl"),
}


def parse_ts(raw):
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


def load_emissions(path: Path, since) -> list:
    if not path.exists():
        return []
    out = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            ts = parse_ts(rec.get("ts") or "")
            if ts is None or (since and ts < since):
                continue
            out.append({"ts": ts, "flags": rec.get("flags") or [], "src": rec.get("src", "state")})
    return out


def iter_transcript_events(root: Path, lo, hi):
    """Yield (ts, role, text, bash_commands) for events inside [lo, hi].

    Only opens transcript files whose mtime falls at or after lo, which
    bounds IO on large histories. Tolerant of shape drift: anything without
    a parseable timestamp or role is skipped.
    """
    if not root.exists():
        return
    lo_epoch = lo.timestamp() - 3600  # slack for clock skew between event ts and file mtime
    for path in root.glob("*/*.jsonl"):
        try:
            if path.stat().st_mtime < lo_epoch:
                continue
        except OSError:
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    ts = parse_ts(rec.get("timestamp") or "")
                    if ts is None or ts < lo or ts > hi:
                        continue
                    role = rec.get("type") or (rec.get("message") or {}).get("role") or ""
                    if role not in ("user", "assistant"):
                        continue
                    text_parts, commands = [], []
                    content = (rec.get("message") or {}).get("content")
                    if isinstance(content, str):
                        text_parts.append(content)
                    elif isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") == "text":
                                text_parts.append(block.get("text") or "")
                            elif block.get("type") == "tool_use":
                                inp = block.get("input") or {}
                                cmd = inp.get("command") if isinstance(inp, dict) else ""
                                commands.append((block.get("name") or "") + " " + (cmd or ""))
                    yield (ts, role, " ".join(text_parts).lower(), [c.lower() for c in commands])
        except OSError:
            continue


def evaluate(emissions: list, transcripts: Path, window_s: int) -> dict:
    """Per flag class (plus the healthy control): ack/act/latency aggregates."""
    if not emissions:
        return {}
    lo = min(e["ts"] for e in emissions)
    hi = max(e["ts"] for e in emissions) + timedelta(seconds=window_s)
    events = sorted(iter_transcript_events(transcripts, lo, hi), key=lambda ev: ev[0])

    stats = defaultdict(lambda: {"emissions": 0, "acknowledged": 0, "acted": 0,
                                 "latencies": [], "windows_with_activity": 0})
    for em in emissions:
        end = em["ts"] + timedelta(seconds=window_s)
        window = []
        for ev in events:
            if ev[0] <= em["ts"] or ev[0] > end:
                continue
            if ev[1] == "user" and window:
                break  # response window closes at the next user turn
            window.append(ev)
        classes = em["flags"] if em["flags"] else ["<healthy-control>"]
        for cls in classes:
            s = stats[cls]
            s["emissions"] += 1
            if window:
                s["windows_with_activity"] += 1
            keywords = FLAG_KEYWORDS.get(cls, ())
            actions = FLAG_ACTIONS.get(cls, ())
            ack = act = False
            tool_calls_seen = 0
            latency = None
            for ts, role, text, commands in window:
                if commands:
                    tool_calls_seen += len(commands)
                if not ack and keywords and role == "assistant" and any(k in text for k in keywords):
                    ack, latency = True, latency if latency is not None else tool_calls_seen
                if not act and actions and any(a in c for c in commands for a in actions):
                    act, latency = True, latency if latency is not None else tool_calls_seen - 1
            if ack:
                s["acknowledged"] += 1
            if act:
                s["acted"] += 1
            if latency is not None:
                s["latencies"].append(max(0, latency))
    return dict(stats)


def render(stats: dict) -> str:
    if not stats:
        return "no emissions to evaluate"
    out = [f"  {'class':20s} {'emitted':>8s} {'active-win':>10s} {'ack%':>7s} {'act%':>7s} {'med-latency':>12s}"]
    out.append("  " + "-" * 70)
    for cls in sorted(stats, key=lambda c: (c == "<healthy-control>", c)):
        s = stats[cls]
        n = s["emissions"]
        ack = s["acknowledged"] / n * 100 if n else 0.0
        act = s["acted"] / n * 100 if n else 0.0
        lat = sorted(s["latencies"])
        med = str(lat[len(lat) // 2]) if lat else "-"
        out.append(f"  {cls:20s} {n:>8d} {s['windows_with_activity']:>10d} {ack:>6.1f}% {act:>6.1f}% {med:>12s}")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--transcripts", type=Path, default=DEFAULT_TRANSCRIPTS)
    parser.add_argument("--since", type=str, default=None)
    parser.add_argument("--window", type=int, default=600, help="response window seconds")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    since = None
    if args.since:
        since = parse_ts(args.since if "T" in args.since else args.since + "T00:00:00+00:00")
        if since is None:
            print(f"invalid --since: {args.since}", file=sys.stderr)
            return 2

    emissions = load_emissions(args.log, since)
    if not emissions:
        print(f"no emissions in {args.log}", file=sys.stderr)
        return 0
    stats = evaluate(emissions, args.transcripts, args.window)

    if args.json:
        printable = {
            cls: {**s, "latencies": sorted(s["latencies"])} for cls, s in stats.items()
        }
        print(json.dumps({"log": str(args.log), "emissions": len(emissions),
                          "window_s": args.window, "classes": printable}, indent=2, default=str))
        return 0

    flagged = sum(s["emissions"] for c, s in stats.items() if c != "<healthy-control>")
    control = stats.get("<healthy-control>", {}).get("emissions", 0)
    print(f"log: {args.log}")
    print(f"emissions: {len(emissions)} ({flagged} flag-class observations, {control} healthy controls)")
    print(f"response window: {args.window}s, closes at next user turn\n")
    print(render(stats))
    print("\nack% / act% on flagged classes vs <healthy-control> is the behavior-shift signal.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
