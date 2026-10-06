"""
Host preconditions for soma (0.12.0): things that make the agent's NEXT action fail or hang.

Three readings, each cheap and each sensed before the agent acts:

  - stale network mounts: every cifs/smb3/nfs/nfs4/fuse.sshfs/9p mount in
    /proc/mounts (SOMA_NET_MOUNTS = a comma list to override, 0 = off) is
    probed with statvfs, once per mount source (device field) at most every
    SOMA_NET_PROBE_S (30) s, the attempt stamped in the host-wide state before
    the probe, in a worker thread under a shared deadline (SOMA_NET_TIMEOUT_MS,
    300). No answer inside it = stale (a dead transport under the mount would
    hang the tool call that touches it). An error that returns (ENOENT,
    EACCES) is an answer. Abandoned probe threads are daemons and die with the
    process. While a mount is stale it is re-probed at most every
    STALE_BACKOFF_S (60 s), remembered in the host-wide state, so a dead mount
    does not add its timeout to every tool call. Body flag STALE.
    Not stat: a CIFS client answers stat of the root from its cache (0.01 ms,
    nothing on the wire), so a dead transport never showed; statvfs goes to the
    server (about 9 ms here). Between probes the last result stands.
  - package manager busy: a process whose comm is apt, apt-get, aptitude,
    dpkg, unattended-upgr (the upgrade worker, not the always-on
    unattended-upgrade-shutdown daemon whose comm is cut to the same 15
    characters; told apart by its cmdline, read only for that comm),
    autoinstaller (Plesk's updater, also what `plesk installer` runs), or packagekitd while it has a dpkg
    child, read from the hook's one /proc walk. No lock is
    probed: taking the dpkg lock even for an instant can make a real apt fail.
    SOMA_PKG=0 off. Body flag PKG.
  - reboot pending: /run/reboot-required exists (Debian/Ubuntu). A segment
    only, never a flag; it rides on a line that is emitted anyway. SOMA_REBOOT=0 off.

Pure stdlib (os/threading/time), never raises.
"""

import os
import threading
import time

NET_FSTYPES = {"cifs", "smb3", "nfs", "nfs4", "fuse.sshfs", "9p"}
PKG_COMMS = {"apt", "apt-get", "aptitude", "dpkg", "unattended-upgr", "autoinstaller"}
STALE_BACKOFF_S = 60.0
REBOOT_FILE = "/run/reboot-required"
# statvfs, not stat: a CIFS client answers stat of the mount root from its cache (measured 0.01 ms,
# no request on the wire even 6 s apart), so a dead transport never shows; statvfs goes to the server
DEFAULT_PROBE = os.statvfs
PROBE = DEFAULT_PROBE  # module-level so tests can swap in a probe that never returns
OFF = ("0", "off", "false", "no")


def _off(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in OFF


def _unescape(field: str) -> str:
    """/proc/mounts octal escapes (\\040 space, \\011 tab, \\012 newline, \\134 backslash)."""
    if "\\" not in field:
        return field
    out, i = [], 0
    while i < len(field):
        c = field[i]
        if c == "\\" and i + 3 < len(field) and field[i + 1:i + 4].isdigit():
            out.append(chr(int(field[i + 1:i + 4], 8)))
            i += 4
        else:
            out.append(c)
            i += 1
    return "".join(out)


def net_mount_table(proc_root: str = "/proc") -> list:
    """[(source, mount point)] of the network mounts in /proc/mounts, in order, deduped by mount
    point. Ignores SOMA_NET_MOUNTS: the workspace senses use it to stay off these paths."""
    try:
        with open(os.path.join(proc_root, "mounts")) as f:
            text = f.read()
    except OSError:
        return []
    out, seen = [], set()
    for ln in text.splitlines():
        parts = ln.split()
        if len(parts) >= 3 and parts[2] in NET_FSTYPES:
            mp = _unescape(parts[1])
            if mp not in seen:
                seen.add(mp)
                out.append((_unescape(parts[0]), mp))
    return out


def net_mounts(proc_root: str = "/proc") -> list:
    """Network mount points, in /proc/mounts order, deduped. [] when off or unreadable."""
    raw = os.environ.get("SOMA_NET_MOUNTS")
    if raw is not None:
        if raw.strip().lower() in OFF:
            return []
        return [s.strip() for s in raw.split(",") if s.strip()]
    try:
        with open(os.path.join(proc_root, "mounts")) as f:
            text = f.read()
    except OSError:
        return []
    out = []
    for ln in text.splitlines():
        parts = ln.split()
        if len(parts) >= 3 and parts[2] in NET_FSTYPES:
            mp = _unescape(parts[1])
            if mp not in out:
                out.append(mp)
    return out


def bounded_calls(paths: list, fn, timeout_s: float) -> tuple:
    """soma_lib.disk_usage's watchdog pattern, generalized: fn(path) for each path in
    parallel daemon threads under one shared deadline. Returns (results {path: value or None on OSError}, hung [paths still running at the deadline])."""
    results = {}

    def run(p):
        try:
            results[p] = fn(p)
        except Exception:  # an error that returns is an answer; never a traceback on stderr
            results[p] = None

    threads = {}
    for p in paths:
        t = threading.Thread(target=run, args=(p,), daemon=True)
        t.start()
        threads[p] = t
    deadline = time.monotonic() + timeout_s
    hung = []
    for p in paths:
        threads[p].join(max(0.0, deadline - time.monotonic()))
        if threads[p].is_alive():
            hung.append(p)
    return results, hung


def _timeout_s() -> float:
    try:
        return max(0.01, float(os.environ.get("SOMA_NET_TIMEOUT_MS", "300")) / 1000.0)
    except ValueError:
        return 0.3


def _valid_ts(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")


def probe_interval_s() -> float:
    try:
        return max(0.0, float(os.environ.get("SOMA_NET_PROBE_S", "30")))
    except ValueError:
        return 30.0


def stale_mounts(mounts: list, record, now: float, probe=None, timeout_s: float | None = None,
                 stamps: dict | None = None, interval_s: float = 0.0, stamp=None) -> tuple:
    """(stale [mount points], new record {source: time of the last probe that hung}).

    mounts: mount points, or (source, mount point) pairs; one probe per source (its first mount
    point), the answer applied to all its mount points. A source in the record probed less than
    STALE_BACKOFF_S ago is not probed again and stays stale. With stamps (a dict {source: time of
    the last probe attempt}, updated in place), a source attempted less than interval_s ago is not
    probed and its last result stands; due sources are stamped, and stamp(stamps) is called,
    BEFORE the probe, so parallel hooks do not all probe in the same second. A record time in the
    future (clock went backwards) or junk means probe now. Sources no longer listed drop out."""
    probe = PROBE if probe is None else probe
    timeout_s = _timeout_s() if timeout_s is None else timeout_s
    record = record if isinstance(record, dict) else {}
    pairs = [m if isinstance(m, tuple) else (m, m) for m in mounts]
    first = {}
    for src, mp in pairs:
        first.setdefault(src, mp)
    new, to_probe = {}, []
    for src in first:
        t = record.get(src)
        a = stamps.get(src) if isinstance(stamps, dict) else None
        if _valid_ts(t) and 0 <= now - t < STALE_BACKOFF_S:
            new[src] = t
        elif _valid_ts(a) and 0 <= now - a < interval_s:
            if _valid_ts(t) and 0 <= now - t:
                new[src] = t  # a probe is due elsewhere; the last result stands
        else:
            to_probe.append(src)
    if isinstance(stamps, dict):
        for k in [k for k in stamps if k not in first]:
            del stamps[k]
        if to_probe:
            stamps.update({src: now for src in to_probe})
            if stamp is not None:
                try:
                    stamp(stamps)
                except Exception:
                    pass
    if to_probe:
        _, hung = bounded_calls([first[s] for s in to_probe], probe, timeout_s)
        for src in to_probe:
            if first[src] in hung:
                new[src] = now
    stale = [mp for src, mp in pairs if src in new]
    return stale, new


def _upgrade_worker(proc_root: str, pid) -> bool:
    """comm unattended-upgr is the kernel's 15-character cut of both unattended-upgrade (the
    worker that runs apt) and unattended-upgrade-shutdown (an always-on daemon that holds
    nothing). Only the worker counts; an unreadable cmdline is not counted (print less)."""
    try:
        with open(os.path.join(proc_root, str(pid), "cmdline"), "rb") as f:
            argv = f.read(4096).split(b"\0")
    except OSError:
        return False
    return not any(a.rstrip(b"/").endswith(b"unattended-upgrade-shutdown") for a in argv[:3])


def pkg_busy(table, proc_root: str = "/proc") -> str | None:
    """Comma-joined sorted comms of the processes holding the package manager, None when idle or off."""
    if _off("SOMA_PKG") or not isinstance(table, dict):
        return None
    found = set()
    dpkg_parents = set()
    for pid, e in table.items():
        if e.get("state") in ("Z", "X"):
            continue
        comm = e.get("comm")
        if comm in PKG_COMMS and (comm != "unattended-upgr" or _upgrade_worker(proc_root, pid)):
            found.add(comm)
        if comm == "dpkg":
            dpkg_parents.add(e.get("ppid"))
    for pid in dpkg_parents:
        e = table.get(pid)
        if e and e.get("comm") == "packagekitd" and e.get("state") not in ("Z", "X"):
            found.add("packagekitd")
    return ",".join(sorted(found)) or None


def reboot_pending() -> bool:
    if _off("SOMA_REBOOT"):
        return False
    try:
        return os.path.exists(REBOOT_FILE)
    except Exception:
        return False


def host_reading(proc_root: str, table, record, now: float, stamps=None, stamp=None) -> dict:
    """{stale, record, stamps, net, pkg, reboot}; never raises (a failing piece reads as healthy).
    net: every network mount point (discovered, plus any SOMA_NET_MOUNTS entries), for the
    workspace senses to stay off."""
    out = {"stale": [], "record": {}, "stamps": {}, "net": [], "pkg": None, "reboot": False}
    try:
        table_m = net_mount_table(proc_root)
        mounts = net_mounts(proc_root)
        out["net"] = list(dict.fromkeys([mp for _, mp in table_m] + mounts))
        if mounts:
            if os.environ.get("SOMA_NET_MOUNTS") is None:
                entries = table_m
            else:
                src = {mp: s for s, mp in table_m}
                entries = [(src.get(m, m), m) for m in mounts]
            st = dict(stamps) if isinstance(stamps, dict) else {}
            st = {k: v for k, v in st.items() if _valid_ts(v)}
            out["stale"], out["record"] = stale_mounts(entries, record, now, stamps=st,
                                                       interval_s=probe_interval_s(), stamp=stamp)
            out["stamps"] = st
    except Exception:
        pass
    try:
        out["pkg"] = pkg_busy(table, proc_root)
    except Exception:
        pass
    out["reboot"] = reboot_pending()
    return out
