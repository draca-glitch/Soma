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

Usage in settings.json (PostToolUse, no matcher so every tool is sampled):
  "PostToolUse": [{
    "hooks": [{ "type": "command", "command": "~/.claude/hooks/soma-pulse.py", "timeout": 2000 }]
  }]

Like its sibling, it never raises into the hook path.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from soma_lib import pulse_line


def main() -> int:
    try:
        sys.stdin.read()
    except Exception:
        pass
    try:
        line = pulse_line()
        if line:
            print(line)
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
