#!/usr/bin/env python3
"""Soma compaction hook: one command for PreCompact and PostCompact (0.11.0).

PreCompact indexes the identifiers (paths, commit hashes, #ids, URLs, agent ids) in
the span about to be compacted; PostCompact diffs them against the summary and the
messages the harness kept, writes the dropped ones to
<state dir>/soma-compact/<session_id>-<epoch>.md and leaves a notice that the prompt
hook or the pulse appends to the [system-state] line once. See soma_compact.py.

Neither event can inject context, so this prints nothing and exits 0 on any input.
SOMA_COMPACT=0 turns it off.

Usage in settings.json (the timeout is in SECONDS):
  "PreCompact":  [{ "hooks": [{ "type": "command", "command": "~/.claude/hooks/soma-compact.py", "timeout": 10 }] }],
  "PostCompact": [{ "hooks": [{ "type": "command", "command": "~/.claude/hooks/soma-compact.py", "timeout": 10 }] }]
"""

import json
import sys
from pathlib import Path


def main() -> int:
    try:
        raw = sys.stdin.read(1 << 22)
        payload = json.loads(raw) if raw.strip() else None
        if not isinstance(payload, dict):
            return 0
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from soma_compact import handle
        handle(payload)
    except BaseException:  # ValueError, RecursionError, a missing library: silence, never a blocked hook
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
