"""
Context-window and rate-limit sense for soma hooks.

The agent's own context window is part of its body: how full it is decides
whether it has room to keep working before the harness compacts it, and the
plan's rate-limit use decides how much parallel effort it can still spend.
Claude Code hands those numbers only to the STATUSLINE command, never to
hooks, so this module has two halves:

  - write_from_statusline(): called by soma-context.py, which a statusline
    script pipes its stdin JSON to. Writes one small per-session state file,
    atomically, and prunes files of dead sessions. Prints nothing.
  - context_segment(): called by soma-state.py while building the
    [system-state] line. Reads this session's state file; without one, falls
    back to the last `usage` entry in the session transcript (tokens only,
    the window size is not in the transcript).

State file: <state_dir>/soma-ctx/<session_id>.json
  {"ts": 1791320000, "used_pct": 87, "used_tokens": 865627, "window": 1000000,
   "five_hour": {"used_pct": 7, "resets_at": 1791327000} | null,
   "seven_day": {"used_pct": 19, "resets_at": 1791723600} | null}

Pure stdlib, and deliberately only json/os/time so the statusline writer
starts fast. Nothing here raises into a hook or a statusline.
"""

import json
import math
import os
import stat
import time

CTX_SUBDIR = "soma-ctx"
USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
# Max age of a state file the reader trusts. Context only changes on a model
# call, and every model call refreshes the statusline, so an idle session's
# numbers stay true overnight; a day bounds the case where the statusline
# integration was removed or a session is resumed much later.
CTX_MAX_AGE_S = 86400
PRUNE_AGE_S = 3 * 86400        # state files of sessions silent this long are removed
PRUNE_EVERY_S = 3600           # the prune scan runs at most this often
TAIL_MAX_BYTES = 4 * 1024 * 1024   # transcript fallback never reads more than this from the end
TAIL_FIRST_CHUNK = 64 * 1024
SAFE_ID_MAX = 128              # id + ".json" + ".<id>.<pid>.tmp" must stay under NAME_MAX (255)
CTX_PCT_MAX = 100              # a context fill outside 0..100 is junk
RATE_PCT_MAX = 1000            # a quota can read over 100, but not without bound
BIG = 1e12                     # token counts and window sizes beyond this are junk
EPOCH_MS = 1e11                # a resets_at above this is epoch milliseconds
OFF_VALUES = ("0", "off", "false", "no")


def state_dir(override: str | None = None) -> str:
    """Soma's state directory: override, SOMA_STATE_DIR, CLAUDE_KIT_STATE_DIR, ~/.claude/state."""
    return (override or os.environ.get("SOMA_STATE_DIR") or os.environ.get("CLAUDE_KIT_STATE_DIR")
            or os.path.join(os.path.expanduser("~"), ".claude", "state"))


def safe_id(session_id) -> str | None:
    """The session id reduced to [A-Za-z0-9_-] (it becomes a file name), or None when nothing is left."""
    if not isinstance(session_id, str):
        return None
    out = "".join(c for c in session_id[:SAFE_ID_MAX] if c.isascii() and (c.isalnum() or c in "-_"))
    return out or None


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x - x == 0


def _floor(x) -> int | None:
    return int(x // 1) if _num(x) else None


def _pct(x, hi) -> int | None:
    """x floored when it is a number within 0..hi, else None."""
    return _floor(x) if _num(x) and 0 <= x <= hi else None


def _count(x) -> int | None:
    """A token count or window size: a non-negative, bounded number, else None."""
    return _floor(x) if _num(x) and 0 <= x <= BIG else None


def _epoch(x) -> int | None:
    """resets_at as epoch seconds; epoch milliseconds are accepted, anything else is None."""
    if not _num(x) or x < 0:
        return None
    if x > EPOCH_MS:
        x = x / 1000
    return _floor(x) if x <= EPOCH_MS else None


def _env_pos_float(name: str, default: float) -> float:
    """A finite, non-negative float from the environment, else the default."""
    try:
        v = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) and v >= 0 else default


def _ctx_path(sid: str, sdir: str | None) -> str:
    return os.path.join(state_dir(sdir), CTX_SUBDIR, sid + ".json")


# --- writer (statusline side) -------------------------------------------------

def _window(w) -> dict | None:
    if not isinstance(w, dict):
        return None
    pct = _pct(w.get("used_percentage"), RATE_PCT_MAX)
    if pct is None:
        return None
    return {"used_pct": pct, "resets_at": _epoch(w.get("resets_at"))}


def write_from_statusline(doc, sdir: str | None = None, now: float | None = None) -> None:
    """Persist the statusline's context and rate-limit numbers for this session. Never raises."""
    tmp = None
    try:
        if not isinstance(doc, dict):
            return
        sid = safe_id(doc.get("session_id"))
        if not sid:
            return
        now = time.time() if now is None else now
        cw = doc.get("context_window")
        cw = cw if isinstance(cw, dict) else {}
        cu = cw.get("current_usage")
        used = None
        if isinstance(cu, dict):
            vals = [cu.get(k) for k in USAGE_KEYS if _count(cu.get(k)) is not None]
            used = _count(sum(vals)) if vals else None
        rl = doc.get("rate_limits")
        rl = rl if isinstance(rl, dict) else {}
        out = {"ts": int(now), "used_pct": _pct(cw.get("used_percentage"), CTX_PCT_MAX), "used_tokens": used,
               "window": _count(cw.get("context_window_size")),
               "five_hour": _window(rl.get("five_hour")), "seven_day": _window(rl.get("seven_day"))}
        if all(out[k] is None for k in out if k != "ts"):
            return
        d = os.path.join(state_dir(sdir), CTX_SUBDIR)
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, f".{sid}.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(out))
        os.replace(tmp, os.path.join(d, sid + ".json"))
        tmp = None
        _maybe_prune(d, now)
    except Exception:
        return
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _maybe_prune(d: str, now: float) -> None:
    """Remove state and temp files older than PRUNE_AGE_S, at most once per PRUNE_EVERY_S."""
    marker = os.path.join(d, ".pruned")
    try:
        if 0 <= now - os.stat(marker).st_mtime < PRUNE_EVERY_S:  # a future mtime counts as due
            return
    except OSError:
        pass
    try:
        with os.scandir(d) as it:
            for e in it:
                if e.name.endswith((".json", ".tmp")) and now - e.stat().st_mtime > PRUNE_AGE_S:
                    os.unlink(e.path)
        with open(marker, "w"):
            pass
    except OSError:
        return


# --- reader (hook side) ---------------------------------------------------------

def read_state(sid: str, sdir: str | None = None, now: float | None = None) -> dict | None:
    """This session's state file when it is valid and fresh enough, else None."""
    try:
        with open(_ctx_path(sid, sdir), encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        return None
    if not isinstance(doc, dict) or not _num(doc.get("ts")):
        return None
    now = time.time() if now is None else now
    max_age = _env_pos_float("SOMA_CTX_MAX_AGE_S", CTX_MAX_AGE_S)
    if now - doc["ts"] > max_age or doc["ts"] - now > 300:
        return None
    return doc


def _usage_tokens(line: bytes) -> int | None:
    if b'"usage"' not in line:
        return None
    try:
        d = json.loads(line)
    except ValueError:
        return None
    if not isinstance(d, dict) or d.get("type") != "assistant" or d.get("isSidechain"):
        return None
    msg = d.get("message")
    usage = msg.get("usage") if isinstance(msg, dict) else None
    if not isinstance(usage, dict):
        return None
    total = sum(usage[k] for k in USAGE_KEYS if _num(usage.get(k)))
    return int(total) if total > 0 else None


def transcript_tokens(path, max_bytes: int | None = None) -> int | None:
    """Context tokens at the last model call, from the last assistant `usage` in a
    JSONL transcript. Reads backwards from the end, bounded by TAIL_MAX_BYTES."""
    if not isinstance(path, str) or not path:
        return None
    limit = TAIL_MAX_BYTES if max_bytes is None else max_bytes
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):  # a FIFO with no writer would block open()
            return None
        with open(path, "rb") as f:
            pos = f.seek(0, 2)
            partial, step, scanned = b"", TAIL_FIRST_CHUNK, 0
            while pos > 0 and scanned < limit:
                n = min(step, pos, limit - scanned)
                pos -= n
                f.seek(pos)
                partial = f.read(n) + partial
                scanned += n
                step *= 2
                lines = partial.split(b"\n")
                # the first piece may be cut mid-line; keep it for the next read
                partial = lines[0] if pos > 0 else b""
                for line in reversed(lines[1:] if pos > 0 else lines):
                    tokens = _usage_tokens(line)
                    if tokens:
                        return tokens
    except Exception:
        return None
    return None


def _k(tokens: int) -> str:
    return f"{round(tokens / 1000)}k"


def context_segment(hook_input, sdir: str | None = None, now: float | None = None) -> tuple:
    """('ctx 87% (866k/1000k)(HIGH) · 5h 7% · 7d 19%', high) for this session, or (None, False).

    Off with SOMA_CTX=0|off|false|no. High when the used share reaches SOMA_CTX_PCT (default 85; 0 disables)."""
    try:
        if os.environ.get("SOMA_CTX", "1").strip().lower() in OFF_VALUES or not isinstance(hook_input, dict):
            return None, False
        now = time.time() if now is None else now
        sid = safe_id(hook_input.get("session_id"))
        doc = (read_state(sid, sdir, now) if sid else None) or {}
        pct, tokens, window = doc.get("used_pct"), doc.get("used_tokens"), doc.get("window")
        tokens = _count(tokens) or None
        window = _count(window) or None
        pct = _pct(pct, CTX_PCT_MAX)
        if pct is None and tokens and window:
            pct = _pct(tokens * 100 // window, CTX_PCT_MAX)
        parts, high = [], False
        if pct is not None:
            th = _env_pos_float("SOMA_CTX_PCT", 85.0)
            high = bool(th) and pct >= th
            size = f" ({_k(tokens)}/{_k(window)})" if tokens and window else ""
            parts.append(f"ctx {pct}%{size}" + ("(HIGH)" if high else ""))
        else:
            tokens = tokens or transcript_tokens(hook_input.get("transcript_path"))
            if tokens and tokens >= 1000:  # below that "ctx 0k" says nothing
                parts.append(f"ctx {_k(tokens)}")
        for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
            w = doc.get(key)
            wp = _pct(w.get("used_pct"), RATE_PCT_MAX) if isinstance(w, dict) else None
            if wp is None:
                continue
            reset = _epoch(w.get("resets_at"))
            if reset is not None and reset <= now:
                continue  # the window has rolled over; its old figure is no longer true
            parts.append(f"{label} {wp}%")
        return (" · ".join(parts) or None), high
    except Exception:
        return None, False
