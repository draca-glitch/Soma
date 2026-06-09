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
import threading
import time
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
        "mem_tte_h": _env_float("SOMA_MEM_TTE_H", 2.0),            # flag drain when RAM empties within this many hours
        "disk_ttf_h": _env_float("SOMA_DISK_TTF_H", 24.0),         # flag fill when a mount fills within this many hours
        "top_growth_gbh": _env_float("SOMA_TOP_GROWTH_GBH", 0.5),  # flag top-process growth above this GB/h; 0 disables
        "self_rss_pct": _env_float("SOMA_SELF_RSS_PCT", 40.0),     # own-process-tree RAM share ceiling; 0 disables
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


def disk_usage(paths: list, timeout_s: float | None = None) -> dict:
    """statvfs each path under a shared deadline. {mounts: [{mount, pct, free_kb}], numb: [path]}.

    A network mount whose transport died (VPN drop under CIFS/NFS) blocks
    statvfs indefinitely, which would block the hook and with it the prompt.
    Probes run in parallel watchdog threads; a probe that misses the
    deadline reports the mount as numb, the body's strongest mount signal
    (an unfeeling limb), instead of hanging. Abandoned probe threads are
    daemons and die with the process. Identical filesystems are deduped.
    """
    if timeout_s is None:
        timeout_s = _env_float("SOMA_MOUNT_TIMEOUT_MS", 150.0) / 1000.0
    results = {}

    def probe(path):
        try:
            results[path] = os.statvfs(path)
        except OSError:
            results[path] = None

    threads = {}
    for p in paths:
        t = threading.Thread(target=probe, args=(p,), daemon=True)
        t.start()
        threads[p] = t
    deadline = time.monotonic() + timeout_s
    mounts, numb, seen = [], [], set()
    for p in paths:
        threads[p].join(max(0.0, deadline - time.monotonic()))
        if threads[p].is_alive():
            numb.append(p)
            continue
        st = results.get(p)
        if st is None:
            continue
        sig = (st.f_blocks, st.f_bavail)
        if st.f_blocks == 0 or sig in seen:
            continue
        seen.add(sig)
        used = st.f_blocks - st.f_bfree
        pct = used / (used + st.f_bavail) * 100 if (used + st.f_bavail) else 0.0
        mounts.append({"mount": p, "pct": pct, "free_kb": st.f_bavail * st.f_frsize // 1024})
    return {"mounts": mounts, "numb": numb}


def self_tree_rss(proc_root: str = "/proc", self_pid: int | None = None) -> dict | None:
    """RSS of the agent's own process tree. {name, rss_kb, procs} or None.

    The body boundary: 'I am heavy' is a different fact from 'the world is
    heavy'. Self is the nearest ancestor of this hook whose comm matches
    SOMA_SELF_COMM (default claude,node: the harness that spawned the hook),
    plus every descendant of that ancestor, which covers the MCP servers it
    spawned (the agent's organs) and any tool subprocesses currently running
    (the agent's own effort). Falls back to the hook's immediate parent when
    no ancestor matches; None when the chain cannot be read at all.
    """
    self_names = set(_env_list("SOMA_SELF_COMM", ["claude", "node"]))
    self_pid = self_pid if self_pid is not None else os.getpid()
    root = Path(proc_root)
    ppid_of, comm_of, rss_of, kids_of = {}, {}, {}, {}
    try:
        entries = list(root.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            stat = (entry / "stat").read_text()
            resident_pages = int((entry / "statm").read_text().split()[1])
        except (OSError, IndexError, ValueError):
            continue
        try:
            comm = stat[stat.index("(") + 1:stat.rindex(")")]
            ppid = int(stat[stat.rindex(")") + 1:].split()[1])
        except (ValueError, IndexError):
            continue
        ppid_of[pid] = ppid
        comm_of[pid] = comm
        rss_of[pid] = int(resident_pages * PAGE_KB)
        kids_of.setdefault(ppid, []).append(pid)
    if self_pid not in ppid_of:
        return None
    anchor, walked = None, set()
    pid = self_pid
    while pid in ppid_of and pid not in walked and pid > 1:
        walked.add(pid)
        if comm_of.get(pid) in self_names:
            anchor = pid
            break
        pid = ppid_of[pid]
    if anchor is None:
        anchor = ppid_of.get(self_pid, self_pid)
        if anchor not in ppid_of:
            anchor = self_pid
    total, count, queue, visited = 0, 0, [anchor], set()
    while queue:
        pid = queue.pop()
        if pid in visited:
            continue
        visited.add(pid)
        total += rss_of.get(pid, 0)
        count += 1
        queue.extend(kids_of.get(pid, []))
    return {"name": comm_of.get(anchor, f"pid{anchor}"), "rss_kb": total, "procs": count}


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


def snapshot_anchor(state: dict, now: float) -> dict:
    """Reference point for rates: timestamp plus the level readings that trend."""
    mem = state.get("mem", {})
    top = state.get("top")
    return {
        "ts": now,
        "mem_avail_kb": mem.get("MemAvailable"),
        "disks": {d["mount"]: d["free_kb"] for d in state.get("disks", [])},
        "top": {"name": top["name"], "rss_kb": top["rss_kb"]} if top else None,
    }


def roll_state(prev: dict, state: dict, now: float, anchor_s: int | None = None) -> dict:
    """Next persisted state doc. Counters refresh every reading; the trend
    anchor only when it has aged past anchor_s, so rates are measured over a
    stable window even when readings come seconds apart."""
    anchor_s = anchor_s if anchor_s is not None else _env_int("SOMA_TREND_ANCHOR_S", 600)
    anchor = prev.get("anchor")
    if (not isinstance(anchor, dict) or (now - anchor.get("ts", 0)) >= anchor_s
            or anchor.get("ts", 0) > now):
        anchor = snapshot_anchor(state, now)
    return {"counters": state.get("counters", {}), "anchor": anchor}


def compute_trends(state: dict, anchor: dict | None, now: float) -> dict:
    """Movement sense: rates of change against the anchor, in GB/h.

    mem_gb_h negative = draining (mem_tte_h = hours to empty at that rate);
    per-mount gb_h positive = filling (ttf_h = hours to full); top_gb_h only
    when the same process is still the top consumer. {} when the anchor is
    missing or under a minute old (rates would be noise).
    """
    if not isinstance(anchor, dict):
        return {}
    dt_h = (now - anchor.get("ts", now)) / 3600.0
    if dt_h < 1 / 60:
        return {}
    out = {}
    avail = state.get("mem", {}).get("MemAvailable")
    a_avail = anchor.get("mem_avail_kb")
    if avail is not None and a_avail is not None:
        rate = (avail - a_avail) / 1048576 / dt_h
        out["mem_gb_h"] = rate
        if rate < 0:
            out["mem_tte_h"] = (avail / 1048576) / -rate
    disks = {}
    for d in state.get("disks", []):
        a_free = (anchor.get("disks") or {}).get(d["mount"])
        if a_free is None:
            continue
        fill = (a_free - d["free_kb"]) / 1048576 / dt_h
        entry = {"gb_h": fill}
        if fill > 0:
            entry["ttf_h"] = (d["free_kb"] / 1048576) / fill
        disks[d["mount"]] = entry
    if disks:
        out["disks"] = disks
    top, a_top = state.get("top"), anchor.get("top")
    if (top and isinstance(a_top, dict) and top.get("name") == a_top.get("name")
            and a_top.get("rss_kb") is not None):
        out["top_gb_h"] = (top["rss_kb"] - a_top["rss_kb"]) / 1048576 / dt_h
    return out


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
    du = disk_usage(mounts)
    state["disks"] = du["mounts"]
    state["numb"] = du["numb"]
    state["self"] = self_tree_rss(proc_root)
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


def assess(state: dict, th: dict | None = None, events: dict | None = None,
           trends: dict | None = None) -> dict:
    """Flag which conditions cross a threshold. {flags: set, mem_avail_pct, swap_mb, load_ratio, top_pct, hot, strained, events, trends}."""
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
    own = state.get("self")
    self_pct = (own["rss_kb"] / total * 100) if (own and total) else 0.0

    if total and mem_avail_pct < th["mem_avail_pct"]:
        flags.add("LOW_MEM")
    if th["self_rss_pct"] and self_pct > th["self_rss_pct"]:
        flags.add("SELF")
    if state.get("numb"):
        flags.add("NUMB")
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
    trends = trends or {}
    tte = trends.get("mem_tte_h")
    # DRAIN needs both an imminent time-to-empty and less than half the RAM
    # left; a big one-off allocation on a mostly-free box is loading, not leaking.
    if tte is not None and tte < th["mem_tte_h"] and mem_avail_pct < 50.0:
        flags.add("DRAIN")
    for entry in trends.get("disks", {}).values():
        ttf = entry.get("ttf_h")
        if ttf is not None and ttf < th["disk_ttf_h"]:
            flags.add("FILL")
    if th["top_growth_gbh"] and trends.get("top_gb_h", 0.0) >= th["top_growth_gbh"]:
        flags.add("GROW")
    return {
        "flags": flags,
        "mem_avail_pct": mem_avail_pct,
        "swap_mb": swap_mb,
        "load_ratio": load_ratio,
        "top_pct": top_pct,
        "self_pct": self_pct,
        "hot": hot,
        "strained": strained,
        "events": events,
        "trends": trends,
    }


def render(state: dict, a: dict) -> str:
    """One compact [system-state] line. Crossing fields carry an inline flag."""
    mem = state.get("mem", {})
    total = mem.get("MemTotal", 0)
    avail = mem.get("MemAvailable", 0)
    swap_used = mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)
    parts = []
    trends = a.get("trends", {})
    if total:
        tag = "(LOW)" if "LOW_MEM" in a["flags"] else ""
        if "DRAIN" in a["flags"]:
            tag += f" ({trends['mem_gb_h']:+.1f}G/h, empty ~{trends['mem_tte_h']:.1f}h)(DRAIN)"
        parts.append(f"mem {human_kb(avail)}/{human_kb(total)} avail{tag}")
    if swap_used > 0:
        parts.append(f"swap {human_kb(swap_used)}")
    else:
        parts.append("swap 0")
    top = state.get("top")
    if top and total:
        tag = "(TOP)" if "TOP" in a["flags"] else ""
        if "GROW" in a["flags"]:
            tag += f" (+{trends['top_gb_h']:.1f}G/h)(GROW)"
        parts.append(f"top {top['name']} {human_kb(top['rss_kb'])}({a['top_pct']:.1f}%){tag}")
    own = state.get("self")
    if own and total:
        tag = "(SELF)" if "SELF" in a["flags"] else ""
        parts.append(f"self {own['name']}[{own['procs']}] {human_kb(own['rss_kb'])}({a['self_pct']:.1f}%){tag}")
    worst = max(state.get("disks", []), key=lambda d: d["pct"], default=None)
    if worst:
        tag = "(HIGH)" if "DISK" in a["flags"] else ""
        parts.append(f"{worst['mount']} {worst['pct']:.0f}%{tag}")
    if "FILL" in a["flags"]:
        mount, entry = min(
            ((m, e) for m, e in trends.get("disks", {}).items() if "ttf_h" in e),
            key=lambda kv: kv[1]["ttf_h"])
        parts.append(f"fill {mount} +{entry['gb_h']:.1f}G/h (full ~{entry['ttf_h']:.0f}h)(FILL)")
    if state.get("numb"):
        parts.append("numb: " + ",".join(state["numb"]))
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
                  state_dir: str | None = None, now: float | None = None) -> str | None:
    """Top-level: gather, diff against the persisted baseline, assess, persist,
    return the line to print or None to stay silent."""
    if mode == "off":
        return None
    now = now if now is not None else time.time()
    state = gather(proc_root, mounts, services, hwmon_root, sys_root)
    prev = load_state(state_dir)
    events = diff_events(state.get("counters", {}), prev.get("counters", {}))
    trends = compute_trends(state, prev.get("anchor"), now)
    a = assess(state, events=events, trends=trends)
    doc = roll_state(prev, state, now)
    doc["last_flags"] = sorted(a["flags"])
    save_state(doc, state_dir)
    if mode == "always" or a["flags"]:
        line = render(state, a)
        log_emission(line, a["flags"], state_dir)
        return line
    return None


# Acute pain flags: their clearing is the delta baseline advancing, not a
# recovery. Everything else clearing is a condition genuinely passing.
ACUTE_FLAGS = {"OOM", "ECC"}


def should_pulse(prev_flags: set, cur_flags: set) -> bool:
    """Mid-turn emission gate: a flag appeared, or a chronic condition cleared."""
    appeared = cur_flags - prev_flags
    recovered = (prev_flags - cur_flags) - ACUTE_FLAGS
    return bool(appeared or recovered)


def pulse_line(proc_root: str = "/proc", mounts=None, services=None,
               hwmon_root: str = "/sys/class/hwmon", sys_root: str = "/sys",
               state_dir: str | None = None, now: float | None = None) -> str | None:
    """Mid-turn proprioception for PostToolUse: emit only on flag transitions.

    The prompt-time hook re-orients at every prompt; at tool cadence that
    would be spam. This emits only when the body's condition changes while
    the agent is acting: something crossed a threshold, or a chronic
    condition passed. A long healthy turn costs zero lines.
    """
    if os.environ.get("SOMA_PULSE", "transition") == "off":
        return None
    now = now if now is not None else time.time()
    state = gather(proc_root, mounts, services, hwmon_root, sys_root)
    prev = load_state(state_dir)
    events = diff_events(state.get("counters", {}), prev.get("counters", {}))
    trends = compute_trends(state, prev.get("anchor"), now)
    a = assess(state, events=events, trends=trends)
    doc = roll_state(prev, state, now)
    doc["last_flags"] = sorted(a["flags"])
    save_state(doc, state_dir)
    if should_pulse(set(prev.get("last_flags", [])), a["flags"]):
        line = render(state, a)
        log_emission(line, a["flags"], state_dir, src="pulse")
        return line
    return None


def log_emission(line: str, flags: set, state_dir: str | None = None, src: str = "state") -> None:
    """Append an emission record (falsifiability substrate). Never raises."""
    if os.environ.get("SOMA_LOG", "1") != "1":
        return
    try:
        from datetime import datetime, timezone
        d = _state_dir(state_dir)
        d.mkdir(parents=True, exist_ok=True)
        rec = {"ts": datetime.now(timezone.utc).astimezone().isoformat(),
               "flags": sorted(flags), "line": line, "src": src}
        with open(d / "soma-log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        return
