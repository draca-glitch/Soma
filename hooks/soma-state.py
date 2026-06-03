#!/usr/bin/env python3
"""
UserPromptSubmit hook: emit a one-line [system-state] summary so Claude
arrives at the prompt already oriented to the host's physical condition.

Soma is the body axis of the self-grounding triad (Soma=body, Kairos=time,
Mnemos=memory). This hook is proprioception: read the body, and when
something is worth noticing, say so in one line. It senses, it never acts.

Output (default pressure mode emits only when a threshold is crossed):
  [system-state] mem 6.2G/61G avail(LOW) · swap 1.1G · top mnemos-mcp 15.5G(25.4%)(TOP) · / 71% · load 14/16(HIGH)

A healthy box stays silent. Logic lives in soma_lib.py; this hook is just
the renderer plus the emission log. It never raises into the prompt path.

Modes (SOMA_MODE): pressure (default) | always | off.

Usage in settings.json:
  "UserPromptSubmit": [{
    "hooks": [{ "type": "command", "command": "~/.claude/hooks/soma-state.py", "timeout": 2000 }]
  }]
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from soma_lib import gather, assess, render, log_emission


def main() -> int:
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    if "<task-notification>" in raw:
        return 0

    mode = os.environ.get("SOMA_MODE", "pressure")
    if mode == "off":
        return 0

    try:
        state = gather()
        a = assess(state)
        if mode == "always" or a["flags"]:
            line = render(state, a)
            print(line)
            log_emission(line, a["flags"])
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
