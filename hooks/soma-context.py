#!/usr/bin/env python3
"""
Statusline bridge: hand the context-window and rate-limit numbers that
Claude Code gives only to the statusline over to soma-state.py.

Claude Code passes the statusline command a JSON document on stdin that
carries `context_window` (used share, current usage, window size) and, on
subscription plans, `rate_limits` (five_hour, seven_day). Hooks never see
those. This command stores them in a small per-session file under the soma
state directory (<state_dir>/soma-ctx/<session_id>.json, atomic write), and
soma-state.py renders them as `ctx 87% (866k/1000k) · 5h 7% · 7d 19%`.

It prints NOTHING and always exits 0, whatever the input: it runs inside
someone's statusline and must never add output or an error to it.

Usage: add one line to the statusline script, after it has read stdin into
$input:
  printf '%s' "$input" | ~/.claude/hooks/soma-context.py

soma_ctx.py must sit beside this file. Python 3.10+, no dependencies.
"""

import os
import sys


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        import json
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from soma_ctx import write_from_statusline
        write_from_statusline(json.loads(raw))
    except BaseException:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
