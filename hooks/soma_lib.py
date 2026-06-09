"""
Shared system-state primitives for soma hooks.

Soma is the body axis of the self-grounding triad (Soma=body, Kairos=time,
Mnemos=memory). It answers "what physical state is the host in?" the way
Kairos answers "when am I?". Pure proprioception: read /proc, gate on
notable conditions, inject one orienting line. It senses, it never acts.

Single source of truth for: /proc parsing, top-RSS discovery, disk and
service probing, threshold gating, one-line rendering. Pure stdlib.

Used by:
  - soma-state.py (emits [system-state] summary line on UserPromptSubmit)
  - future evaluators that replay the emission log

Design rules (mirrors the sibling Kairos):
  - never raise into the hook; degrade to a partial reading or silence
  - default-quiet: in pressure mode, emit ONLY when something crosses a
    threshold. A healthy box stays silent. This is the deliberate fix for
    the noise that craters a chatty advisory layer's usefulness.
  - orient, do not decide: the line states the body's condition, nothing
    about what to do with it.
"""

import json
import os
import subprocess
from pathlib import Path

PAGE_KB = (os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096) / 1024


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def thresholds() -> dict:
    """Gate thresholds, all SOMA_* env-overridable. Tuned for a 64G box."""
    return {
        "mem_avail_pct": _env_float("SOMA_MEM_AVAIL_PCT", 15.0),   # free RAM floor
        "swap_mb": _env_int("SOMA_SWAP_MB", 256),                  # swap-in-use ceiling
        "disk_pct": _env_float("SOMA_DISK_PCT", 85.0),             # mount-full ceiling
        "load_ratio": _env_float("SOMA_LOAD_RATIO", 1.0),          # load1 / cores ceiling
        "top_rss_pct": _env_float("SOMA_TOP_RSS_PCT", 25.0),       # single-process RAM share worth noting; 0 disables
        "temp_cpu": _env_float("SOMA_TEMP_CPU", 85.0),             # degC ceilings per sensor class; 0 disables a class
        "temp_disk": _env_float("SOMA_TEMP_DISK", 70.0),
        "temp_gpu": _env_float("SOMA_TEMP_GPU", 90.0),
        "psi_pct": _env_float("SOMA_PSI_PCT", 25.0),               # PSI some/avg10 stall-share ceiling; 0 disables
    }


def parse_meminfo(text: str) -> dict:
    """/proc/meminfo -> {MemTotal, MemAvailable, SwapTotal, SwapFree} in kB."""
    out = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
            try:
                out[key] = int(rest.strip().split()[0])
            except (IndexError, ValueError):
                continue
    return out


def parse_loadavg(text: str) -> tuple:
    parts = text.split()
    try:
        return (float(parts[0]), float(parts[1]), float(parts[2]))
    except (IndexError, ValueError):
        return (0.0, 0.0, 0.0)


def top_rss(proc_root: str = "/proc") -> dict | None:
    """Process with the largest resident set. {name, rss_kb} or None.

    Reads statm (resident pages) + comm per pid. Races (pid exiting
    mid-scan) are skipped, not fatal.
    """
    best = None
    root = Path(proc_root)
    try:
        entries = list(root.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            resident_pages = int((entry / "statm").read_text().split()[1])
        except (OSError, IndexError, ValueError):
            continue
        rss_kb = int(resident_pages * PAGE_KB)
        if best is None or rss_kb > best["rss_kb"]:
            try:
                name = (entry / "comm").read_text().strip()
            except OSError:
                name = f"pid{entry.name}"
            best = {"name": name, "rss_kb": rss_kb}
    return best


def disk_usage(paths: list) -> list:
    """statvfs each path; dedupe identical filesystems. [{mount, pct, free_kb}]."""
    seen = set()
    out = []
    for p in paths:
        try:
            st = os.statvfs(p)
        except OSError:
            continue
        sig = (st.f_blocks, st.f_bavail)
        if st.f_blocks == 0 or sig in seen:
            continue
        seen.add(sig)
        used = st.f_blocks - st.f_bfree
        pct = used / (used + st.f_bavail) * 100 if (used + st.f_bavail) else 0.0
        out.append({"mount": p, "pct": pct, "free_kb": st.f_bavail * st.f_frsize // 1024})
    return out


def read_psi(proc_root: str = "/proc") -> dict:
    """Stall share per resource from /proc/pressure, "some" avg10. {cpu|memory|io: pct}.

    PSI is the strain sense: the percent of the last 10s in which at least
    one task stalled waiting on the resource. It separates busy-and-fine
    (high load, zero stall) from wedged (low load, high stall), which load
    average cannot. Absent on kernels without CONFIG_PSI; keys are simply
    missing then.
    """
    out = {}
    for res in ("cpu", "memory", "io"):
        try:
            text = (Path(proc_root) / "pressure" / res).read_text()
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith("some "):
                for tok in line.split():
                    if tok.startswith("avg10="):
                        try:
                            out[res] = float(tok.split("=", 1)[1])
                        except ValueError:
                            pass
                break
    return out


def read_counters(proc_root: str = "/proc", sys_root: str = "/sys") -> dict:
    """Damage counters and degraded-state levels for the pain channel.

    oom_kill / edac_ce / edac_ue are lifetime counters, meaningful only as
    deltas against the previous reading; md_degraded is a current level.
    Keys are absent when the kernel does not expose the source.
    """
    out = {}
    try:
        for line in (Path(proc_root) / "vmstat").read_text().splitlines():
            if line.startswith("oom_kill "):
                out["oom_kill"] = int(line.split()[1])
                break
    except (OSError, ValueError, IndexError):
        pass
    for kind in ("ce", "ue"):
        total = None
        for f in Path(sys_root, "devices/system/edac/mc").glob(f"mc*/{kind}_count"):
            try:
                total = (total or 0) + int(f.read_text().strip())
            except (OSError, ValueError):
                continue
        if total is not None:
            out[f"edac_{kind}"] = total
    degraded = None
    for f in Path(sys_root, "block").glob("md*/md/degraded"):
        try:
            degraded = (degraded or 0) + int(f.read_text().strip())
        except (OSError, ValueError):
            continue
    if degraded is not None:
        out["md_degraded"] = degraded
    return out


def diff_events(counters: dict, prev: dict) -> dict:
    """Pain since the previous reading: positive counter deltas plus live degraded state.

    Negative deltas (counter reset after reboot) are dropped. An empty prev
    (first run) yields no deltas; the baseline simply starts.
    """
    ev = {}
    for key in ("oom_kill", "edac_ce", "edac_ue"):
        if key in counters and key in prev:
            d = counters[key] - prev[key]
            if d > 0:
                ev[key] = d
    if counters.get("md_degraded", 0) > 0:
        ev["md_degraded"] = counters["md_degraded"]
    return ev


def _state_dir(state_dir: str | None = None) -> Path:
    return Path(state_dir or os.environ.get("SOMA_STATE_DIR")
                or os.environ.get("CLAUDE_KIT_STATE_DIR", str(Path.home() / ".claude" / "state")))


def load_state(state_dir: str | None = None) -> dict:
    """Previously persisted reading (counter baseline, trend anchor). {} when absent or corrupt."""
    try:
        doc = json.loads((_state_dir(state_dir) / "soma-state.json").read_text())
        return doc if isinstance(doc, dict) else {}
    except Exception:
        return {}


def save_state(doc: dict, state_dir: str | None = None) -> None:
    """Persist the reading for the next run's deltas. Atomic replace, never raises."""
    try:
        d = _state_dir(state_dir)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "soma-state.json.tmp"
        tmp.write_text(json.dumps(doc))
        tmp.replace(d / "soma-state.json")
    except Exception:
        return


# hwmon chip name -> sensor class. Only classes with a threshold are read;
# anything else (VRM, chipset, ACPI zones) stays out of the line.
CHIP_CLASSES = {
    "k10temp": "cpu", "coretemp": "cpu", "zenpower": "cpu", "cpu_thermal": "cpu",
    "nvme": "disk", "drivetemp": "disk",
    "amdgpu": "gpu", "radeon": "gpu", "i915": "gpu", "nouveau": "gpu",
}


def read_temps(hwmon_root: str = "/sys/class/hwmon") -> dict:
    """Hottest reading per sensor class from sysfs hwmon. {cpu|disk|gpu: degC}.

    max() within a class so multi-device classes (two NVMe drives, several
    CCDs) report their worst sensor. Unknown chips are ignored; missing or
    malformed sysfs entries are skipped, never fatal.
    """
    out = {}
    try:
        chips = list(Path(hwmon_root).iterdir())
    except OSError:
        return out
    for chip in chips:
        try:
            cls = CHIP_CLASSES.get((chip / "name").read_text().strip())
        except OSError:
            continue
        if not cls:
            continue
        for probe in chip.glob("temp*_input"):
            try:
                deg = int(probe.read_text().strip()) / 1000
            except (OSError, ValueError):
                continue
            if cls not in out or deg > out[cls]:
                out[cls] = deg
    return out


def service_states(names: list) -> list:
    """systemctl is-active for a small watchlist. [(name, state)]. Empty list = no probe."""
    if not names:
        return []
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", *names],
            capture_output=True, text=True, timeout=1.5,
        )
        states = proc.stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return [(n, "unknown") for n in names]
    return list(zip(names, states + ["unknown"] * (len(names) - len(states))))


def human_kb(kb: float) -> str:
    mb = kb / 1024
    if mb < 1024:
        return f"{mb:.0f}M" if mb >= 10 else f"{mb:.1f}M"
    g = mb / 1024
    return f"{g:.0f}G" if g >= 100 else f"{g:.1f}G"


def gather(proc_root: str = "/proc", mounts: list | None = None, services: list | None = None,
           hwmon_root: str = "/sys/class/hwmon", sys_root: str = "/sys") -> dict:
    """Read the body's current state. Never raises; missing pieces are absent keys."""
    mounts = mounts if mounts is not None else _env_list("SOMA_MOUNTS", ["/", "/root/work"])
    services = services if services is not None else _env_list("SOMA_SERVICES", [])
    state = {"cores": os.cpu_count() or 1}
    try:
        state["mem"] = parse_meminfo((Path(proc_root) / "meminfo").read_text())
    except OSError:
        state["mem"] = {}
    try:
        state["load"] = parse_loadavg((Path(proc_root) / "loadavg").read_text())
    except OSError:
        state["load"] = (0.0, 0.0, 0.0)
    state["top"] = top_rss(proc_root)
    state["disks"] = disk_usage(mounts)
    state["services"] = service_states(services)
    state["temps"] = read_temps(hwmon_root)
    state["psi"] = read_psi(proc_root)
    state["counters"] = read_counters(proc_root, sys_root)
    return state


def _env_list(name: str, default: list) -> list:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return [s.strip() for s in raw.split(",") if s.strip()]


def assess(state: dict, th: dict | None = None, events: dict | None = None) -> dict:
    """Flag which conditions cross a threshold. {flags: set, mem_avail_pct, swap_mb, load_ratio, top_pct, hot, strained, events}."""
    th = th or thresholds()
    flags = set()
    mem = state.get("mem", {})
    total = mem.get("MemTotal", 0)
    avail = mem.get("MemAvailable", 0)
    swap_used_kb = mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)
    mem_avail_pct = (avail / total * 100) if total else 100.0
    swap_mb = swap_used_kb / 1024
    cores = state.get("cores", 1)
    load1 = state.get("load", (0.0,))[0]
    load_ratio = load1 / cores if cores else 0.0
    top = state.get("top")
    top_pct = (top["rss_kb"] / total * 100) if (top and total) else 0.0

    if total and mem_avail_pct < th["mem_avail_pct"]:
        flags.add("LOW_MEM")
    if swap_mb > th["swap_mb"]:
        flags.add("SWAP")
    if load_ratio > th["load_ratio"]:
        flags.add("LOAD")
    if th["top_rss_pct"] and top_pct > th["top_rss_pct"]:
        flags.add("TOP")
    for mount in state.get("disks", []):
        if mount["pct"] > th["disk_pct"]:
            flags.add("DISK")
    for _, svc_state in state.get("services", []):
        if svc_state not in ("active", "unknown"):
            flags.add("SVC")
    hot = set()
    for cls, deg in state.get("temps", {}).items():
        ceiling = th.get(f"temp_{cls}", 0)
        if ceiling and deg >= ceiling:
            hot.add(cls)
    if hot:
        flags.add("HOT")
    strained = set()
    if th["psi_pct"]:
        strained = {res for res, pct in state.get("psi", {}).items() if pct >= th["psi_pct"]}
    if strained:
        flags.add("STRAIN")
    events = events or {}
    if events.get("oom_kill"):
        flags.add("OOM")
    if events.get("edac_ce") or events.get("edac_ue"):
        flags.add("ECC")
    if events.get("md_degraded"):
        flags.add("RAID")
    return {
        "flags": flags,
        "mem_avail_pct": mem_avail_pct,
        "swap_mb": swap_mb,
        "load_ratio": load_ratio,
        "top_pct": top_pct,
        "hot": hot,
        "strained": strained,
        "events": events,
    }


def render(state: dict, a: dict) -> str:
    """One compact [system-state] line. Crossing fields carry an inline flag."""
    mem = state.get("mem", {})
    total = mem.get("MemTotal", 0)
    avail = mem.get("MemAvailable", 0)
    swap_used = mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)
    parts = []
    if total:
        tag = "(LOW)" if "LOW_MEM" in a["flags"] else ""
        parts.append(f"mem {human_kb(avail)}/{human_kb(total)} avail{tag}")
    if swap_used > 0:
        parts.append(f"swap {human_kb(swap_used)}")
    else:
        parts.append("swap 0")
    top = state.get("top")
    if top and total:
        tag = "(TOP)" if "TOP" in a["flags"] else ""
        parts.append(f"top {top['name']} {human_kb(top['rss_kb'])}({a['top_pct']:.1f}%){tag}")
    worst = max(state.get("disks", []), key=lambda d: d["pct"], default=None)
    if worst:
        tag = "(HIGH)" if "DISK" in a["flags"] else ""
        parts.append(f"{worst['mount']} {worst['pct']:.0f}%{tag}")
    load1 = state.get("load", (0.0,))[0]
    tag = "(HIGH)" if "LOAD" in a["flags"] else ""
    parts.append(f"load {load1:.1f}/{state.get('cores', 1)}{tag}")
    psi = state.get("psi", {})
    if psi:
        tag = "(STRAIN:" + ",".join(sorted(a.get("strained", set()))) + ")" if "STRAIN" in a["flags"] else ""
        parts.append("psi " + "/".join(f"{psi.get(r, 0):.0f}" for r in ("cpu", "memory", "io")) + f"%{tag}")
    temps = state.get("temps", {})
    if temps:
        hot = a.get("hot", set())
        seg = " ".join(
            f"{cls} {deg:.0f}" + ("(HOT)" if cls in hot else "")
            for cls, deg in sorted(temps.items())
        )
        parts.append(f"temp {seg}°C")
    ev = a.get("events", {})
    if ev:
        labels = []
        if "oom_kill" in ev:
            labels.append(f"oom-kill {ev['oom_kill']}")
        if "edac_ce" in ev:
            labels.append(f"ecc-ce +{ev['edac_ce']}")
        if "edac_ue" in ev:
            labels.append(f"ecc-ue +{ev['edac_ue']}")
        if "md_degraded" in ev:
            labels.append("raid-degraded")
        parts.append("pain " + " ".join(labels))
    down = [n for n, s in state.get("services", []) if s not in ("active", "unknown")]
    if down:
        parts.append("svc-down: " + ",".join(down))
    return "[system-state] " + " · ".join(parts)


def line_for_mode(mode: str, proc_root: str = "/proc", mounts=None, services=None,
                  hwmon_root: str = "/sys/class/hwmon", sys_root: str = "/sys",
                  state_dir: str | None = None) -> str | None:
    """Top-level: gather, diff against the persisted baseline, assess, persist,
    return the line to print or None to stay silent."""
    if mode == "off":
        return None
    state = gather(proc_root, mounts, services, hwmon_root, sys_root)
    prev = load_state(state_dir)
    events = diff_events(state.get("counters", {}), prev.get("counters", {}))
    a = assess(state, events=events)
    save_state({"counters": state.get("counters", {})}, state_dir)
    if mode == "always" or a["flags"]:
        line = render(state, a)
        log_emission(line, a["flags"], state_dir)
        return line
    return None


def log_emission(line: str, flags: set, state_dir: str | None = None) -> None:
    """Append an emission record (falsifiability substrate). Never raises."""
    if os.environ.get("SOMA_LOG", "1") != "1":
        return
    try:
        from datetime import datetime, timezone
        d = _state_dir(state_dir)
        d.mkdir(parents=True, exist_ok=True)
        rec = {"ts": datetime.now(timezone.utc).astimezone().isoformat(),
               "flags": sorted(flags), "line": line}
        with open(d / "soma-log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        return
