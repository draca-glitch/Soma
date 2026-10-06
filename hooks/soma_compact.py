"""Soma compaction awareness (0.11.0): what the compaction summary dropped.

When the harness compacts a session, the agent keeps a prose summary and loses the
concrete identifiers it was holding (paths, commit hashes, #ids, URLs, agent ids)
without being able to tell which. PreCompact indexes the span about to be compacted;
PostCompact diffs that index against the summary and the messages the harness kept
verbatim, writes the dropped identifiers (plus the user's own messages of the span) to
an index file, and leaves a pending notice that the prompt hook or the pulse appends
to the [system-state] line once. Identifiers only: it does not recover reasoning, and
"kept" means a whole-token occurrence in the summary or a preserved message (a file path
also by a basename no other indexed path shares).

Files under <state_dir>/soma-compact/: <sid>.pre.json (PreCompact index), <sid>.json
(the notice state), <sid>-<epoch>.md (the dropped index, 0600). Pruned after 3 days.
Pure stdlib (json/os/re/stat/time; datetime for the boundary timestamp). Never raises.
"""

import json
import os
import re
import stat
import time
from datetime import datetime

from soma_ctx import (OFF_VALUES, PRUNE_AGE_S, PRUNE_EVERY_S, TAIL_MAX_BYTES, _k, safe_id, state_dir,
                      transcript_tokens)

SUBDIR = "soma-compact"
READ_MAX_BYTES = 64 * 1024 * 1024   # never read more than this from the transcript's end
PRE_MAX_AGE_S = 15 * 60             # an older pre index belongs to some other compaction
NOTICE_MAX_AGE_S = 24 * 3600        # a notice nobody announced within a day is dropped
FRESH_BOUNDARY_S = 15 * 60          # a boundary this recent is the compaction being handled
PRE_MAX_BYTES = 32 * 1024 * 1024
CLASS_CAP = 500
USER_MSG_CAP = 2000
USER_MSGS_MAX = 200
CLASSES = ("paths", "hashes", "ids", "urls", "agents")
SINGULAR = {"paths": "path", "hashes": "hash", "ids": "id", "urls": "url", "agents": "agent"}

URL_RE = re.compile(r"https?://[^\s\"'<>()\[\]{}`]+")
PATH_RE = re.compile(r"(?<![\w.~:/\\-])(/[\w.@+-]+(?:/[\w.@+-]+)+)")
# a UUID is removed before the hash scan; a hash is not cut out of a longer hex-and-dash run
UUID_RE = re.compile(r"\b(?<![0-9A-Fa-f]-)[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}\b(?!-[0-9A-Fa-f])")
HASH_RE = re.compile(r"\b(?<![0-9A-Fa-f]-)[0-9a-f]{7,40}\b(?!-[0-9A-Fa-f])")
# not after & or # or a word char, not after ':' or ': ', not followed by ';' or '}' (a CSS value)
ID_RE = re.compile(r"(?<![\w&#:])(?<!: )#\d{3,7}\b(?![;}])")
CSS_LINE = re.compile(r"color|background", re.I)
WHOLE_PATH_HEAD = re.compile(r"/[^/\s*]+/")  # a non-empty first segment: not // or /* code
URL_CHARS = r"[^\s\"'<>()\[\]{}`]"
AGENT_RE = re.compile(r"agentId:\s*([A-Za-z0-9_-]{4,64})")
HASH_DIGIT = re.compile(r"[0-9]")
HASH_LETTER = re.compile(r"[a-f]")


def enabled() -> bool:
    return os.environ.get("SOMA_COMPACT", "1").strip().lower() not in OFF_VALUES


def new_index() -> dict:
    return {**{c: {} for c in CLASSES}, "user": []}


def _bump(d: dict, key: str) -> None:
    d[key] = d.get(key, 0) + 1


def index_text(idx: dict, text: str) -> None:
    """Add the identifiers in one piece of agent-held text (not a tool result) to idx."""
    if not text:
        return
    for m in URL_RE.findall(text):
        _bump(idx["urls"], m.rstrip(".,;:!?"))
    rest = URL_RE.sub(" ", text)
    for m in PATH_RE.findall(rest):
        p = m.rstrip(".,;:!?")
        if p.count("/") >= 2 and not p.endswith("/"):
            _bump(idx["paths"], p)
    for m in HASH_RE.findall(UUID_RE.sub(" ", rest) if "-" in rest else rest):
        if HASH_DIGIT.search(m) and HASH_LETTER.search(m):
            _bump(idx["hashes"], m)
    for m in ID_RE.finditer(rest) if "#" in rest else ():
        if len(m.group()) == 7:  # six digits on a line about colours is a colour
            a, b = rest.rfind("\n", 0, m.start()) + 1, rest.find("\n", m.end())
            if CSS_LINE.search(rest[a:b if b >= 0 else len(rest)]):
                continue
        _bump(idx["ids"], m.group())


def whole_path(v: str) -> bool:
    """A tool input value that is itself an absolute path: taken whole, spaces included."""
    return (WHOLE_PATH_HEAD.match(v) is not None and "\n" not in v and not v.endswith("/")
            and len(v) <= 4096)


def _strings(x, out: list, paths: list, depth: int = 0, key=None) -> list:
    """Every string value in a tool_use input, recursively (bounded depth); a value that is
    itself an absolute path (not a command) goes to paths whole instead."""
    if depth > 20:
        return out
    if isinstance(x, str):
        (paths if key != "command" and whole_path(x) else out).append(x)
    elif isinstance(x, dict):
        for k, v in x.items():
            _strings(v, out, paths, depth + 1, k)
    elif isinstance(x, list):
        for v in x:
            _strings(v, out, paths, depth + 1, key)
    return out


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def _reinjected(text: str) -> bool:
    """A system reminder: the harness injects these again after a compaction, so their
    identifiers (a CLAUDE.md, a file listing) are not lost and would only be noise."""
    return text.lstrip().startswith("<system-reminder>")


INTERRUPTED_RE = re.compile(r"\[Request interrupted by user[^\]\n]*\]")


def _typed(text: str) -> bool:
    t = text.strip()
    return bool(t) and not t.startswith("<") and INTERRUPTED_RE.fullmatch(t) is None


def entry_texts(e: dict, paths: list | None = None) -> tuple:
    """(agent-held texts, tool-result texts, human-typed text or None) of one transcript entry;
    tool input values that are whole absolute paths go to `paths` (or to held without it)."""
    held, results, typed = [], [], None
    msg = e.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if e.get("isMeta") or e.get("isCompactSummary"):
        return held, results, typed  # harness-written, not something the agent was holding
    if isinstance(content, str):
        if not _reinjected(content):
            held.append(content)
        if e.get("type") == "user" and _typed(content):
            typed = content
        return held, results, typed
    if not isinstance(content, list):
        return held, results, typed
    whole = paths if paths is not None else held
    texts, has_result = [], False
    for b in content:
        if not isinstance(b, dict):
            continue
        kind = b.get("type")
        if kind == "text" and isinstance(b.get("text"), str) and not _reinjected(b["text"]):
            texts.append(b["text"])
        elif kind == "tool_use":
            held.extend(_strings(b.get("input"), [], whole))
        elif kind == "tool_result":
            has_result = True
            results.append(_result_text(b.get("content")))
    held.extend(texts)
    if e.get("type") == "user" and not has_result and texts:
        joined = "\n".join(texts)
        if _typed(joined):
            typed = joined
    return held, results, typed


def _main_chain(e) -> bool:
    return isinstance(e, dict) and e.get("type") in ("user", "assistant") and not e.get("isSidechain")


CHUNK = 4 * 1024 * 1024


def _regular(path) -> bool:
    """A regular file (a FIFO with no writer would block open(), as in soma_ctx)."""
    try:
        return isinstance(path, str) and stat.S_ISREG(os.stat(path).st_mode)
    except OSError:
        return False


def _read_lines(path, need: int = 2) -> list:
    """Complete lines (bytes) from the transcript's end, read backwards in chunks, stopping
    once `need` compact boundaries are in hand or READ_MAX_BYTES have been read. Each chunk
    is split once; only its complete lines are searched, so the cost stays linear."""
    if not _regular(path):
        return []
    blocks, carry, found = [], b"", 0
    with open(path, "rb") as f:
        size = f.seek(0, os.SEEK_END)
        pos = size
        while pos > 0 and size - pos < READ_MAX_BYTES:
            step = min(CHUNK, pos, READ_MAX_BYTES - (size - pos))
            pos -= step
            f.seek(pos)
            parts = (f.read(step) + carry).split(b"\n")
            carry, block = parts[0], parts[1:]  # the first piece may be a partial line
            blocks.append(block)
            if any(b"compact_boundary" in ln for ln in block):
                found += sum(1 for ln in block if _boundary(ln) is not None)
                if found >= need:
                    break
    if pos == 0:
        blocks.append([carry])  # the file's first line is complete
    return [ln for block in reversed(blocks) for ln in block if ln.strip()]


def _boundary(line: bytes):
    if b"compact_boundary" not in line:
        return None
    try:
        d = json.loads(line)
    except Exception:
        return None
    if isinstance(d, dict) and d.get("type") == "system" and d.get("subtype") == "compact_boundary":
        return d
    return None


def _boundary_epoch(b: dict) -> float | None:
    """The boundary's time; None when it is missing, unparseable or naive (no zone)."""
    try:
        t = datetime.fromisoformat(str(b.get("timestamp")).replace("Z", "+00:00"))
        return t.timestamp() if t.tzinfo is not None else None
    except Exception:
        return None


def _boundaries(lines: list) -> list:
    """[(line index, boundary entry)] in file order."""
    return [(i, b) for i, ln in enumerate(lines) if (b := _boundary(ln)) is not None]


def _index_lines(lines) -> dict:
    idx = new_index()
    users = []
    for ln in lines:
        try:
            e = json.loads(ln)
        except Exception:
            continue
        if not _main_chain(e):
            continue
        whole = []
        held, results, typed = entry_texts(e, whole)
        for p in whole:
            _bump(idx["paths"], p)
        for t in held:
            index_text(idx, t)
        for t in results:
            for m in AGENT_RE.findall(t):
                _bump(idx["agents"], m)
        if typed is not None:
            users.append(typed[:USER_MSG_CAP])
    idx["user"] = users[-USER_MSGS_MAX:]
    return idx


def build_index(path, before_boundary_at: int | None = None, lines: list | None = None) -> dict:
    """Index the span after the last compact boundary (or from the start of the bounded read)
    up to EOF; with before_boundary_at, the span between the previous boundary and that one."""
    if lines is None:
        lines = _read_lines(path, 1 if before_boundary_at is None else 2)
    bounds = [i for i, _ in _boundaries(lines)]
    if before_boundary_at is not None:
        prev = [i for i in bounds if i < before_boundary_at]
        return _index_lines(lines[(prev[-1] + 1 if prev else 0):before_boundary_at])
    return _index_lines(lines[(bounds[-1] + 1 if bounds else 0):])


# --- files --------------------------------------------------------------------

def _dir(sdir) -> str:
    return os.path.join(state_dir(sdir), SUBDIR)


def _atomic(path: str, data: str, mode: int = 0o600) -> bool:
    tmp = f"{os.path.dirname(path)}/.{os.path.basename(path)}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def _read_json(path: str, cap: int = 65536):
    try:
        with open(path, "rb") as f:
            raw = f.read(cap + 1)
        if len(raw) > cap:
            return None
        doc = json.loads(raw)
        return doc if isinstance(doc, dict) else None
    except Exception:
        return None


def _prune(d: str, now: float) -> None:
    """State, pre, index and temp files older than 3 days, at most hourly (soma_ctx's scheme)."""
    marker = os.path.join(d, ".pruned")
    try:
        if 0 <= now - os.stat(marker).st_mtime < PRUNE_EVERY_S:
            return
    except OSError:
        pass
    try:
        with os.scandir(d) as it:
            for e in it:
                if e.name.endswith((".json", ".md", ".tmp", ".claim")) and now - e.stat().st_mtime > PRUNE_AGE_S:
                    os.unlink(e.path)
        os.close(os.open(marker, os.O_WRONLY | os.O_CREAT, 0o600))
        os.utime(marker)
    except OSError:
        return


def read_state(sid, sdir=None) -> dict | None:
    s = safe_id(sid)
    return _read_json(os.path.join(_dir(sdir), s + ".json")) if s else None


# --- hook events --------------------------------------------------------------

def pre_compact(sid: str, path: str, sdir=None, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    if not _regular(path):
        return False
    idx = build_index(path)
    doc = {"ts": now, "tokens": transcript_tokens(path), "index": idx}
    d = _dir(sdir)
    ok = _atomic(os.path.join(d, sid + ".pre.json"), json.dumps(doc))
    _prune(d, now)
    return ok


def _preserved_text(lines: list, uuids) -> str:
    want = {u for u in uuids if isinstance(u, str)} if isinstance(uuids, list) else set()
    if not want:
        return ""
    out = []
    for ln in lines:
        m = re.search(rb'"uuid":\s*"([^"]+)"', ln)
        if not m or m.group(1).decode("utf-8", "replace") not in want:
            continue
        try:
            e = json.loads(ln)
        except Exception:
            continue
        if isinstance(e, dict):
            whole = []
            held, results, _ = entry_texts(e, whole)
            out.extend(whole + held + results)
    return "\n".join(out)


_TAIL = r"(?![\w-]|[./]\w)"   # not continued into a longer name or path


def _occurs(token: str, text: str, lead: str) -> bool:
    """token occurs literally in text as a whole token (substring test first, it is cheap)."""
    return token in text and re.search(lead + re.escape(token) + _TAIL, text) is not None


URL_END = r"(?=$|[\s\"'<>()\[\]{}`]|[.,;:!?]+(?:$|[\s\"'<>()\[\]{}`]))"


def _file_name(name: str) -> bool:
    """A file's last segment: it has a dot that is not only a leading one (.bashrc is not)."""
    return "." in name.lstrip(".")


def _kept(cls: str, ident: str, kept: str, shared: frozenset = frozenset()) -> bool:
    if cls == "paths":
        # the full path, or the basename as a token when it is a file's and no other indexed
        # path shares it (also inside a relative mention, dir/name.py)
        if _occurs(ident, kept, r"(?<![\w.~/-])"):
            return True
        name = os.path.basename(ident)
        return _file_name(name) and name not in shared and _occurs(name, kept, r"(?<![\w.-])")
    if cls == "ids":
        return re.search(re.escape(ident) + r"(?!\d)", kept) is not None if ident in kept else False
    if cls == "urls":  # a whole token: a longer URL it is a prefix of does not keep it
        return ident in kept and re.search(r"(?<![^\s\"'<>()\[\]{}`])" + re.escape(ident) + URL_END, kept) is not None
    return ident in kept  # a short hash kept as part of the full one counts as kept


def dropped_of(idx: dict, kept: str) -> dict:
    """{class: {identifier: count}} of idx's identifiers that kept does not keep."""
    names = {}
    for p in (idx.get("paths") or {}):
        if isinstance(p, str):
            _bump(names, os.path.basename(p))
    shared = frozenset(n for n, c in names.items() if c > 1)
    return {c: {k: n for k, n in (idx.get(c) or {}).items()
                if isinstance(k, str) and isinstance(n, int) and not _kept(c, k, kept, shared)} for c in CLASSES}


def _fresh_boundary(lines: list, now: float, since: float | None = None, strict: bool = False):
    """(line index, entry) of the last boundary when it is this compaction's, else None.
    It is when its time is within FRESH_BOUNDARY_S of now and not older than the start of
    this compaction as Soma knows it (`since`: the pre index's ts less slack, or, strictly
    newer, the previous notice's ts); with no start known, the window alone decides."""
    bounds = _boundaries(lines)
    if bounds:
        i, b = bounds[-1]
        t = _boundary_epoch(b)
        if t is None or abs(now - t) > FRESH_BOUNDARY_S:
            return None
        if since is not None and (t <= since if strict else t < since):
            return None
        return i, b
    return None


PRE_SLACK_S = 5


def _compaction_start(pre, prev) -> tuple:
    """(since, strict) for _fresh_boundary: the pre index's ts minus slack, else the previous
    notice's ts (a boundary must be strictly newer), else (None, False)."""
    if pre:
        return pre["ts"] - PRE_SLACK_S, False
    if prev and isinstance(prev.get("ts"), (int, float)) and not isinstance(prev.get("ts"), bool):
        return float(prev["ts"]), True
    return None, False


def _meta_tokens(b) -> tuple:
    meta = b.get("compactMetadata") if isinstance(b, dict) else None
    meta = meta if isinstance(meta, dict) else {}
    ok = lambda v: v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 1e12 else None  # noqa: E731
    return ok(meta.get("preTokens")), ok(meta.get("postTokens")), meta


def _render_index(dropped: dict, users: list, now: float, trigger) -> str:
    out = [f"# Compaction {time.strftime('%Y-%m-%d %H:%M', time.localtime(now))} ({trigger or 'unknown'})", "",
           "Identifiers held before the compaction that occur neither in the summary nor in a message "
           "the harness kept. Identifiers only; the reasoning around them is not recoverable here.", ""]
    for c in CLASSES:
        items = sorted(dropped[c].items(), key=lambda kv: (-kv[1], kv[0]))
        if not items:
            continue
        note = f" ({len(items)} dropped, {CLASS_CAP} listed)" if len(items) > CLASS_CAP else f" ({len(items)})"
        out += [f"## {c}{note}", ""] + [f"- {k} ({n}x)" if n > 1 else f"- {k}" for k, n in items[:CLASS_CAP]] + [""]
    out += ["## User messages, verbatim", ""]
    for u in users:
        out += ["> " + u.replace("\n", "\n> "), ""]
    return "\n".join(out)


def post_compact(sid: str, path, summary, trigger=None, sdir=None, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    d = _dir(sdir)
    pre_path = os.path.join(d, sid + ".pre.json")
    pre = _read_json(pre_path, PRE_MAX_BYTES)
    if pre and not (isinstance(pre.get("ts"), (int, float)) and 0 <= now - pre["ts"] <= PRE_MAX_AGE_S
                    and isinstance(pre.get("index"), dict)):
        pre = None
    try:
        lines = _read_lines(path) if isinstance(path, str) else []
    except Exception:
        lines = []
    prev = read_state(sid, sdir)
    since, strict = _compaction_start(pre, prev)
    fresh = _fresh_boundary(lines, now, since, strict)
    if pre:
        idx = pre["index"]
    elif lines:
        idx = build_index(None, fresh[0] if fresh else None, lines)
    else:
        return False
    pre_tok, post_tok, meta = _meta_tokens(fresh[1]) if fresh else (None, None, {})
    if pre_tok is None and pre and isinstance(pre.get("tokens"), int):
        pre_tok = pre["tokens"]
    preserved = meta.get("preservedMessages") if isinstance(meta.get("preservedMessages"), dict) else {}
    kept = (summary if isinstance(summary, str) else "") + "\n" + _preserved_text(lines, preserved.get("uuids"))
    dropped = dropped_of(idx, kept)
    users = [u for u in idx.get("user") or [] if isinstance(u, str)][-USER_MSGS_MAX:]
    index_file = os.path.join(d, f"{sid}-{int(now)}.md")
    if not _atomic(index_file, _render_index(dropped, users, now, trigger)):
        return False
    count = (prev.get("count") if prev and isinstance(prev.get("count"), int) else 0) + 1
    doc = {"ts": now, "trigger": trigger if isinstance(trigger, str) else None, "count": count,
           "pre_tokens": pre_tok, "post_tokens": post_tok, "dropped": {c: len(dropped[c]) for c in CLASSES},
           "index_file": index_file, "transcript": path if isinstance(path, str) else None, "announced": False,
           "since": since, "strict": strict}
    ok = _atomic(os.path.join(d, sid + ".json"), json.dumps(doc))
    try:
        os.unlink(pre_path)
    except OSError:
        pass
    _prune(d, now)
    return ok


def handle(payload, sdir=None, now: float | None = None) -> bool:
    """Dispatch one PreCompact/PostCompact payload. True when a file was written; never raises."""
    try:
        if not enabled() or not isinstance(payload, dict):
            return False
        sid = safe_id(payload.get("session_id"))
        path = payload.get("transcript_path")
        if not sid or not isinstance(path, str):
            return False
        event = payload.get("hook_event_name")
        if event == "PreCompact":
            return pre_compact(sid, path, sdir, now)
        if event == "PostCompact":
            return post_compact(sid, path, payload.get("compact_summary"), payload.get("trigger"), sdir, now)
        return False
    except Exception:
        return False


# --- announce -----------------------------------------------------------------

def _retry_tokens(doc: dict) -> None:
    """post_tokens still unknown: look once at the transcript tail for this compaction's boundary."""
    path = doc.get("transcript")
    if not _regular(path):
        return
    try:
        with open(path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - TAIL_MAX_BYTES))
            lines = f.read(TAIL_MAX_BYTES).split(b"\n")
    except Exception:
        return
    since = doc.get("since") if isinstance(doc.get("since"), (int, float)) else None
    fresh = _fresh_boundary(lines, doc["ts"], since, doc.get("strict") is True)
    if fresh:
        pre_tok, post_tok, _ = _meta_tokens(fresh[1])
        doc["post_tokens"] = post_tok
        if pre_tok is not None:
            doc["pre_tokens"] = pre_tok


def segment(doc: dict) -> str:
    gen = f"×{doc['count']} " if isinstance(doc.get("count"), int) and doc["count"] > 1 else ""
    out = f"compacted {gen}{time.strftime('%H:%M', time.localtime(doc['ts']))}"
    pre, post = doc.get("pre_tokens"), doc.get("post_tokens")
    if isinstance(pre, int) and isinstance(post, int):
        out += f" ({_k(pre)}→{_k(post)})"
    dropped = doc.get("dropped") if isinstance(doc.get("dropped"), dict) else {}
    parts = [f"{dropped[c]} {c if dropped[c] != 1 else SINGULAR[c]}" for c in CLASSES if isinstance(dropped.get(c), int) and dropped[c] > 0]
    if parts:
        return out + " · dropped: " + ", ".join(parts) + f" → {doc.get('index_file')}"
    return out + " · nothing dropped"


def take_notice(sid, sdir=None, now: float | None = None) -> str | None:
    """This session's pending compaction notice as a line segment, marked announced on the way;
    None when there is none, it is older than 24 h, or the mark could not be written."""
    try:
        if not enabled():
            return None
        s = safe_id(sid)
        doc = read_state(s, sdir) if s else None
        if not doc or doc.get("announced") is not False or not isinstance(doc.get("ts"), (int, float)):
            return None
        now = time.time() if now is None else now
        if now - doc["ts"] > NOTICE_MAX_AGE_S or doc["ts"] - now > 300:
            return None
        # one caller wins: an exclusive claim per session, compaction count and notice time
        claim = os.path.join(_dir(sdir), f"{s}.{doc.get('count')}.{int(doc['ts'] * 1000)}.claim")
        try:
            os.close(os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        except OSError:
            return None
        if doc.get("post_tokens") is None:
            _retry_tokens(doc)
        seg = segment(doc)
        doc["announced"] = True
        if _atomic(os.path.join(_dir(sdir), s + ".json"), json.dumps(doc)):
            return seg
        try:
            os.unlink(claim)  # the mark did not land: a later caller may try again
        except OSError:
            pass
        return None
    except Exception:
        return None
