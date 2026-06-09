# Changelog

All notable changes to Soma. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows [SemVer](https://semver.org/spec/v2.0.0.html).

Soma is pre-1.0: minor bumps may include incompatible changes when the cost of carrying compatibility shims would outweigh the value. Patch releases (0.x.y where y > 0) are bug-fix only.

## [Unreleased]

Next probable: an emission-vs-behavior evaluator (replay `soma-log.jsonl` against transcripts) to measure whether body-state injection shifts decisions, the falsifiable test of whether the orienting mechanism generalizes off the time axis.

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
