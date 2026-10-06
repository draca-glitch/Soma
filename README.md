# Soma

**Author:** [Mikael Wedlund](https://eastblue.se/mikael-wedlund) (`draca-glitch`)

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
[system-state] mem 6.2G/61G avail 10%(LOW) · swap 1.1G · top mnemos-mcp 15.5G(25.4%)(TOP) · / 71% · load 14/16(HIGH)
```

That single line front-loads a fact the agent would otherwise have to go dig for. It **senses; it never acts.** No restarts, no kills, no "you should". It states the body's condition and lets the agent decide, the same division of labor that makes a sense of time useful without being bossy.

## What it watches

- **Memory**: available RAM as a share of total, and swap in use.
- **Top consumer**: the process holding the most private (anonymous) memory, surfaced when it holds a notable share of RAM even if total memory is fine (the common case: one process quietly dominating a healthy box). Ranking is on private memory, not resident set: a process that memory-maps large files (a torrent client seeding, a media server, an mmap'd model runner) carries a resident set dominated by page cache the kernel reclaims on demand, and ranking on that let it win the slot and hide the real consumer. When the two diverge the line shows both: `top qbittorrent-nox 174M(0.6%, 15.5G mapped)`.
- **Disk**: percent-used on a small mount watchlist.
- **Load**: 1-minute load average against core count.
- **Temperature**: hottest sensor per class (CPU, disk, GPU, RAM, board/PCH, wifi, ACPI zone) from sysfs hwmon, when the kernel exposes them. The body's fever check: a `(HOT)` tag on the class that crossed its ceiling. Placeholder readings outside -40..150°C (ACPI zones love publishing -263°C for unwired trip points) are dropped.
- **Strain**: PSI stall shares (`/proc/pressure`, `some` avg10) for cpu/memory/io. The felt difference between busy-and-fine (high load, zero stall) and wedged (low load, high stall), which load average cannot express. Rendered as `psi 1/0/38%` in cpu/mem/io order.
- **Pain**: damage events since the previous reading, from kernel counters: OOM kills (`/proc/vmstat`), ECC corrected/uncorrected memory errors (EDAC), and a degraded md RAID array. Levels are sensations; these are injuries. Counter baselines persist in `soma-state.json` (and, with a `session_id`, per session; see the pulse's **Per session** below), so an event is reported once to each session, at its next prompt or tool call after it happened.
- **Self vs world**: the private memory of the agent's own process tree, found by walking from the hook to the nearest harness ancestor (`SOMA_SELF_COMM`) and summing its whole subtree: the harness, the MCP servers it spawned (the agent's organs), and any tool subprocesses currently running (the agent's own effort). `self claude[14] 12.1G(19.5%)`; flags `SELF` past `SOMA_SELF_RSS_PCT`. "I am heavy" is a different fact from "the world is heavy", and the agent should know which one it is feeling.
- **Numb limbs**: every mount probe runs in a watchdog thread under a shared deadline (`SOMA_MOUNT_TIMEOUT_MS`). A mount that stops answering (a network mount whose VPN dropped) is reported as `numb: /mnt/nas` with flag `NUMB` instead of hanging the hook, converting Soma's own worst failure mode into its most valuable mount signal. An agent that knows the limb is numb does not run the command that would have blocked on it.
- **Movement**: rates of change against a rolling anchor (default window 30 min, `SOMA_TREND_ANCHOR_S=1800`): RAM draining toward empty, a mount filling toward full, the top process growing (private memory, so a seeder paging files in does not read as growth). A level says "85% used"; a rate says "full in ~6h", which is the form a decision actually needs. Flags: `DRAIN` (empty within `SOMA_MEM_TTE_H` and already below half), `FILL` (full within `SOMA_DISK_TTF_H`), `GROW` (top process gaining over `SOMA_TOP_GROWTH_GBH`). Healthy lines carry no rate annotations; movement only shows when flagged.
- **Steal** (virtualized hosts): hypervisor steal share over the trend window; see the VPS section below.
- **Services** (opt-in): `systemctl is-active` over a short watchlist; surfaces any that are not active.
- **Context window and quota** (Claude Code, needs the statusline bridge for the full form): how full the agent's own context window is and how much of the plan's rate limit is used, `ctx 87% (866k/1000k)(HIGH) · 5h 7% · 7d 19%`. The window is part of the body too: an agent that knows it is at 87% can save its state before the harness compacts it, and one that sees how much of the five-hour quota is spent knows how much parallel effort is left. `(HIGH)` marks a fill at or past `SOMA_CTX_PCT` (default 85), and in pressure mode the line is emitted on every prompt while the fill is at or above that threshold. The quota figures never make the line emit by themselves; they ride along when something else does (or in `SOMA_MODE=always`). The `5h` and `7d` parts appear only on subscription plans, and a window whose `resets_at` has passed is dropped rather than shown with its old figure. See [Context window](#context-window) for the one-line setup and the fallback.

## Two hooks, two cadences

- `soma-state.py` (UserPromptSubmit): orients at prompt time, gated by `SOMA_MODE`.
- `soma-pulse.py` (PostToolUse): samples mid-turn, while the agent is acting, which is exactly when the agent itself is loading the box. Emits only on a flag **transition** (something appeared, or a chronic condition cleared), so a long healthy turn costs zero lines and a persisting condition is not repeated every tool call. An acute pain flag clearing is just the delta baseline advancing and does not count as a recovery. Gated by `SOMA_PULSE`.
  - **Output format.** Claude Code does not hand a PostToolUse hook's plain stdout to the model; only `{"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "..."}}` on stdout reaches it, so the pulse prints that (`SOMA_PULSE_FORMAT=json`, default). `SOMA_PULSE_FORMAT=plain` prints the bare line for harnesses that read stdout. The prompt hook keeps plain stdout, which works for UserPromptSubmit.
  - **Hold.** A value hovering on a threshold would toggle its flag every few calls. A level flag (everything except OOM and ECC) is announced once when it appears; it counts as cleared only after it has stayed absent for `SOMA_PULSE_HOLD_S` seconds (default 300, `0` = off) without a break, and only then is the recovery announced, by the pulse. This holds across prompts too: a prompt that prints nothing keeps what the session was told and starts the absence clock of each told flag that is gone (one already absent keeps its time), so a flag that reappears inside the hold stays silent and a recovery that happened just before a silent prompt is still announced by the pulse once the hold has passed. Acute flags (OOM, ECC) carry no hold and are never held: every new kill or error is announced, their clearing never.
  - **Per session.** With a `session_id` on stdin, each session keeps its own file `<state dir>/soma-pulse/<session_id>.json` holding two things: the level flags it has been told (with the hold), and its own baseline of the kernel's cumulative OOM-kill and EDAC counters. An acute event is "the counter is above what this session last saw", so every session hears it once, independently, and the line states the count since that session's baseline. That holds from a session's second contact on. Its first contact (no session file, or a file without a counter baseline, such as one written before 0.10.1), whether the prompt hook or the pulse, reports what the host has seen since the host's last sample, as v0.10.0 did (typically the kill that took down the previous session), and then stores its own baseline at the current values; on a host with no counters recorded yet it reports nothing. Only the session's own main-agent samples advance that file: the pulse and the prompt hook. A tool call made inside a subagent (the hook input carries `agent_id`) announces nothing, leaves the file alone and does not advance the host-wide counters, so the main agent hears the transition or the event on its own next call. The prompt hook records on every prompt: when it prints a line, exactly the flags on that line (a flag absent from the line counts as told cleared); when it stays silent, the flags told before, kept with their absence clocks started. A first pulse with no session file (no prompt hook wired, `SOMA_MODE=off`, file pruned) treats the session as told nothing: it announces the standing level flags once, plus the host-wide acute events of a first contact, and baselines the counters at their current values. A counter below the stored baseline (reboot) re-baselines silently. If the told-state cannot be written (unwritable state dir, full disk), the pulse prints nothing rather than repeating the line after every call. Without a `session_id` (or with a soma_ctx.py older than 0.10.1) the pulse falls back to one host-wide record, as before; trends, the rolling anchor and the host-wide counters stay host-wide in every case.
  - **Known limit.** Two pulses of the same session started within microseconds of each other can both announce the same transition (there is no lock; 0 of 30 tries with the real script, seen only under a synchronised barrier).

Both share `soma-state.json` (counter baselines, trend anchor, last flag set), and every prompt records what its session has been told, so a condition announced at prompt time is not re-announced by the first pulse.

## Virtualized hosts (VPS)

Several senses go dark inside a guest, by design rather than by failure:

- **Temperature**: hypervisors do not expose hwmon chips to guests; `read_temps()` returns `{}` and the segment never renders.
- **ECC (EDAC)**: the memory controller belongs to the host; the guest kernel has no `edac` sysfs tree, so the counter is simply absent.
- **RAID**: storage redundancy is the host's job; no `md` devices, no `RAID` flag.
- **NVMe/disk sensors**: virtual block devices carry no drivetemp class.

Everything absent degrades to a missing key and a missing line segment: a VPS deployment is quieter, never broken. What remains (PSI, OOM kills, swap, disk fill, numb mounts, self-vs-world, all trends) works identically, and PSI arguably matters more on shared infrastructure.

One sense exists specifically FOR the VPS case: **steal**. `/proc/stat` steal jiffies measure cycles the hypervisor took while the guest had work to run, the only way to feel an oversold host from inside; load average looks innocent while the landlord throttles you. Rendered as `steal 12%` once it exceeds noise (0.5%), flagged `(STEAL)` past `SOMA_STEAL_PCT` (default 10). On dedicated hardware steal stays at 0 and the segment never appears.

## Measuring whether it works

`analyze-emission-behavior.py` replays the emission log against session transcripts and reports, per flag class, how often the agent acknowledged the condition, acted on it, and how quickly, with healthy always-mode emissions as the control population. A flag class whose ack/act rates match the healthy control is a sense nobody uses; one that separates is measured behavior shift. This is Soma's falsifiability substrate: the project's premise (orienting injection generalizes beyond the time axis) is tested against its own production log, not asserted.

## Design principles

- **Default-quiet.** In the default `pressure` mode, Soma emits *only* when something crosses a threshold. A healthy box stays silent. A layer that narrates the boring case every turn trains the reader to ignore it.
- **Orient, do not decide.** Soma reports state. What to do about it is the agent's call.
- **Cheap.** Pure stdlib, reads `/proc`, one `statvfs` per mount, an optional `systemctl` probe only if a watchlist is set. About 60 ms end to end per prompt as a Python process start included (measured on a Ryzen 7 PRO 8700GE with about 500 processes; the `/proc` scan dominates), no model, no network.
- **Never blocks.** The hook degrades to a partial reading or to silence; it never raises into the prompt path.
- **Falsifiable.** Every emission is logged (`soma-log.jsonl`) so its value can be measured later, not just asserted.

## Install

Drop the hooks somewhere Claude Code can run them (e.g. `~/.claude/hooks/`) and register the UserPromptSubmit hook in `settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command", "command": "~/.claude/hooks/soma-state.py", "timeout": 2 }] }
    ],
    "PostToolUse": [
      { "hooks": [{ "type": "command", "command": "~/.claude/hooks/soma-pulse.py", "timeout": 2 }] }
    ],
    "PreCompact": [
      { "hooks": [{ "type": "command", "command": "~/.claude/hooks/soma-compact.py", "timeout": 10 }] }
    ],
    "PostCompact": [
      { "hooks": [{ "type": "command", "command": "~/.claude/hooks/soma-compact.py", "timeout": 10 }] }
    ]
  }
}
```

The hook `timeout` is in seconds, not milliseconds. `soma_lib.py`, `soma_ctx.py` and `soma_compact.py` must sit beside `soma-state.py` (without `soma_compact.py` the hooks run as before, with no compaction notice). Python 3.10+, no dependencies.

## Context window

Claude Code gives the context-window and rate-limit numbers only to the **statusline** command, as JSON on stdin; hooks never receive them. `soma-context.py` is the bridge: the statusline script pipes the same JSON to it, it stores the numbers for that session, and `soma-state.py` reads them at the next prompt. Add one line to your statusline script, after it has read its stdin into `$input`:

```bash
printf '%s' "$input" | ~/.claude/hooks/soma-context.py
```

The bridge prints nothing and always exits 0, whatever it is fed, so it cannot add output or an error to the statusline. It writes `soma-ctx/<session_id>.json` under the state directory (the session id is reduced to `[A-Za-z0-9_-]` first), atomically via a temp file and rename, so a hook reading at the same moment never sees a partial file:

```json
{"ts": 1791320000, "used_pct": 87, "used_tokens": 865627, "window": 1000000,
 "five_hour": {"used_pct": 7, "resets_at": 1791327000},
 "seven_day": {"used_pct": 19, "resets_at": 1791723600}}
```

`used_tokens` is input + cache creation + cache read of `current_usage` (null early in a session); `five_hour` and `seven_day` are null on plans without rate limits. Files of sessions that have been silent for three days are pruned during a write, at most once an hour.

The hook trusts a state file for up to `SOMA_CTX_MAX_AGE_S` (default one day). Context only changes on a model call and every model call refreshes the statusline, so an idle session's numbers stay true overnight; the limit only retires a file after the integration was removed or a session is resumed much later.

**Fallback.** Without a usable state file (no statusline bridge, a stale file, another harness), the hook reads the last assistant `usage` entry of the session transcript, scanning backwards from the end and never more than 4 MiB, and renders `ctx 866k`: tokens only, no percentage and no `(HIGH)`, because the transcript does not carry the window size. With neither source the segment is absent and the line is exactly what it was before. `SOMA_CTX=0` turns the segment off.

## Compaction

When Claude Code compacts a session, the agent keeps a prose summary and loses the concrete things it was holding: file paths, commit hashes, ids, URLs, the user's exact words. It cannot tell what went missing. `soma-compact.py` (one command for both `PreCompact` and `PostCompact`, entries above) tells it, once, in one line, and leaves an index file to get the identifiers back. Zero LLM, no network.

- **PreCompact** indexes the span about to be compacted: main-chain entries after the previous compaction boundary (or from the start) up to the end, read backwards and never more than 64 MiB. Indexed are the user's text, the assistant's text and every string in a tool call's input; tool results are not (an `ls` of a thousand files is not something the agent was holding), except agent ids written as `agentId: <id>`. Sidechain (subagent) entries, meta entries and system reminders (the harness re-injects those) are skipped.
- **PostCompact** diffs that index against `compact_summary` and the messages the harness kept verbatim (`compactMetadata.preservedMessages` of the boundary entry, when it is already in the transcript). Without a fresh PreCompact index it builds one from the transcript itself, so either order of the harness's writes works.
- The next hook to run for that session, the prompt hook or the pulse (a mid-turn automatic compaction has no prompt after it), appends the notice as the line's last segment, emitting even on a healthy box, and marks it said. A subagent's tool call never consumes it; a notice nobody announced within 24 hours is dropped.

```
[system-state] mem 35.2G/61G avail 57% · swap 0 · / 41% · load 2/16 · ctx 4% (38k/1000k) · compacted 20:41 (866k→38k) · dropped: 37 paths, 12 hashes, 9 ids → /root/.claude/state/soma-compact/<sid>-<epoch>.md
```

Classes with nothing dropped are left out; with nothing dropped at all it reads `compacted 20:41 (866k→38k) · nothing dropped`. The token figures come from the boundary's `compactMetadata` and are left out when unknown. From a session's second compaction on it reads `compacted ×2 20:41 ...`: a summary of a summary deserves less trust. The emission is logged with the flag `COMPACT`.

The **index file** (Markdown, mode 0600 because it holds the user's words) lists the dropped identifiers per class (paths, hashes, ids, urls, agents), most-mentioned first, at most 500 per class with the true count stated, then "User messages, verbatim": every human-typed message of the compacted span in order (each capped at 2000 characters, the newest 200 kept; tool results, hook output, system reminders and meta entries excluded). The classes: absolute paths with at least two segments (not a URL's path), hex hashes of 7 to 40 characters with at least one digit and one letter, `#123` style ids of 3 to 7 digits, `http(s)` URLs, agent ids.

**Limits, plainly.** It recovers identifiers, not reasoning: why a path mattered is gone with the summary. "Kept" means a literal occurrence in the summary or in a preserved message (a path also counts as kept when its basename occurs as a whole token), so an identifier the summary paraphrases counts as dropped, and one it mentions in passing counts as kept. State lives in `soma-compact/` under the state directory (`<sid>.pre.json`, `<sid>.json`, `<sid>-<epoch>.md`), pruned after three days. `SOMA_COMPACT=0` turns all of it off.

## Configuration

All thresholds are `SOMA_*` environment variables. Defaults are tuned for a large-RAM workstation/server; lower them on small boxes.

| Variable | Default | Meaning |
|----------|---------|---------|
| `SOMA_MODE` | `pressure` | `pressure` (quiet unless notable), `always` (emit every turn), `off` |
| `SOMA_PULSE` | `transition` | mid-turn hook gate: `transition` (emit when a flag appears or a chronic one clears), `off` |
| `SOMA_PULSE_FORMAT` | `json` | pulse output: `json` (the PostToolUse `additionalContext` envelope, the only form the model receives) or `plain` |
| `SOMA_PULSE_HOLD_S` | `300` | seconds a chronic flag must stay absent before its recovery is announced; `0` disables the hold |
| `SOMA_MEM_AVAIL_PCT` | `15` | flag when available RAM drops below this percent of total |
| `SOMA_SWAP_MB` | `256` | flag when swap-in-use exceeds this many MB |
| `SOMA_DISK_PCT` | `85` | flag when any watched mount exceeds this percent used |
| `SOMA_LOAD_RATIO` | `1.0` | flag when 1-min load / cores exceeds this |
| `SOMA_TOP_RSS_PCT` | `25` | flag the top process when its private (anonymous) memory exceeds this percent of total RAM; `0` disables |
| `SOMA_TEMP_CPU` | `85` | degC ceiling for the CPU sensor class (k10temp, coretemp, ...); `0` disables |
| `SOMA_TEMP_DISK` | `70` | degC ceiling for the disk sensor class (nvme, drivetemp); `0` disables |
| `SOMA_TEMP_GPU` | `90` | degC ceiling for the GPU sensor class (amdgpu, i915, ...); `0` disables |
| `SOMA_TEMP_RAM` | `80` | degC ceiling for the RAM sensor class (jc42, spd5118); `0` disables |
| `SOMA_TEMP_BOARD` | `90` | degC ceiling for the board sensor class (pch_*); `0` disables |
| `SOMA_TEMP_WIFI` | `80` | degC ceiling for the wifi sensor class (iwlwifi*); `0` disables |
| `SOMA_TEMP_ACPI` | `90` | degC ceiling for ACPI thermal zones (acpitz); `0` disables |
| `SOMA_PSI_PCT` | `25` | flag `STRAIN` when any PSI `some` avg10 stall share crosses this percent; `0` disables |
| `SOMA_MEM_TTE_H` | `2` | flag `DRAIN` when RAM would empty within this many hours (and is already below half) |
| `SOMA_DISK_TTF_H` | `24` | flag `FILL` when a watched mount would fill within this many hours |
| `SOMA_TOP_GROWTH_GBH` | `0.5` | flag `GROW` when the top process gains private memory faster than this many GB/h; `0` disables |
| `SOMA_TREND_ANCHOR_S` | `1800` | rolling anchor age for rate computation; rates are measured over at least this window |
| `SOMA_SELF_RSS_PCT` | `40` | flag `SELF` when the agent's own process tree's private memory exceeds this percent of total RAM; `0` disables |
| `SOMA_SELF_COMM` | `claude,node` | comm names recognized as the harness ancestor when walking up from the hook |
| `SOMA_MOUNT_TIMEOUT_MS` | `150` | shared deadline for all mount probes; a probe that misses it reports the mount as numb |
| `SOMA_STEAL_PCT` | `10` | flag `STEAL` when hypervisor steal share over the trend window crosses this percent; `0` disables |
| `SOMA_MOUNTS` | `/,/root/work` | comma-separated mounts to check (duplicate filesystems are deduped) |
| `SOMA_SERVICES` | *(empty)* | comma-separated services to probe; empty means no `systemctl` call |
| `SOMA_CTX` | `1` | context-window and rate-limit segment; `0`, `off`, `false`, `no` disable, anything else enables |
| `SOMA_CTX_PCT` | `85` | mark `ctx` `(HIGH)` and emit in pressure mode when the context window is at least this percent full; `0` disables the mark |
| `SOMA_CTX_MAX_AGE_S` | `86400` | oldest statusline state file the hook still trusts; older falls back to the transcript |
| `SOMA_LOG` | `1` | append each emission to the log; `0` disables |
| `SOMA_COMPACT` | `1` | compaction awareness (the `soma-compact.py` hook and the notice); `0`, `off`, `false`, `no` disable |
| `SOMA_STATE_DIR` | `~/.claude/state` | where `soma-log.jsonl`, `soma-state.json`, `soma-ctx/`, `soma-pulse/` and `soma-compact/` are written (falls back to `CLAUDE_KIT_STATE_DIR`) |

## Relationship to the research

Soma is a deliberate **generalization experiment**. Kairos established that injecting an orthogonal orienting signal (time) measurably shapes behavior. Soma asks whether the same mechanism, applied to a different orthogonal axis (the physical substrate), produces the same kind of value. If it does, the underlying claim generalizes beyond time. If it does not, that is a falsification boundary worth knowing.

Soma is its **own** project and its own axis. It is not part of, and does not modify, Kairos or the temporal-cognition argument; that paper's strength is its clean single-axis claim, and Soma is kept separate to preserve it.

## License

MIT. See [LICENSE](LICENSE).
