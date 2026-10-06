#!/usr/bin/env python3
"""
PostToolUse hook: mid-turn proprioception.

The prompt-time hook (soma-state.py) orients the agent when the human
speaks. But the body changes most while the agent is acting: builds,
benches, parallel subagents. This hook samples after every tool call and
emits ONLY on a flag transition, when a condition appears or a chronic one
clears. A long healthy turn costs zero lines; the OOM kill or the HOT flag
reaches the agent while it is still acting, not at the next prompt.

Modes (SOMA_PULSE): transition (default) | off.

Output: Claude Code hands a PostToolUse hook's plain stdout to nobody; only
this JSON on stdout (exit 0) reaches the model, so that is what is printed:
  {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "[system-state] ..."}}
SOMA_PULSE_FORMAT=plain prints the bare line instead, for harnesses that read
stdout (default json).

Anti-flap: SOMA_PULSE_HOLD_S (default 300, 0 = off) is how long a chronic flag
must stay absent, unbroken, before its recovery is announced; a flag is
announced once while it is held. Acute flags (OOM, ECC) are announced on every
appearance. Delivery is per session (<state_dir>/soma-pulse/<session_id>.json),
so two sessions each hear a transition once. A tool call inside a subagent
(stdin carries agent_id) announces nothing; the main agent hears it itself.

Usage in settings.json (PostToolUse, no matcher so every tool is sampled;
the timeout is in SECONDS):
  "PostToolUse": [{
    "hooks": [{ "type": "command", "command": "~/.claude/hooks/soma-pulse.py", "timeout": 2 }]
  }]

Like its sibling, it never raises into the hook path.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from soma_lib import pulse_line


def main() -> int:
    try:
        raw = sys.stdin.read(1 << 20)
    except Exception:
        raw = ""
    try:
        payload = json.loads(raw) if raw.strip() else None
    except Exception:  # ValueError, RecursionError on absurd nesting, anything else
        payload = None
    if not isinstance(payload, dict):
        payload = None
    try:
        line = pulse_line(hook_input=payload)
        if line:
            if os.environ.get("SOMA_PULSE_FORMAT", "json").strip().lower() == "plain":
                print(line)
            else:
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                         "additionalContext": line}}))
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
