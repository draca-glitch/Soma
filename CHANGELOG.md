# Changelog

All notable changes to Soma. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows [SemVer](https://semver.org/spec/v2.0.0.html).

Soma is pre-1.0: minor bumps may include incompatible changes when the cost of carrying compatibility shims would outweigh the value. Patch releases (0.x.y where y > 0) are bug-fix only.

## [Unreleased]

Next probable: efference-copy tagging (mark strain as self-caused when it follows the agent's own heavy tool calls vs unexplained), and the cheap-sense backlog (inode pct, reboot recency, clock-sync guard, battery/VRAM classes).

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
