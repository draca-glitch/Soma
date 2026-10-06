"""
Workspace senses for soma (0.12.0): who else works here, did HEAD move, what did I leave running.

Three readings of the agent's workplace, taken from the process table the
hook already walked once (soma_lib.proc_table) and from the git directory
of the session's cwd (read directly, no subprocess):

  - peers: other live Claude Code sessions on the host, and how many of them
    work in the same git toplevel (else the same cwd) as this session.
    A session root is a process whose comm is in SOMA_PEER_COMM (default
    claude) with no such process among its ancestors, not a zombie and not
    stopped. This session's own root is the topmost one above the hook, so its
    headless children are never peers, nor is a detached one reparented to init
    (its environment's CLAUDE_PID equals this session's root pid); a peer's headless
    children are not separate sessions either. An unreadable cwd counts as a session
    elsewhere. The payload cwd is resolved (symlinks) before comparing.
  - HEAD: the ref HEAD points at (branch name, or the first 7 characters of a
    detached hash), per session. A change against the session's record is said
    once; a commit on the same branch is not a change. In the pulse, a Bash
    command with git as a command word is the agent's own doing and is recorded
    silently. An unreadable or empty HEAD keeps the last good record. Only regular
    files are opened (O_NONBLOCK, so a FIFO cannot hang the hook).
  - leftovers (bg): processes this session started through the Bash tool that
    still run SOMA_BG_AGE_S (600) after they started. The discriminator is
    CLAUDE_PID equal to the session root's pid without CLAUDE_PROJECT_DIR (measured:
    Bash-tool shells, hook and statusline commands all carry CLAUDE_PID, MCP servers do
    not; only the hook and statusline children carry CLAUDE_PROJECT_DIR), with a start
    time after the root's (pid reuse). Looked at: the root's subtree and processes
    reparented to PID 1 or to a parent named systemd.
    It is a heuristic: it never forces a line and raises no flag.

Network mounts: no path under a network mount (soma_host's fstypes, from
/proc/mounts) or a mount recorded stale is ever touched, decided by string
comparison: HEAD and peers are silent for such a cwd, a peer there is elsewhere.
The cwd's toplevel is walked once per cwd (again after REWALK_S) and kept in the
record as `where`; every walk is bounded to MAX_LEVELS.

State: <state_dir>/soma-work/<sid>.json {ts, top, head, peers, where}. A said-once
item (HEAD moved, peers appearing) is claimed through an exclusive file keyed
on the record it changes, and returned only when the new record was written.
A subagent's call reads and writes nothing. Never raises. Pure stdlib.
Used by soma_lib.line_for_mode and soma_lib.pulse_line.
"""

import os
import re
import stat
import time

try:
    from soma_ctx import read_session_json, safe_id, state_dir, write_session_json
except Exception:  # without the store nothing is recorded, so nothing is said
    def safe_id(session_id):
        return None

    def read_session_json(subdir, sid, sdir=None):
        return None

    def write_session_json(subdir, sid, doc, sdir=None, now=None):
        return False

    def state_dir(override=None):
        return override or ""

WORK_SUBDIR = "soma-work"
OFF_VALUES = ("0", "off", "false", "no")
SHELLS = {"bash", "sh", "dash", "zsh", "fish"}
CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
MARKER = b"CLAUDE_PID="
HOOK_MARK = b"\0CLAUDE_PROJECT_DIR="  # hook and statusline children carry it, Bash-tool shells do not
MAX_LEVELS = 40
REWALK_S = 60.0
GIT_WORD = re.compile(r"(?:^|[;&|(\s])git(?:\s|$)")


def _on(name: str) -> bool:
    return os.environ.get(name, "1").strip().lower() not in OFF_VALUES


def _self_pid() -> int:
    return os.getpid()


def _names() -> set:
    raw = os.environ.get("SOMA_PEER_COMM", "claude")
    return {n.strip() for n in raw.split(",") if n.strip()} or {"claude"}


# ---- git ---------------------------------------------------------------------------------

def under(path, net) -> bool:
    """path is at or below one of the mount points in net: string comparison only, never a stat."""
    if not isinstance(path, str) or not net:
        return False
    for m in net:
        if isinstance(m, str) and m:
            m = m.rstrip("/") or "/"
            if path == m or path.startswith(m if m == "/" else m + "/"):
                return True
    return False


def _read_regular(path: str) -> str | None:
    """The first 4 KiB of path when it is a regular file. O_NONBLOCK so a FIFO put there
    cannot hang the open; anything but a regular file reads as None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return os.read(fd, 4096).decode("utf-8", "replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def _git_dir(top: str) -> str | None:
    dot = os.path.join(top, ".git")
    try:
        st = os.stat(dot)
    except OSError:
        return None
    if stat.S_ISDIR(st.st_mode):
        return dot
    line = _read_regular(dot) if stat.S_ISREG(st.st_mode) else None
    if not line or not line.strip().startswith("gitdir:"):
        return None
    path = line.strip()[len("gitdir:"):].strip()
    return os.path.normpath(os.path.join(top, path))


def resolve(path, net=()) -> str | None:
    """realpath without ever touching a path under a mount in net: component by component, each
    checked by string before its lstat, at most MAX_LEVELS symlinks. None when it would cross
    into such a mount or cannot be resolved."""
    if not isinstance(path, str) or not os.path.isabs(path) or under(path, net):
        return None
    parts, done, links, steps = [p for p in path.split("/") if p], "/", 0, 0
    while parts:
        steps += 1
        if steps > 4096:
            return None
        p = parts.pop(0)
        if p == ".":
            continue
        if p == "..":
            done = os.path.dirname(done)
            continue
        cand = os.path.join(done, p)
        if under(cand, net):
            return None
        try:
            st = os.lstat(cand)
            if stat.S_ISLNK(st.st_mode):
                links += 1
                if links > MAX_LEVELS:
                    return None
                target = os.readlink(cand)
                parts = [x for x in target.split("/") if x] + parts
                if target.startswith("/"):
                    done = "/"
                continue
        except OSError:
            return None
        done = cand
    return done


def toplevel(cwd: str, net=()) -> str | None:
    """The nearest directory at or above cwd holding a .git entry, or None; at most MAX_LEVELS
    levels, and None at the first level under a mount in net (never stat'ed)."""
    path = os.path.normpath(cwd) if isinstance(cwd, str) else None
    if not path or not os.path.isabs(path):
        return None
    for _ in range(MAX_LEVELS):
        if under(path, net):
            return None
        if os.path.lexists(os.path.join(path, ".git")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent
    return None


def head_ref(top: str) -> str | None:
    """The branch HEAD points at (refs/heads/ stripped), or the first 7 characters of a detached
    hash; None when HEAD is missing, empty, not a regular file or unparseable."""
    try:
        gd = _git_dir(top)
        head = _read_regular(os.path.join(gd, "HEAD")) if gd else None
        head = head.strip() if head else ""
        if head.startswith("ref:"):
            ref = head[4:].strip()
            ref = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
        elif len(head) >= 7 and all(c in "0123456789abcdef" for c in head):
            ref = head[:7]
        else:
            return None
        return ref or None
    except Exception:
        return None


def git_head(cwd, net=()) -> tuple | None:
    """(toplevel, ref) for cwd, None without a readable git directory or under a mount in net."""
    try:
        real = resolve(cwd, net)
        top = toplevel(real, net) if real else None
        ref = head_ref(top) if top else None
        return (top, ref) if ref else None
    except Exception:
        return None


# ---- process table readings ----------------------------------------------------------------

def _ancestors(table: dict, pid: int) -> list:
    out, seen = [], set()
    while pid in table and pid not in seen and pid > 1:
        seen.add(pid)
        out.append(pid)
        pid = table[pid].get("ppid")
    return out


def _cwd(proc_root: str, pid: int) -> str | None:
    try:
        return os.readlink(os.path.join(proc_root, str(pid), "cwd"))
    except OSError:
        return None


def peers(table: dict, proc_root: str, self_pid: int, cwd, net=(), mine=None) -> dict | None:
    """{here, total}: live session roots elsewhere in the same place as this session, and all
    live sessions including this one. None when the hook is not under a session root, or when
    this session's cwd is under a mount in net. A candidate whose environment carries CLAUDE_PID
    equal to this session's root is this session's own detached child, not a peer. A peer whose
    cwd is under a mount in net counts as elsewhere, untouched. mine: a callable returning this
    cwd's toplevel (cached by the caller), called only when there is a candidate."""
    names = _names()
    chain = _ancestors(table, self_pid)
    own = [p for p in chain if table[p].get("comm") in names]
    if not own:
        return None
    own_root = own[-1]
    if isinstance(cwd, str) and os.path.isabs(cwd):
        cwd = resolve(cwd, net)
    else:
        cwd = _cwd(proc_root, own_root)
        cwd = None if under(cwd, net) else cwd
    if not cwd:
        return None
    value = str(own_root).encode()
    cands = []
    for pid, e in table.items():
        if pid == own_root or e.get("comm") not in names or e.get("state") in ("Z", "T", "t", "X", "x"):
            continue
        if any(table[a].get("comm") in names for a in _ancestors(table, e.get("ppid"))):
            continue
        env = _environ(proc_root, pid)
        if env is not None and (b"\0" + env).find(b"\0" + MARKER + value + b"\0") >= 0:
            continue  # this session's own `claude -p`, reparented once its shell exited
        cands.append(pid)
    here, total = 0, 1 + len(cands)
    if not cands:
        return {"here": 0, "total": total}
    top = mine() if callable(mine) else toplevel(cwd, net)
    for pid in cands:
        pc = _cwd(proc_root, pid)
        if pc is None or under(pc, net):
            continue
        if (toplevel(pc, net) == top) if top else (os.path.normpath(pc) == os.path.normpath(cwd)):
            here += 1
    return {"here": here, "total": total}


def _uptime(proc_root: str) -> float | None:
    try:
        with open(os.path.join(proc_root, "uptime")) as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _environ(proc_root: str, pid: int) -> bytes | None:
    try:
        with open(os.path.join(proc_root, str(pid), "environ"), "rb") as f:
            return f.read(262144)
    except OSError:
        return None


def _marked(proc_root: str, pid: int, value: bytes) -> bool:
    """CLAUDE_PID=<root> without CLAUDE_PROJECT_DIR: a Bash-tool process, not a hook or
    statusline child (those carry both)."""
    env = _environ(proc_root, pid)
    if env is None:
        return False
    env = b"\0" + env
    return env.find(b"\0" + MARKER + value + b"\0") >= 0 and env.find(HOOK_MARK) < 0


def leftovers(table: dict, proc_root: str, self_pid: int, min_age: float) -> dict | None:
    """{count, oldest_s, name} of this session's Bash-started processes older than min_age,
    a process and its marked descendants counted once. None when there are none."""
    names = _names()
    anchor = next((p for p in _ancestors(table, self_pid) if table[p].get("comm") in names), None)
    if anchor is None or not isinstance(table[anchor].get("start"), int):
        return None
    up = _uptime(proc_root)
    if up is None:
        return None
    root_start, skip = table[anchor]["start"], set(_ancestors(table, self_pid))
    newest = (up - min_age) * CLK_TCK
    tree, queue, kids = set(), [anchor], {}
    for pid, e in table.items():
        kids.setdefault(e.get("ppid"), []).append(pid)
    while queue:
        p = queue.pop()
        if p not in tree:
            tree.add(p)
            queue.extend(kids.get(p, []))
    value = str(anchor).encode()
    cand = set()
    for pid, e in table.items():
        start = e.get("start")
        if pid in skip or not isinstance(start, int) or not root_start <= start <= newest:
            continue
        if pid in tree or e.get("ppid") == 1 or table.get(e.get("ppid"), {}).get("comm") == "systemd":
            if _marked(proc_root, pid, value):
                cand.add(pid)
    # descendants of a marked orphan are reached through it; count only the tops
    for pid in list(cand):
        for k in kids.get(pid, []):
            st = table[k].get("start")
            if k not in skip and isinstance(st, int) and st <= newest and _marked(proc_root, k, value):
                cand.add(k)
    tops = [p for p in cand if table[p].get("ppid") not in cand]
    if not tops:
        return None
    oldest = min(tops, key=lambda p: table[p]["start"])
    name, pid = table[oldest].get("comm", "?"), oldest
    # a shell wrapper is named by its one child, when that child is itself a leftover
    # (a dev server under `bash -c`); a polling loop's passing sleep is not
    while name in SHELLS and len(kids.get(pid, [])) == 1:
        k = kids[pid][0]
        st = table[k].get("start")
        if not isinstance(st, int) or st > newest:
            break
        pid, name = k, table[k].get("comm") or name
    return {"count": len(tops), "oldest_s": int(up - table[oldest]["start"] / CLK_TCK), "name": name}


def bg_segment(bg: dict) -> str:
    age = bg["oldest_s"]
    shown = f"{age // 60}m" if age < 7200 else f"{age // 3600}h"
    return f"bg {bg['count']} (oldest {shown} {bg['name']})"


# ---- per-session state and the said-once claim ----------------------------------------------

def _read(sdir, sid) -> dict:
    doc = read_session_json(WORK_SUBDIR, sid, sdir)
    return doc if isinstance(doc, dict) else {}


def _write(sdir, sid, doc) -> bool:
    return write_session_json(WORK_SUBDIR, sid, doc, sdir)


def _claim(sdir, sid, prev_ts) -> str | None:
    key = int(prev_ts * 1000) if isinstance(prev_ts, (int, float)) and not isinstance(prev_ts, bool) \
        and prev_ts == prev_ts and abs(prev_ts) < 1e12 else 0
    path = os.path.join(state_dir(sdir), WORK_SUBDIR, f"{sid}.{key}.claim")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        return path
    except OSError:
        return None


def _str(v):
    return v if isinstance(v, str) and v else None


def _where(prev: dict, cwd, net, now) -> dict:
    """{cwd, real, top, ts}: the payload cwd resolved and its toplevel, from the session record
    when it is for the same cwd and younger than REWALK_S (a new `git init` is found within that),
    else walked now. A cwd under a mount in net is never walked: real and top are None."""
    w = prev.get("where")
    if isinstance(w, dict) and w.get("cwd") == cwd and isinstance(w.get("ts"), (int, float)) \
            and not isinstance(w.get("ts"), bool) and 0 <= now - w["ts"] < REWALK_S \
            and not under(w.get("real"), net):
        return {"cwd": cwd, "real": _str(w.get("real")), "top": _str(w.get("top")), "ts": w["ts"]}
    real = resolve(cwd, net) if cwd else None
    return {"cwd": cwd, "real": real, "top": toplevel(real, net) if real else None, "ts": now}


def work_reading(hook_input, table, proc_root: str = "/proc", sdir=None, now=None,
                 pulse: bool = False, self_pid: int | None = None, net=None) -> dict:
    """{segs, force, flags} for this session: segments to append in order (HEAD moved, peers,
    bg), whether one of them must force the line (a said-once item), and its log flags.
    net: the network and stale mount points this run already knows; no path under them is
    touched (None: read the network mounts from proc_root/mounts, no stat)."""
    out = {"segs": [], "force": False, "flags": set()}
    try:
        if not isinstance(hook_input, dict) or hook_input.get("agent_id"):
            return out
        sid = safe_id(hook_input.get("session_id"))
        if not sid:
            return out
        now = time.time() if now is None else now
        self_pid = _self_pid() if self_pid is None else self_pid
        if net is None:
            try:
                from soma_host import net_mount_table
                net = [mp for _, mp in net_mount_table(proc_root)]
            except Exception:
                net = []
        net = [m for m in net if isinstance(m, str) and m]
        cwd = hook_input.get("cwd")
        cwd = os.path.normpath(cwd) if isinstance(cwd, str) and os.path.isabs(cwd) else None
        table = table if isinstance(table, dict) else {}
        prev = _read(sdir, sid)
        new = {"ts": now, "top": _str(prev.get("top")), "head": _str(prev.get("head"))}
        p_told = prev.get("peers") if isinstance(prev.get("peers"), int) and not isinstance(prev.get("peers"), bool) \
            and prev["peers"] >= 0 else 0
        new["peers"] = p_told
        old_where = prev.get("where") if isinstance(prev.get("where"), dict) else None
        where = None

        def mine():
            nonlocal where
            if where is None:
                where = _where(prev, cwd, net, now) if cwd else {"cwd": None, "real": None, "top": None, "ts": now}
            return where["top"]
        head_seg = peer_seg = None
        peer_new = False
        if _on("SOMA_HEAD") and cwd and not under(cwd, net):
            top = mine()
            ref = head_ref(top) if top else None
            if top and ref is None:
                pass  # HEAD unreadable or half-written: keep the last good record, say nothing
            else:
                if top and top == new["top"] and new["head"] and ref != new["head"]:
                    ti = hook_input.get("tool_input")
                    cmd = ti.get("command") if isinstance(ti, dict) else None
                    own = pulse and hook_input.get("tool_name") == "Bash" and isinstance(cmd, str) \
                        and GIT_WORD.search(cmd) is not None
                    if not own:
                        head_seg = f"HEAD {new['head']}→{ref} since last {'tool call' if pulse else 'prompt'}"
                new["top"], new["head"] = top, ref
        if _on("SOMA_PEERS") and table and not (cwd and under(cwd, net)):
            pr = peers(table, proc_root, self_pid, (where or {}).get("real") or cwd, net,
                       mine if cwd else None)
            if pr is not None:
                new["peers"] = pr["here"]
                if pr["here"] > 0:
                    peer_seg = f"peers {pr['here']} here ({pr['total']} sessions)"
                    peer_new = p_told == 0
        new["where"] = where if where is not None else old_where
        claim = _claim(sdir, sid, prev.get("ts")) if head_seg or peer_new else None
        if (head_seg or peer_new) and not claim:
            head_seg, peer_new = None, False  # another caller is saying it; this one stays quiet
            peer_seg = peer_seg if p_told else None
            return {"segs": [s for s in (peer_seg,) if s], "force": False, "flags": set()}
        same = all(prev.get(k) == new[k] for k in ("top", "head", "peers", "where")) and \
            isinstance(prev.get("ts"), (int, float)) and not isinstance(prev.get("ts"), bool)
        # an unchanged record is not rewritten (one write saved per tool call); a change always is,
        # so the claim key (the record's ts) moves on with every said-once item
        if not same and not _write(sdir, sid, new):
            if claim:
                try:
                    os.unlink(claim)
                except OSError:
                    pass
            head_seg, peer_new = None, False
            if not p_told:
                peer_seg = None
        bg_seg = None
        if _on("SOMA_BG") and table:
            try:
                age = max(0.0, float(os.environ.get("SOMA_BG_AGE_S", "600")))
            except ValueError:
                age = 600.0
            bg = leftovers(table, proc_root, self_pid, age)
            bg_seg = bg_segment(bg) if bg else None
        out["segs"] = [s for s in (head_seg, peer_seg, bg_seg) if s]
        out["force"] = bool(head_seg or peer_new)
        out["flags"] = ({"HEAD"} if head_seg else set()) | ({"PEER"} if peer_new else set())
        return out
    except Exception:
        return {"segs": [], "force": False, "flags": set()}
