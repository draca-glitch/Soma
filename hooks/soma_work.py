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
    headless children are never peers; a peer's headless children are not
    separate sessions either. An unreadable cwd counts as a session elsewhere.
  - HEAD: the ref HEAD points at (branch name, or the first 7 characters of a
    detached hash), per session. A change against the session's record is said
    once; a commit on the same branch is not a change. In the pulse, a Bash
    command mentioning git is the agent's own doing and is recorded silently.
  - leftovers (bg): processes this session started through the Bash tool that
    still run SOMA_BG_AGE_S (600) after they started. The discriminator is the
    CLAUDE_PID variable the harness puts in the Bash tool's environment (and
    only there: MCP servers, hooks and the statusline do not carry it) equal to
    the session root's pid, with a start time after the root's (pid reuse).
    Only processes under the session root or reparented to init are looked at.
    It is a heuristic: it never forces a line and raises no flag.

State: <state_dir>/soma-work/<sid>.json {ts, top, head, peers}. A said-once
item (HEAD moved, peers appearing) is claimed through an exclusive file keyed
on the record it changes, and returned only when the new record was written.
A subagent's call reads and writes nothing. Never raises. Pure stdlib.
Used by soma_lib.line_for_mode and soma_lib.pulse_line.
"""

import os
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


def _on(name: str) -> bool:
    return os.environ.get(name, "1").strip().lower() not in OFF_VALUES


def _self_pid() -> int:
    return os.getpid()


def _names() -> set:
    raw = os.environ.get("SOMA_PEER_COMM", "claude")
    return {n.strip() for n in raw.split(",") if n.strip()} or {"claude"}


# ---- git ---------------------------------------------------------------------------------

def _git_dir(top: str) -> str | None:
    dot = os.path.join(top, ".git")
    if os.path.isdir(dot):
        return dot
    try:
        with open(dot) as f:
            line = f.read(4096).strip()
    except OSError:
        return None
    if not line.startswith("gitdir:"):
        return None
    path = line[len("gitdir:"):].strip()
    return os.path.normpath(os.path.join(top, path))


def toplevel(cwd: str) -> str | None:
    """The nearest directory at or above cwd holding a .git entry, or None."""
    path = os.path.abspath(cwd)
    for _ in range(64):
        if os.path.lexists(os.path.join(path, ".git")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent
    return None


def git_head(cwd) -> tuple | None:
    """(toplevel, ref) for cwd: the branch HEAD points at (refs/heads/ stripped), or the first
    7 characters of a detached hash. None without a readable git directory."""
    try:
        if not isinstance(cwd, str) or not os.path.isabs(cwd):
            return None
        top = toplevel(cwd)
        gd = _git_dir(top) if top else None
        if not gd:
            return None
        with open(os.path.join(gd, "HEAD")) as f:
            head = f.read(4096).strip()
        if head.startswith("ref:"):
            ref = head[4:].strip()
            ref = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
        elif len(head) >= 7 and all(c in "0123456789abcdef" for c in head):
            ref = head[:7]
        else:
            return None
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


def peers(table: dict, proc_root: str, self_pid: int, cwd) -> dict | None:
    """{here, total}: live session roots elsewhere in the same place as this session, and all
    live sessions including this one. None when the hook is not under a session root."""
    names = _names()
    chain = _ancestors(table, self_pid)
    own = [p for p in chain if table[p].get("comm") in names]
    if not own:
        return None
    own_root = own[-1]
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        cwd = _cwd(proc_root, own_root)
        if not cwd:
            return None
    mine = toplevel(cwd)
    here, total = 0, 1
    for pid, e in table.items():
        if pid == own_root or e.get("comm") not in names or e.get("state") in ("Z", "T", "t", "X", "x"):
            continue
        if any(table[a].get("comm") in names for a in _ancestors(table, e.get("ppid"))):
            continue
        total += 1
        pc = _cwd(proc_root, pid)
        if pc is None:
            continue
        if (toplevel(pc) == mine) if mine else (os.path.normpath(pc) == os.path.normpath(cwd)):
            here += 1
    return {"here": here, "total": total}


def _uptime(proc_root: str) -> float | None:
    try:
        with open(os.path.join(proc_root, "uptime")) as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _marked(proc_root: str, pid: int, value: bytes) -> bool:
    try:
        with open(os.path.join(proc_root, str(pid), "environ"), "rb") as f:
            env = f.read(262144)
    except OSError:
        return False
    return (b"\0" + env).find(b"\0" + MARKER + value + b"\0") >= 0


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


def work_reading(hook_input, table, proc_root: str = "/proc", sdir=None, now=None,
                 pulse: bool = False, self_pid: int | None = None) -> dict:
    """{segs, force, flags} for this session: segments to append in order (HEAD moved, peers,
    bg), whether one of them must force the line (a said-once item), and its log flags."""
    out = {"segs": [], "force": False, "flags": set()}
    try:
        if not isinstance(hook_input, dict) or hook_input.get("agent_id"):
            return out
        sid = safe_id(hook_input.get("session_id"))
        if not sid:
            return out
        now = time.time() if now is None else now
        self_pid = _self_pid() if self_pid is None else self_pid
        cwd = hook_input.get("cwd")
        cwd = cwd if isinstance(cwd, str) and os.path.isabs(cwd) else None
        table = table if isinstance(table, dict) else {}
        prev = _read(sdir, sid)
        new = {"ts": now, "top": _str(prev.get("top")), "head": _str(prev.get("head"))}
        p_told = prev.get("peers") if isinstance(prev.get("peers"), int) and not isinstance(prev.get("peers"), bool) \
            and prev["peers"] >= 0 else 0
        new["peers"] = p_told
        head_seg = peer_seg = None
        peer_new = False
        if _on("SOMA_HEAD") and cwd:
            g = git_head(cwd)
            top, ref = g if g else (None, None)
            if top and top == new["top"] and new["head"] and ref != new["head"]:
                cmd = hook_input.get("tool_input", {}).get("command") if isinstance(hook_input.get("tool_input"), dict) else None
                own = pulse and hook_input.get("tool_name") == "Bash" and isinstance(cmd, str) and "git" in cmd
                if not own:
                    head_seg = f"HEAD {new['head']}→{ref} since last {'tool call' if pulse else 'prompt'}"
            new["top"], new["head"] = top, ref
        if _on("SOMA_PEERS") and table:
            pr = peers(table, proc_root, self_pid, cwd)
            if pr is not None:
                new["peers"] = pr["here"]
                if pr["here"] > 0:
                    peer_seg = f"peers {pr['here']} here ({pr['total']} sessions)"
                    peer_new = p_told == 0
        claim = _claim(sdir, sid, prev.get("ts")) if head_seg or peer_new else None
        if (head_seg or peer_new) and not claim:
            head_seg, peer_new = None, False  # another caller is saying it; this one stays quiet
            peer_seg = peer_seg if p_told else None
            return {"segs": [s for s in (peer_seg,) if s], "force": False, "flags": set()}
        same = all(prev.get(k) == new[k] for k in ("top", "head", "peers")) and \
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
