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
   "seven_day": {"used_pct": 19, "resets_at": 1791723600} | null,
   "samples": [{"ts", "used_tokens", "used_pct", "five_pct", "seven_pct"}, ...] (<= 24, 0.12.0),
   "five_series": [{"ts", "pct"}, ...] (the 5-hour window, <= one point per 300 s, <= 24, 0.12.0),
   "model": "claude-opus-5-5", "model_prev": ..., "model_changed_ts": ..., "model_first": ...,
   "cost": 41.2, "rl_seen": true}
The model this session was last told about (hook side): <state_dir>/soma-model/<session_id>.json
  {"told": "claude-opus-5-5"}, written only when a notice is said.
The prompt hook's own fill-per-turn history: <state_dir>/soma-turns/<session_id>.json
  {"fills": [[statusline ts, fill pct], ...]} (<= 4), written only by the prompt hook.

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
SAMPLES_MAX = 24               # usage history kept in the state file (one entry per change of used_tokens)
MODEL_MAX = 128                # a model id longer than this is junk
COST_MAX = 1e6                 # a session cost above this many dollars is junk
MODEL_SUBDIR = "soma-model"    # the hook side's record of the model this session was last told
TURNS_SUBDIR = "soma-turns"    # the prompt hook's per-session fill-per-turn history
TURNS_KEEP = 4                 # fills kept: the last 3 completed turns
TURNS_MIN = 2                  # completed turns needed before a rate is shown
TURNS_SHOW_MAX = 10            # the rate is shown only when this few turns are left
SERIES_STEP_S = 300            # the 5-hour series keeps at most one point per this many seconds
SERIES_MAX = 24                # ... and at most this many points (two hours)
QUOTA_MIN_SPAN_S = 900         # a quota rate needs at least this much history
QUOTA_MIN_RISE = 5             # ... rising at least this many points over it (whole percentages)
QUOTA_BURST_RISE = 3           # the recent third leads the rate only when it alone rises this much
QUOTA_FRESH_S = 600            # a series whose newest point is older than this projects nothing
QUOTA_FLAG_PCT = 50            # QUOTA is raised only for a window at least this used
WINDOW_S = 5 * 3600            # the five-hour window (the only one projected)
NOTICE_MAX_AGE_S = 86400       # a model change older than this is not announced
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


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


def _model_id(x) -> str | None:
    """A model id worth printing: a short string of visible ASCII, else None."""
    if isinstance(x, str) and 0 < len(x) <= MODEL_MAX and all(33 <= ord(c) < 127 for c in x):
        return x
    return None


def _samples(x) -> list:
    """The stored sample history, keeping only well-formed entries (an older file has none)."""
    if not isinstance(x, list):
        return []
    out = []
    for s in x[-SAMPLES_MAX:]:
        if isinstance(s, dict) and _num(s.get("ts")) and _count(s.get("used_tokens")) is not None:
            out.append({"ts": int(s["ts"]), "used_tokens": int(s["used_tokens"]),
                        "used_pct": _pct(s.get("used_pct"), CTX_PCT_MAX),
                        "five_pct": _pct(s.get("five_pct"), RATE_PCT_MAX),
                        "seven_pct": _pct(s.get("seven_pct"), RATE_PCT_MAX)})
    return out


def _series(x) -> list:
    """The stored 5-hour series, keeping only well-formed points (an older file has none)."""
    if not isinstance(x, list):
        return []
    return [{"ts": int(p["ts"]), "pct": int(p["pct"])} for p in x[-SERIES_MAX:]
            if isinstance(p, dict) and _num(p.get("ts")) and _pct(p.get("pct"), RATE_PCT_MAX) is not None]


def _next_series(old: dict, fh, now: float) -> list:
    """The 5-hour series after this reading: a point at most every SERIES_STEP_S, started over
    when the window resets (its use falls or its resets_at changes) or the clock goes back."""
    series = _series(old.get("five_series"))
    if not fh:
        return series
    ofh = old.get("five_hour")
    if series and (fh["used_pct"] < series[-1]["pct"] or now < series[-1]["ts"]
                   or not isinstance(ofh, dict) or ofh.get("resets_at") != fh["resets_at"]):
        series = []
    if not series or now - series[-1]["ts"] >= SERIES_STEP_S:
        series.append({"ts": int(now), "pct": fh["used_pct"]})
    return series[-SERIES_MAX:]


def write_from_statusline(doc, sdir: str | None = None, now: float | None = None) -> None:
    """Persist the statusline's context and rate-limit numbers for this session. Never raises."""
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
        m = doc.get("model")
        model = _model_id(m.get("id")) if isinstance(m, dict) else None
        c = doc.get("cost")
        cost = c.get("total_cost_usd") if isinstance(c, dict) else None
        cost = round(cost, 2) if _num(cost) and 0 <= cost <= COST_MAX else None
        if all(out[k] is None for k in out if k != "ts") and model is None and cost is None:
            return
        old = read_session_json(CTX_SUBDIR, sid, sdir) or {}
        samples = _samples(old.get("samples"))
        if used is not None and (not samples or samples[-1]["used_tokens"] != used):
            samples.append({"ts": int(now), "used_tokens": used, "used_pct": out["used_pct"],
                            "five_pct": out["five_hour"]["used_pct"] if out["five_hour"] else None,
                            "seven_pct": out["seven_day"]["used_pct"] if out["seven_day"] else None})
        out["samples"] = samples[-SAMPLES_MAX:]
        out["five_series"] = _next_series(old, out["five_hour"], now)
        prev = _model_id(old.get("model"))
        out["model"] = model or prev
        out["model_first"] = _model_id(old.get("model_first")) or prev or model
        if model and prev and model != prev:
            out["model_prev"], out["model_changed_ts"] = prev, int(now)
        elif out["model"] == prev and _model_id(old.get("model_prev")) and _num(old.get("model_changed_ts")):
            out["model_prev"], out["model_changed_ts"] = old["model_prev"], int(old["model_changed_ts"])
        out["cost"] = cost
        # sticky: a session that has carried rate_limits once is a subscription session for good
        out["rl_seen"] = isinstance(doc.get("rate_limits"), dict) or old.get("rl_seen") is True
        write_session_json(CTX_SUBDIR, sid, out, sdir, now)
    except Exception:
        return


def write_session_json(subdir: str, sid, doc: dict, sdir: str | None = None, now: float | None = None) -> bool:
    """Atomically write <state_dir>/<subdir>/<safe sid>.json, pruning dead sessions' files
    on the way (3 days, at most hourly). The shared per-session store of the statusline
    bridge and the pulse hook. True when written; never raises."""
    tmp = None
    try:
        sid = safe_id(sid)
        if not sid:
            return False
        now = time.time() if now is None else now
        d = os.path.join(state_dir(sdir), subdir)
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, f".{sid}.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(doc))
        os.replace(tmp, os.path.join(d, sid + ".json"))
        tmp = None
        _maybe_prune(d, now)
        return True
    except Exception:
        return False
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def read_session_json(subdir: str, sid, sdir: str | None = None) -> dict | None:
    """The dict stored by write_session_json for this session, or None when absent, corrupt or oversized."""
    try:
        sid = safe_id(sid)
        if not sid:
            return None
        with open(os.path.join(state_dir(sdir), subdir, sid + ".json"), "rb") as f:
            raw = f.read(65537)
        if len(raw) > 65536:
            return None
        doc = json.loads(raw)
        return doc if isinstance(doc, dict) else None
    except Exception:
        return None


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
                if e.name.endswith((".json", ".tmp", ".claim")) and now - e.stat().st_mtime > PRUNE_AGE_S:
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


def _hm(t: float, weekday: bool = False) -> str:
    """Local HH:MM, with an English weekday (locale independent) when asked."""
    lt = time.localtime(t)
    return (DAYS[lt.tm_wday] + " " if weekday else "") + f"{lt.tm_hour:02d}:{lt.tm_min:02d}"


def _last_compaction(sid: str, doc: dict, sdir: str | None) -> float:
    """Latest compaction this session is known to have had (0 when none): the compaction
    record soma_compact keeps, and any drop of used_tokens in the bridge's own samples."""
    cut = 0.0
    rec = read_session_json("soma-compact", sid, sdir)
    if isinstance(rec, dict) and _num(rec.get("ts")):
        cut = float(rec["ts"])
    samples = _samples(doc.get("samples"))
    for a, b in zip(samples, samples[1:]):
        if b["used_tokens"] < a["used_tokens"]:
            cut = max(cut, float(b["ts"]))
    return cut


def _turn_rate(sid: str, doc: dict, fill: float, sdir: str | None, record: bool) -> tuple:
    """(fill growth per completed turn in points, turns known) from the prompt hook's own
    history (<state_dir>/soma-turns/<sid>.json), after noting this prompt's reading when
    record is set. The growth is the larger of the mean of the last up to 3 turns and the
    latest turn alone, so a big latest turn is not averaged away. A real turn always adds
    tokens: a reading with the same statusline ts or the same fill is not a new turn; a fill
    that drops (a compaction) or a reading older than the last one (a clock gone back)
    starts the history over. Never raises; (None, 0) when there is nothing to say."""
    try:
        old = read_session_json(TURNS_SUBDIR, sid, sdir) or {}
        fills = old.get("fills")
        fills = [f for f in fills if isinstance(f, list) and len(f) == 2 and _num(f[0]) and _num(f[1])] \
            if isinstance(fills, list) else []
        ts = doc.get("ts")
        if record and _num(ts):
            if not fills or ts < fills[-1][0] or fill < fills[-1][1]:
                fills = [[ts, fill]]
            elif ts > fills[-1][0] and fill > fills[-1][1]:
                fills = (fills + [[ts, fill]])[-TURNS_KEEP:]
            write_session_json(TURNS_SUBDIR, sid, {"fills": fills}, sdir)
        # a compaction since a stored fill makes every delta across it meaningless, even when
        # the context has grown back past the old fill by the time of the next prompt
        cut = _last_compaction(sid, doc, sdir)
        fills = [f for f in fills[-TURNS_KEEP:] if f[0] >= cut]
        for a, b in zip(fills, fills[1:]):
            if b[0] <= a[0] or b[1] < a[1]:
                return None, 0
        n = len(fills) - 1
        # the history ends at this reading: the same statusline ts, or the same fill (a refresh
        # without growth, kept under the earlier ts)
        if n < 1 or not _num(ts) or ts < fills[-1][0] or (fills[-1][0] != ts and fills[-1][1] != fill):
            return None, 0
        return max((fills[-1][1] - fills[0][1]) / n, fills[-1][1] - fills[-2][1]), n
    except Exception:
        return None, 0


def _rate(pts: list) -> float | None:
    """Points per second over a series of (ts, pct), or None when its span is empty."""
    span = pts[-1][0] - pts[0][0]
    return (pts[-1][1] - pts[0][1]) / span if span > 0 else None


def _projection(doc: dict, wp: int, reset, now: float) -> int | None:
    """Epoch second at which the 5-hour window runs out, or None when the evidence is thin.
    Read from the sparse series only, never from the per-token samples: whole percentages
    over a few minutes are rounding noise. Needs a series that is strictly later in time and
    non-decreasing in use, starts inside the current window, spans QUOTA_MIN_SPAN_S, rises
    QUOTA_MIN_RISE points and has a newest point at most QUOTA_FRESH_S old and not from the
    future. The rate is the higher of the whole span's and, when it alone rises
    QUOTA_BURST_RISE points, the most recent third's (pessimistic on purpose). None also for
    a run-out that is past or would not come before the reset."""
    if reset is None or reset <= now or wp >= 100:
        return None
    pts = [(p["ts"], p["pct"]) for p in _series(doc.get("five_series"))]
    if len(pts) < 2 or not 0 <= now - pts[-1][0] <= QUOTA_FRESH_S or pts[0][0] < reset - WINDOW_S:
        return None
    for a, b in zip(pts, pts[1:]):
        if b[0] <= a[0] or b[1] < a[1]:
            return None
    span = pts[-1][0] - pts[0][0]
    if span < QUOTA_MIN_SPAN_S or pts[-1][1] - pts[0][1] < QUOTA_MIN_RISE:
        return None
    rate = _rate(pts)
    recent = [p for p in pts if p[0] >= pts[-1][0] - span / 3]
    if len(recent) >= 2 and recent[-1][1] - recent[0][1] >= QUOTA_BURST_RISE:
        rate = max(rate, _rate(recent))
    # count from the current reading when it is newer and no lower than the series' end
    base_ts, base = pts[-1]
    if _num(doc.get("ts")) and base_ts <= doc["ts"] <= now + 60 and wp >= base:
        base_ts, base = int(doc["ts"]), wp
    out = base_ts + (100 - base) / rate
    return int(out) if now < out < reset else None


def context_reading(hook_input, sdir: str | None = None, now: float | None = None) -> dict:
    """This session's context and quota reading: {"seg": 'ctx 71% (710k/1000k, +6%/turn,
    ~4 turns left) · 5h 62% (out ~22:10, resets 00:50) · 7d 19%' or None, "high": bool,
    "quota": bool}.

    Off with SOMA_CTX=0|off|false|no. High when the used share reaches SOMA_CTX_PCT (default 85;
    0 disables). The fill rate is shown only when it says something (2+ turns, growing, at most
    10 turns to SOMA_CTX_FULL_PCT, default 95); a quota projection only for the 5-hour window,
    only on enough evidence (see _projection), and quota is True only with a printed projection
    on a window at 50 % or more (SOMA_QUOTA=0 turns both off). The 7-day window prints its level. Cost only on a session that has never carried rate_limits. A main-agent
    call (no agent_id) notes this prompt's fill as a turn."""
    res = {"seg": None, "high": False, "quota": False}
    try:
        if os.environ.get("SOMA_CTX", "1").strip().lower() in OFF_VALUES or not isinstance(hook_input, dict):
            return res
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
            fill = tokens * 100 / window if tokens and window and tokens <= window else float(pct)
            rate, n = _turn_rate(sid, doc, fill, sdir, not hook_input.get("agent_id"))
            full = _env_pos_float("SOMA_CTX_FULL_PCT", 95.0)
            extra = ""
            if rate and rate > 0 and n >= TURNS_MIN and fill < full:
                # rounded first: 3.2 / 0.4 is 8.000000000000002 in floats, and 8 turns, not 9
                q = round((full - fill) / rate, 6)
                left = math.ceil(q)
                if q <= TURNS_SHOW_MAX:
                    r = round(rate)
                    extra = (f", +{r}%/turn" if r >= 1 else ", +<1%/turn") \
                        + f", ~{left} turn{'s' if left != 1 else ''} left"
            size = f"{_k(tokens)}/{_k(window)}" if tokens and window else ""
            inner = (size + extra).lstrip(", ")
            parts.append(f"ctx {pct}%" + (f" ({inner})" if inner else "") + ("(HIGH)" if high else ""))
        else:
            tokens = tokens or transcript_tokens(hook_input.get("transcript_path"))
            if tokens and tokens >= 1000:  # below that "ctx 0k" says nothing
                parts.append(f"ctx {_k(tokens)}")
        quota_on = os.environ.get("SOMA_QUOTA", "1").strip().lower() not in OFF_VALUES
        for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
            w = doc.get(key)
            wp = _pct(w.get("used_pct"), RATE_PCT_MAX) if isinstance(w, dict) else None
            if wp is None:
                continue
            reset = _epoch(w.get("resets_at"))
            if reset is not None and reset <= now:
                continue  # the window has rolled over; its old figure is no longer true
            # the 7-day window is never projected: a 1-point step of a whole percentage inside
            # the minutes a session sees reads as a run-out days early (0.12.0 fix round A)
            out = _projection(doc, wp, reset, now) if quota_on and key == "five_hour" else None
            if out is not None:
                parts.append(f"{label} {wp}% (out ~{_hm(out)}, resets {_hm(reset)})")
                res["quota"] = res["quota"] or wp >= QUOTA_FLAG_PCT
            else:
                parts.append(f"{label} {wp}%")
        cost = doc.get("cost")
        if doc.get("rl_seen") is False and doc.get("five_hour") is None and doc.get("seven_day") is None \
                and _num(cost) and 0 < cost <= COST_MAX:
            parts.append(f"cost ${cost:.2f}")
        res["seg"], res["high"] = (" · ".join(parts) or None), high
        return res
    except Exception:
        return {"seg": None, "high": False, "quota": False}


def context_segment(hook_input, sdir: str | None = None, now: float | None = None) -> tuple:
    """(segment, high) of context_reading(), the 0.10/0.11 interface."""
    r = context_reading(hook_input, sdir, now)
    return r["seg"], r["high"]


def take_model_notice(sid, sdir: str | None = None, now: float | None = None) -> str | None:
    """'model <id> (was <told> until HH:MM)' when the serving model differs from the one this
    session was last told about (or, before any notice, the first one its statusline saw),
    once per change through an exclusive claim keyed by the change time and the new model.
    A change away and back before a prompt says nothing. None when there is no change, it is
    older than 24 h or from the future, it was already said, or the state is junk. Never raises."""
    try:
        s = safe_id(sid)
        if not s or os.environ.get("SOMA_CTX", "1").strip().lower() in OFF_VALUES:
            return None
        now = time.time() if now is None else now
        doc = read_state(s, sdir, now) or {}
        model, ts = _model_id(doc.get("model")), doc.get("model_changed_ts")
        rec = read_session_json(MODEL_SUBDIR, s, sdir) or {}
        told = _model_id(rec.get("told")) or _model_id(doc.get("model_first")) or _model_id(doc.get("model_prev"))
        if not model or not told or model == told or not _num(ts) or not 0 <= now - ts <= NOTICE_MAX_AGE_S:
            return None
        claim = os.path.join(state_dir(sdir), CTX_SUBDIR, f"{s}.model.{int(ts)}.{safe_id(model)}.claim")
        try:
            os.close(os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        except OSError:
            return None
        write_session_json(MODEL_SUBDIR, s, {"told": model}, sdir, now)
        return f"model {model} (was {told} until {_hm(ts)})"
    except Exception:
        return None
