# Soma

**Body-state awareness for AI agents.** The body axis of the self-grounding triad.

| Sibling | Greek | Axis | The question it answers |
|---------|-------|------|------------------------|
| **Soma** | σῶμα, body | physical substrate | *what state is my body in?* |
| [Kairos](https://github.com/draca-glitch/Kairos) | καιρός, the moment | time | *when am I?* |
| [Mnemos](https://github.com/draca-glitch/Mnemos) | μνήμη, memory | persistence | *what came before?* |

## Why Soma exists

An AI agent runs *on* a machine but has no native sense of that machine's condition. It will cheerfully try to load a 30B model onto a box that is already swapping, or spend three tool calls running `free`, `ps`, and `df` to discover what one ambient line could have told it before the first word.

It has memory (Mnemos) and a sense of time (Kairos), but no **proprioception**: no felt sense of its own body. Soma is that missing sense. The host is the agent's body; RAM, CPU, disk, and load are its physiology. RAM pressure is the body feeling strained; swap-thrash is it short of breath.

Before each prompt, Soma reads the body's state and, when something is worth noticing, injects one line:

```
[system-state] mem 6.2G/61G avail(LOW) · swap 1.1G · top mnemos-mcp 15.5G(25.4%)(TOP) · / 71% · load 14/16(HIGH)
```

That single line front-loads a fact the agent would otherwise have to go dig for. It **senses; it never acts.** No restarts, no kills, no "you should". It states the body's condition and lets the agent decide, the same division of labor that makes a sense of time useful without being bossy.

## What it watches

- **Memory**: available RAM as a share of total, and swap in use.
- **Top consumer**: the single largest-RSS process, surfaced when it holds a notable share of RAM even if total memory is fine (the common case: one process quietly dominating a healthy box).
- **Disk**: percent-used on a small mount watchlist.
- **Load**: 1-minute load average against core count.
- **Temperature**: hottest sensor per class (CPU, disk, GPU) from sysfs hwmon, when the kernel exposes them. The body's fever check: a `(HOT)` tag on the class that crossed its ceiling.
- **Services** (opt-in): `systemctl is-active` over a short watchlist; surfaces any that are not active.

## Design principles

- **Default-quiet.** In the default `pressure` mode, Soma emits *only* when something crosses a threshold. A healthy box stays silent. A layer that narrates the boring case every turn trains the reader to ignore it.
- **Orient, do not decide.** Soma reports state. What to do about it is the agent's call.
- **Cheap.** Pure stdlib, reads `/proc`, one `statvfs` per mount, an optional `systemctl` probe only if a watchlist is set. Sub-15ms, no model, no network.
- **Never blocks.** The hook degrades to a partial reading or to silence; it never raises into the prompt path.
- **Falsifiable.** Every emission is logged (`soma-log.jsonl`) so its value can be measured later, not just asserted.

## Install

Drop the hooks somewhere Claude Code can run them (e.g. `~/.claude/hooks/`) and register the UserPromptSubmit hook in `settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command", "command": "~/.claude/hooks/soma-state.py", "timeout": 2000 }] }
    ]
  }
}
```

`soma_lib.py` must sit beside `soma-state.py`. Python 3.10+, no dependencies.

## Configuration

All thresholds are `SOMA_*` environment variables. Defaults are tuned for a large-RAM workstation/server; lower them on small boxes.

| Variable | Default | Meaning |
|----------|---------|---------|
| `SOMA_MODE` | `pressure` | `pressure` (quiet unless notable), `always` (emit every turn), `off` |
| `SOMA_MEM_AVAIL_PCT` | `15` | flag when available RAM drops below this percent of total |
| `SOMA_SWAP_MB` | `256` | flag when swap-in-use exceeds this many MB |
| `SOMA_DISK_PCT` | `85` | flag when any watched mount exceeds this percent used |
| `SOMA_LOAD_RATIO` | `1.0` | flag when 1-min load / cores exceeds this |
| `SOMA_TOP_RSS_PCT` | `25` | flag the top process when its RSS exceeds this percent of total RAM; `0` disables |
| `SOMA_TEMP_CPU` | `85` | degC ceiling for the CPU sensor class (k10temp, coretemp, ...); `0` disables |
| `SOMA_TEMP_DISK` | `70` | degC ceiling for the disk sensor class (nvme, drivetemp); `0` disables |
| `SOMA_TEMP_GPU` | `90` | degC ceiling for the GPU sensor class (amdgpu, i915, ...); `0` disables |
| `SOMA_MOUNTS` | `/,/root/work` | comma-separated mounts to check (duplicate filesystems are deduped) |
| `SOMA_SERVICES` | *(empty)* | comma-separated services to probe; empty means no `systemctl` call |
| `SOMA_LOG` | `1` | append each emission to the log; `0` disables |
| `SOMA_STATE_DIR` | `~/.claude/state` | where `soma-log.jsonl` is written |

## Relationship to the research

Soma is a deliberate **generalization experiment**. Kairos established that injecting an orthogonal orienting signal (time) measurably shapes behavior. Soma asks whether the same mechanism, applied to a different orthogonal axis (the physical substrate), produces the same kind of value. If it does, the underlying claim generalizes beyond time. If it does not, that is a falsification boundary worth knowing.

Soma is its **own** project and its own axis. It is not part of, and does not modify, Kairos or the temporal-cognition argument; that paper's strength is its clean single-axis claim, and Soma is kept separate to preserve it.

## License

MIT. See [LICENSE](LICENSE).
