# Changelog

All notable changes to Soma. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows [SemVer](https://semver.org/spec/v2.0.0.html).

Soma is pre-1.0: minor bumps may include incompatible changes when the cost of carrying compatibility shims would outweigh the value. Patch releases (0.x.y where y > 0) are bug-fix only.

## [Unreleased]

Next probable: efference-copy tagging (mark strain as self-caused when it follows the agent's own heavy tool calls vs unexplained), and the cheap-sense backlog (inode pct, reboot recency, clock-sync guard, battery/VRAM classes).

## [0.10.1] - 2026-10-06

### Fixed
- **The pulse reached nobody.** Claude Code does not pass a PostToolUse hook's plain stdout to the model; only `{"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "..."}}` on stdout (exit 0) does. `soma-pulse.py` printed plain text, so every pulse since the hook was built went unheard (1397 logged emissions on the reference host). It now prints that JSON; `SOMA_PULSE_FORMAT=plain` keeps the old line for harnesses that read stdout (default `json`). The prompt hook keeps plain stdout (UserPromptSubmit works that way).
- **Flapping.** Silent, a value hovering on a threshold cost nothing (six days on the reference host: HOT toggled 181 times, DRAIN 42, LOAD 30, LOW_MEM 13); audible it would be spam. New `SOMA_PULSE_HOLD_S` (default 300, 0 = off): a flag is announced once, counts as cleared only after staying absent for the hold time without a break, and only then is the recovery announced. A reappearance inside the window is not a transition, across prompts too: a prompt that prints nothing keeps the told flags and starts the absence clock of each one that is gone (one already absent keeps its time), and the pulse announces the recovery once the hold has passed. Acute flags (OOM, ECC) are never held: every new kill or error is announced, their clearing never. `pulse_transition()` is the new pure gate; `should_pulse()` is unchanged.
- **One session ate another's announcement.** The told-state lived in the host-wide `soma-state.json`, so with two sessions (or a subagent) the first tool call consumed the transition. The pulse now parses its stdin and keeps what each session was told in `<state dir>/soma-pulse/<session_id>.json` (shared `write_session_json` / `read_session_json` in `soma_ctx.py`, same atomic write and 3-day pruning as the statusline bridge). The prompt hook records what it told the session, so the next pulse does not repeat it. With no `session_id` on stdin, or with `soma_ctx.py` absent or older, the pulse falls back to host-wide behaviour (`pulse_held` in `soma-state.json`). A tool call inside a subagent (stdin carries `agent_id`, confirmed in the hooks reference) samples and persists the trend anchor and last flags but announces nothing, leaves the session's told-state alone and does not advance the host-wide counters, so the main agent hears the transition or the kill itself.
- **Acute events per session (pre-release verification round).** OOM and ECC were deltas of host-wide counters against the baseline in `soma-state.json`, so whichever sample ran first (a subagent's tool call, or another session) consumed the event and the main agent never heard the kill. With a `session_id`, the session file now also holds that session's own baseline of the kernel's cumulative `oom_kill`, `edac_ce` and `edac_ue`; an acute event is the counter above that baseline, so each session hears it once, independently, and the line states the count since that session's baseline (three kills seen across two samples: `oom-kill 1`, then `oom-kill 2`). Only the session's own main-agent samples advance it, the pulse and the prompt hook; a subagent's call never touches the session file. This holds from a session's second contact on. Its first contact (no session file, or one without a counter baseline, such as a file from an earlier 0.10.1 build), through the prompt hook or the pulse, reports the host-wide delta since the host's last sample, as v0.10.0 did (typically the kill that took down the previous session), then stores its own baseline; a host with no counters recorded yet reports nothing. A counter below the baseline (reboot) re-baselines silently. Without a `session_id` the acute baseline stays host-wide, as before; trends, the anchor and the level flags' host record stay host-wide in every case.
- **Acute flags were stored in the told-state**, so a second kill at the session's next sample was silent and a new session could be seeded with OOM. Acute flags now never enter `held` (session or host-wide) and never seed one.
- **A new session was seeded late and from another session's state.** The prompt hook wrote the session file only when it emitted, and a first pulse with no file seeded from the host's `last_flags`, so a session could stay silent about a condition it was never told. The prompt hook now writes the session file on every prompt with a `session_id` (except `SOMA_MODE=off`): exactly the level flags on its line (a flag absent from the line counts as told cleared, so the pulse does not announce that recovery again), or, when it stayed silent, the flags told before with their absence clocks started (see Flapping), plus the counter baseline. A first pulse with no session file treats the session as told nothing: it announces the standing level flags once, plus the host-wide acute events of a first contact, and baselines the counters at their current values.
- **Announce only what was recorded.** With an unwritable state dir (or a full disk, exactly when DISK flags) nothing persisted and the same line was injected after every tool call. The pulse now prints nothing unless its told-state (the session file, or `soma-state.json` without a `session_id`) was written. `save_state()` returns whether it wrote.
- **Torn `soma-state.json`.** `save_state()` wrote through one shared `soma-state.json.tmp`, so parallel hooks could tear it and the next `load_state()` returned `{}`, resetting the baselines. It now writes a pid-unique temp file, `os.replace`s it and removes a leftover temp on failure.
- **Junk in a session file** is dropped on read: a key that is not a level-flag name (it produced a phantom recovery), a value that is not null or a finite number no later than now + 300 s (`-Infinity` recovered at once; booleans were taken as null), counters that are not non-negative integers (re-baselined). The 64 KB file cap and 64-entry cap stay.
- **Mixed adapters.** A write with a `session_id` popped the host-wide `pulse_held`, degrading an adapter without session ids on the same host; it is left alone now.
- **Known limit.** Two pulses of the same session started within microseconds of each other can both announce the same transition (no lock; 0 of 30 with the real script, seen only under a synchronised barrier).
- **Docs.** Hook `timeout` is in seconds (a 5000 timeout ran a 20 s sleep to completion): README examples now say `2`, not `2000`, with a note. `SOMA_TREND_ANCHOR_S` default is 1800 since 0.9.1; the README table said 600 and the prose 10 min, both now say 1800 s / 30 min.
- Tests: 140 to 201. The 0.10.0-compatibility test runs the real v0.10.0 `soma_ctx.py` (from git, skipped when git or the tag is unavailable) instead of a stub, through both hooks. The hostile-stdin test also runs with a forced flag and asserts exactly one JSON object of the documented shape. `test_new_session_is_not_told_chronic_condition` (encoded the old seeding rule) is replaced by `test_first_contact_without_session_file_is_told_standing_flags_once`; `test_hook_timing` (could not fail) is removed; the pid-unique temp test now asserts the temp name it saw, so a shared name fails it; the timing is reported instead: about 62 ms end to end, unchanged from 0.10.0.

## [0.10.0] - 2026-10-06

The agent feels its own context window and its plan's quota.

### Added
- **Context-window and rate-limit segment**: `ctx 72% (720k/1000k) · 5h 7% · 7d 19%` at the end of the `[system-state]` line. Claude Code hands these numbers only to the statusline command, so the new `hooks/soma-context.py` is a bridge a statusline script pipes its stdin JSON to (`printf '%s' "$input" | ~/.claude/hooks/soma-context.py`). It writes `<state_dir>/soma-ctx/<session_id>.json` (session id reduced to `[A-Za-z0-9_-]`, temp file plus rename), prints nothing and exits 0 on any input, and prunes files of sessions silent for three days at most once an hour. Motivation: an agent at 87% can save state before compaction, and one that sees the quota knows how much parallel effort is left. The quota figures never make the line emit by themselves; they ride along when something else does (or in `SOMA_MODE=always`).
- **`(HIGH)` at `SOMA_CTX_PCT`** (default 85, `0` disables the mark), the same marker disk and load use. A high fill makes the line emit in pressure mode on every prompt while the fill is at or above the threshold (level-triggered, like the body flags); it is logged as flag `CTX` in `soma-log.jsonl` but kept out of `last_flags`, so the pulse hook does not read it as a transition.
- **Freshness**: the hook trusts a state file for `SOMA_CTX_MAX_AGE_S` (default 86400). Context changes only on a model call, which also refreshes the statusline, so an idle session's numbers stay true overnight. A rate-limit window whose `resets_at` has passed is dropped.
- **Transcript fallback**: without a usable state file the hook reads the last assistant `usage` from the session transcript (backwards from the end, bounded to 4 MiB, sidechain and zero-usage entries skipped) and renders `ctx 866k`, no percentage, since the transcript does not carry the window size. Measured 0.05 to 0.10 ms typical (62 MB and 196 MiB live transcripts); worst bounded case measured 2.8 to 16.1 ms on this box (Ryzen 7 PRO 8700GE), the slow end being 4 MiB of dense non-assistant lines that each carry a `usage` key (15.3 to 16.1 ms), 6.5 ms for 2 KB such lines.
- **`SOMA_CTX`** turns the segment off for `0`, `off`, `false`, `no` (case-insensitive); anything else, including empty, leaves it on. With no context data the line is byte-identical to 0.9.2.
- `hooks/soma_ctx.py` holds both halves (json/os/time only, so the statusline bridge starts fast); `soma_lib._state_dir()` now delegates to it. `soma-state.py` passes its stdin JSON (`session_id`, `transcript_path`) to `line_for_mode(hook_input=...)`. Adapters that run `soma-state.py` with the hook payload inherit the segment.
- **Hardening (pre-release verification round)**: `soma_lib` imports `soma_ctx` guarded, so a missing, broken or older `soma_ctx.py` yields exactly the 0.9.2 line (same state-dir chain, no ctx segment) in both hooks instead of a traceback and no output. `soma-state.py` survives any stdin (absurdly nested JSON no longer raises RecursionError). The transcript fallback opens only a regular file (a FIFO with no writer used to block the hook). Ranges are enforced by writer and reader: context fill 0..100, rate windows 0..1000, token counts and window sizes non-negative and bounded, anything else is absent. `resets_at` accepts epoch milliseconds (above 1e11 is divided by 1000); other junk stays null. `SOMA_CTX_PCT` and `SOMA_CTX_MAX_AGE_S` fall back to their defaults on NaN, infinite or negative values. Session ids cap at 128 characters (256 plus suffixes exceeded NAME_MAX and silently wrote nothing). A `.pruned` marker with a future mtime counts as due. A transcript fallback under 1000 tokens renders nothing instead of `ctx 0k`.
- 68 new tests (140 total).

### Changed
- Public author identity is Mikael Wedlund (`CITATION.cff`, LICENSE, README). The GitHub account remains `draca-glitch`.

## [0.9.2] - 2026-09-03

The top slot ranks on private memory; mmap-heavy processes no longer mask the real consumer.

### Fixed
- **`top_rss()` ranked on resident set**, so any process that memory-maps large files won the "top process" slot with reclaimable page cache and hid the genuine consumer. Observed on a 30.8G NUC: `top qbittorrent-nox 15.5G(49.2%)(TOP)` with 25G available and memory PSI at zero; that process held 0.17G anonymous and 14.6G file-backed, while mnemos at 2.72G anonymous, the process with a documented memory profile worth watching, was invisible. Ranking now uses anonymous memory (statm resident minus shared, same read, no extra syscall). `rss_kb` stays in the entry; `anon_kb` is added.
- **`self_tree_rss()`** sums anonymous memory the same way, so the agent's own tree is not inflated by whatever it has mapped.
- **`assess()`, `compute_trends()`, `snapshot_anchor()`** measure `TOP`, `SELF` and `GROW` on `anon_kb`, falling back to `rss_kb` for state written before this release (one anchor window, then consistent).
- **Rendering**: private memory is the primary figure; the resident total is appended only when file-backed pages dominate and exceed 256M: `top qbittorrent-nox 174M(0.6%, 15.5G mapped)`. Anon-heavy processes render exactly as before.
- Thresholds `SOMA_TOP_RSS_PCT` (25) and `SOMA_SELF_RSS_PCT` (40) keep their defaults and names: they were tuned against anon-heavy consumers (the README example is a 15.5G mnemos-mcp), and the mmap cases they used to fire on were the false positives this release removes. They now measure what they were meant to.
- 8 new tests (72 total).

## [0.9.1] - 2026-07-07

Trend rates no longer over-extrapolate short bursts.

### Fixed
- **Burst over-extrapolation in `compute_trends()`**: the rate window floor was 1 minute, so a short real burst (mnemos-mcp loading ONNX models, a few GB over ~2 minutes) divided by a tiny dt produced absurd GB/h readings (observed +25.4G/h GROW and -12.4G/h DRAIN on a healthy box). New floor `SOMA_TREND_MIN_DT_S` (default 900) bounds worst-case extrapolation to 4x a burst's real delta.
- **`SOMA_TREND_ANCHOR_S` default raised 600 -> 1800** so the anchor window comfortably exceeds the new floor; rates are now measured over 15-30 minute windows.
- **Floor/anchor dead-lock guard**: the floor clamps to 0.75 * anchor_s; without this, a floor at or above the anchor refresh period keeps dt below the floor forever and trends go permanently silent.
- 2 new tests (64 total).

## [0.9.0] - 2026-06-11

Full-body thermoception: every hwmon chip a small machine actually carries.

### Added
- **Four new temperature classes**: `ram` (jc42, spd5118 DIMM sensors), `board` (`pch_*` chipset zones), `wifi` (`iwlwifi*`), `acpi` (acpitz catch-all zone), with ceilings `SOMA_TEMP_RAM` (80), `SOMA_TEMP_BOARD` (90), `SOMA_TEMP_WIFI` (80), `SOMA_TEMP_ACPI` (90); `0` disables a class as before. Flag and rendering logic were already class-generic, so the line grows new segments with no other changes. Motivating box: a NUC whose warmest parts (PCH 52°C, SO-DIMMs 49°C) were exactly the ones Soma could not feel.
- **Prefix matching** for family- or instance-suffixed chip names (`pch_cannonlake`, `iwlwifi_1`) via `CHIP_PREFIXES`, alongside the exact-name `CHIP_CLASSES` map.
- **Bogus-reading filter**: temperatures outside -40..150°C are dropped; ACPI zones publish placeholder sensors near absolute zero (-263°C) for trip points the firmware never wired up.
- 1 new test (62 total).

## [0.8.0] - 2026-06-10

The shared-apartment release: what a guest can and cannot feel.

### Added
- **Steal sense.** `read_jiffies()` reads aggregate cpu jiffies from `/proc/stat`; `compute_trends()` derives the hypervisor steal share over the trend-anchor window. Rendered as `steal 12%` once above noise (0.5%), flagged `STEAL` past `SOMA_STEAL_PCT` (default 10, `0` disables). Steal is the one sense that exists specifically for virtualized guests: cycles the host took while the guest had work to run, invisible to load average. On dedicated hardware it stays at 0 and the segment never renders. Counter resets (reboot) are guarded.
- **README section on virtualized hosts**: temperature, EDAC, RAID, and disk-sensor classes go dark inside a guest by design (the hypervisor owns that hardware); each degrades to an absent key and an absent segment, so a VPS deployment is quieter, never broken. PSI, OOM, swap, disk fill, numb mounts, self-vs-world, and all trends work identically.
- 6 new tests (61 total).

## [0.7.0] - 2026-06-10

The falsifiability layer. Soma now measures whether anyone listens to it.

### Added
- **`analyze-emission-behavior.py`**: joins `soma-log.jsonl` against Claude Code session transcripts and reports, per flag class, whether the agent acknowledged the condition in its response text, acted on it (flag-specific investigation commands), and at what latency (tool calls before first reaction). Healthy always-mode emissions (empty flag set) form the control population: a line of identical shape carrying no notable condition. Flagged-vs-control ack/act rates are the behavior-shift signal, the falsifiable test of whether orienting injection generalizes off the time axis (the sibling Kairos project documents the temporal version). The response window is configurable (default 600s) and closes at the next user turn, so credit never leaks across turns. Privacy: reads transcripts in place, emits aggregate counts only. 6 tests (55 total).
- First live run on the 24-emission corpus already separates populations: GROW acknowledged at 50%, TOP at 20%, healthy controls at 0%.

## [0.6.0] - 2026-06-10

The body boundary, and limbs that stop answering.

### Added
- **Self vs world.** `self_tree_rss()` walks from the hook to the nearest ancestor whose comm matches `SOMA_SELF_COMM` (default `claude,node`) and sums RSS over that ancestor's entire subtree: the harness, its MCP servers (the agent's organs), and any running tool subprocesses (the agent's own effort). Rendered as `self claude[14] 12.1G(19.5%)`; flags `SELF` past `SOMA_SELF_RSS_PCT` (default 40, `0` disables). Falls back to the hook's immediate parent when no harness ancestor is found. "I am heavy" and "the world is heavy" are different facts and now distinguishable.
- **Numb-limb watchdog.** `disk_usage()` now probes every mount in parallel watchdog threads under one shared deadline (`SOMA_MOUNT_TIMEOUT_MS`, default 150). A probe that misses the deadline reports the mount in `numb:` with flag `NUMB` instead of blocking; previously a hung network mount (VPN drop under CIFS/NFS) would hang the hook, and with it the prompt, for the hook timeout. Soma's own worst failure mode is now its most valuable mount signal. Returns `{mounts, numb}` instead of a bare list (pre-1.0 breaking change).
- `NUMB` and `SELF` are chronic flags: the pulse hook announces them once on appearance and once on recovery.
- 7 new tests (49 total).

## [0.5.0] - 2026-06-09

Sampling while moving. Until now Soma only fired when the human spoke; the body changes most while the AGENT acts (builds, benches, parallel subagents), and that entire window was blind.

### Added
- **`hooks/soma-pulse.py` (PostToolUse).** Samples the body after every tool call, emits only on a flag transition: a flag appeared, or a chronic condition cleared (one recovery line). Acute pain flags (OOM, ECC) clearing is the delta baseline advancing, not a recovery, and stays silent; `should_pulse()` encodes the gate. A long healthy turn costs zero lines. `SOMA_PULSE=transition|off`.
- `last_flags` persisted in `soma-state.json` by both hooks, so a condition announced at prompt time is not re-announced by the first pulse, and vice versa.
- Emission log records gain a `src` field (`state` or `pulse`) so the evaluator can separate prompt-time orientation from mid-turn interruption when measuring behavior shift.
- 3 new tests (42 total).

## [0.4.0] - 2026-06-09

The movement sense. Proprioception detects velocity, not just position: "85% used" is ambiguous, "full in ~6h" is actionable. Builds on the state file introduced in 0.3.0.

### Added
- **Trend rates against a rolling anchor.** `roll_state()` keeps a trend anchor in `soma-state.json`, refreshing it only once it ages past `SOMA_TREND_ANCHOR_S` (default 600s), so rates are measured over a stable window even when readings arrive seconds apart. `compute_trends()` derives GB/h rates: RAM drain (with hours-to-empty), per-mount fill (with hours-to-full), and top-process RSS growth (only while the same process holds the top spot).
- **Flags**: `DRAIN` (RAM empties within `SOMA_MEM_TTE_H`, default 2h, and is already below half, so a big one-off allocation on a mostly-free box does not alarm), `FILL` (mount fills within `SOMA_DISK_TTF_H`, default 24h), `GROW` (top process gaining over `SOMA_TOP_GROWTH_GBH`, default 0.5 GB/h).
- **Rendering**: rate annotations appear only on flagged segments, e.g. `mem 9.5G/61G avail (-12.0G/h, empty ~1.5h)(DRAIN)`, `top mnemos-mcp 11G(17%) (+0.7G/h)(GROW)`, `fill / +8.0G/h (full ~10h)(FILL)`. Healthy lines look exactly as before; the quiet aesthetic survives.
- Temperature slope was considered and rejected: thermal time constants are seconds, so a minutes-scale slope is noise. Temps stay level-gated.
- 8 new tests (39 total).

## [0.3.0] - 2026-06-09

Two new senses, plus the persistence they require. Proprioception is not just position (levels); this release adds strain (how hard is the body working to stand still) and pain (what got damaged since you last checked).

### Added
- **Strain sense: PSI.** `read_psi()` parses `/proc/pressure/{cpu,memory,io}` (`some` avg10). New segment `psi 1/0/38%` (cpu/mem/io order); any resource crossing `SOMA_PSI_PCT` (default 25, `0` disables) flags `STRAIN` with the offenders named, e.g. `(STRAIN:io)`. PSI separates busy-and-fine from wedged, which load average structurally cannot. Absent on kernels without CONFIG_PSI; the segment simply does not render.
- **Pain channel: damage events via counter deltas.** `read_counters()` reads lifetime counters (`oom_kill` from `/proc/vmstat`, ECC corrected/uncorrected error counts from EDAC sysfs) and the live `md*/md/degraded` state. `diff_events()` reports positive deltas against the previous reading: flags `OOM`, `ECC`, `RAID`; rendered as e.g. `pain oom-kill 2 ecc-ce +3`. Acute events fire exactly once (the baseline then advances); a degraded array is chronic and reported every reading until rebuilt. Counter resets (reboot) produce no false pain. First run establishes the baseline silently.
- **Persisted state.** `soma-state.json` in the state dir (atomic replace, never raises) carries the counter baseline between readings; foundation for trend rates in the next release.
- `gather()`/`line_for_mode()` take `sys_root` and `state_dir` parameters for hermetic tests.
- 9 new tests (31 total).

## [0.2.0] - 2026-06-09

### Added
- **Temperature sensing.** `read_temps()` reads sysfs hwmon and reports the hottest sensor per class: `cpu` (k10temp, coretemp, zenpower, cpu_thermal), `disk` (nvme, drivetemp), `gpu` (amdgpu, radeon, i915, nouveau). Unknown chips (VRM, chipset, ACPI zones) are ignored. The rendered line gains a `temp cpu 42 disk 30 gpu 39°C` segment when sensors exist; a class crossing its ceiling is tagged `(HOT)` and trips pressure-mode emission. New thresholds: `SOMA_TEMP_CPU` (85), `SOMA_TEMP_DISK` (70), `SOMA_TEMP_GPU` (90), each `0` to disable. DIMM temperatures are not read: the spd5118 driver that exposes DDR5 SPD-hub sensors only landed in kernel 6.10+, and jc42 coverage is rare on servers; the class can be added when hardware exposes it.
- `gather()` and `line_for_mode()` take an `hwmon_root` parameter (default `/sys/class/hwmon`) so tests and replayers can point at a fake tree.
- 7 new tests (22 total).

## [0.1.1] - 2026-06-03

### Fixed
- `[system-state]` now shows the top-process RAM share to one decimal (e.g. `24.6%`) instead of rounding to a whole number. A true 24.6% rounded to `25%`, which read as if it sat at the `SOMA_TOP_RSS_PCT` threshold while the gate (correctly, on the true value) did not flag it. The decimal removes the apparent contradiction between the displayed number and the absent `(TOP)` flag.

## [0.1.0] - 2026-06-03

**First cut.** The body axis ships as a single UserPromptSubmit hook.

- `hooks/soma_lib.py`: pure-stdlib readings (`/proc/meminfo`, `/proc/loadavg`, top-RSS process, `statvfs` disk, optional `systemctl` service probe), threshold gate, one-line renderer, emission log.
- `hooks/soma-state.py`: the UserPromptSubmit hook. Reads stdin, skips task-notifications, emits one `[system-state]` line, never raises into the prompt path.
- Default `pressure` mode: silent unless a threshold is crossed. `always` and `off` modes available.
- Threshold gate covers low available memory, swap in use, full disk, high load, a dominant top-RSS process, and down services. All `SOMA_*` env-tunable.
- Emissions logged to `soma-log.jsonl` as a falsifiability substrate.
- 15 unit tests over parsing, the gate, rendering, and mode behavior.
