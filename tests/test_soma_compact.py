"""Soma 0.11.0: compaction awareness. Synthetic transcripts only."""
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS))
import soma_compact  # noqa: E402
import soma_lib  # noqa: E402

SID = "sess-1"


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))


def user(text, **kw):
    return {"type": "user", "uuid": kw.pop("uuid", None) or f"u{id(text)}", "isSidechain": False,
            "message": {"role": "user", "content": text}, **kw}


def asst(blocks, **kw):
    return {"type": "assistant", "uuid": kw.pop("uuid", None) or f"a{id(blocks)}", "isSidechain": False,
            "message": {"role": "assistant", "content": blocks}, **kw}


def result(text, **kw):
    return user([{"type": "tool_result", "tool_use_id": "t", "content": text}], **kw)


def boundary(t, pre=866000, post=38000, uuids=()):
    return {"type": "system", "subtype": "compact_boundary", "timestamp": _iso(t), "uuid": f"b{t}",
            "compactMetadata": {"trigger": "auto", "preTokens": pre, "postTokens": post,
                                "preservedMessages": {"uuids": list(uuids)}}}


def write(path, entries):
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return str(path)


def entries_basic():
    return [
        user("please fix /root/work/soma/hooks/soma_lib.py and see #7218"),
        asst([{"type": "text", "text": "commit eabf4e4 at https://github.com/x/y/pull/3 ok"},
              {"type": "tool_use", "name": "Bash", "input": {"command": "cat /etc/postfix/main.cf",
                                                             "nested": {"list": ["/var/log/a.log"]}}}]),
        result("/huge/listing/file1\n/huge/listing/file2 deadbee1 agentId: a1b2c3d4e5"),
    ]


def test_extraction_classes_and_guards():
    idx = soma_compact.new_index()
    soma_compact.index_text(idx, "see /a/b, 1234567 and decade cafe0123 #12 #123 #12345678 "
                                 "https://ex.com/p/q/r.html /lonely")
    assert set(idx["paths"]) == {"/a/b"}  # /lonely has one segment, the URL's path is not a path
    assert set(idx["hashes"]) == {"cafe0123"}  # pure digits and all-letter hex words are not hashes
    assert set(idx["ids"]) == {"#123"}
    assert set(idx["urls"]) == {"https://ex.com/p/q/r.html"}


def test_tool_results_ignored_except_agent_ids_and_sidechain_ignored(tmp_path):
    side = user("/side/chain/path", isSidechain=True)
    p = write(tmp_path / "t.jsonl", entries_basic() + [side])
    idx = soma_compact.build_index(p)
    assert "/huge/listing/file1" not in idx["paths"] and "deadbee1" not in idx["hashes"]
    assert set(idx["agents"]) == {"a1b2c3d4e5"}
    assert "/side/chain/path" not in idx["paths"]
    assert {"/root/work/soma/hooks/soma_lib.py", "/etc/postfix/main.cf", "/var/log/a.log"} <= set(idx["paths"])
    assert idx["user"] == ["please fix /root/work/soma/hooks/soma_lib.py and see #7218"]


def test_only_span_after_previous_boundary(tmp_path):
    p = write(tmp_path / "t.jsonl", [user("old /old/path/x"), boundary(time.time() - 7200),
                                     user("new /new/path/y")])
    idx = soma_compact.build_index(p)
    assert set(idx["paths"]) == {"/new/path/y"}


def test_user_messages_skip_meta_reminders_and_results(tmp_path):
    p = write(tmp_path / "t.jsonl", [user("<system-reminder>x</system-reminder>"), user("meta", isMeta=True),
                                     user([{"type": "text", "text": "<task-notification>"}]),
                                     user([{"type": "text", "text": "typed words"}]), result("r")])
    assert soma_compact.build_index(p)["user"] == ["typed words"]


def _pre_post(tmp_path, entries_after=(), summary="", uuids=(), now=None):
    sdir = str(tmp_path / "state")
    now = now or time.time()
    p = write(tmp_path / "t.jsonl", entries_basic())
    assert soma_compact.handle({"hook_event_name": "PreCompact", "session_id": SID, "transcript_path": p,
                                "trigger": "auto"}, sdir, now)
    assert (tmp_path / "state" / "soma-compact" / f"{SID}.pre.json").exists()
    write(p, entries_basic() + [boundary(now, uuids=uuids)] + list(entries_after))
    assert soma_compact.handle({"hook_event_name": "PostCompact", "session_id": SID, "transcript_path": p,
                                "trigger": "auto", "compact_summary": summary}, sdir, now + 1)
    return sdir, soma_compact.read_state(SID, sdir)


def test_pre_then_post_happy_path(tmp_path):
    sdir, st = _pre_post(tmp_path, summary="we edited soma_lib.py; see https://github.com/x/y/pull/3")
    assert not (tmp_path / "state" / "soma-compact" / f"{SID}.pre.json").exists()
    assert st["announced"] is False and st["count"] == 1 and st["trigger"] == "auto"
    assert st["pre_tokens"] == 866000 and st["post_tokens"] == 38000
    d = st["dropped"]
    assert d["paths"] == 2 and d["hashes"] == 1 and d["ids"] == 1 and d["urls"] == 0 and d["agents"] == 1
    md = Path(st["index_file"]).read_text()
    assert "/etc/postfix/main.cf" in md and "soma_lib.py" not in md.split("User messages")[0]
    assert "please fix /root/work/soma" in md.split("User messages, verbatim")[1]
    assert stat.S_IMODE(os.stat(st["index_file"]).st_mode) == 0o600


def test_kept_via_preserved_message(tmp_path):
    kept = asst([{"type": "text", "text": "still holding /etc/postfix/main.cf"}], uuid="keep-1")
    sdir, st = _pre_post(tmp_path, entries_after=[kept], uuids=["keep-1"])
    assert "/etc/postfix/main.cf" not in Path(st["index_file"]).read_text()


@pytest.mark.parametrize("with_boundary", [True, False])
def test_post_without_pre(tmp_path, with_boundary):
    sdir = str(tmp_path / "state")
    now = time.time()
    ents = entries_basic() + ([boundary(now)] if with_boundary else [])
    p = write(tmp_path / "t.jsonl", ents)
    assert soma_compact.handle({"hook_event_name": "PostCompact", "session_id": SID, "transcript_path": p,
                                "compact_summary": ""}, sdir, now)
    st = soma_compact.read_state(SID, sdir)
    assert st["dropped"]["paths"] == 3
    assert st["post_tokens"] == (38000 if with_boundary else None)


def test_second_compaction_counts_generation(tmp_path):
    sdir, st = _pre_post(tmp_path)
    seg = soma_compact.take_notice(SID, sdir, time.time())
    assert seg.startswith("compacted ") and "×" not in seg
    p = st["transcript"]
    later = time.time() + 60
    with open(p, "a") as f:
        f.write(json.dumps(user("more /x/y/z")) + "\n")
    soma_compact.handle({"hook_event_name": "PostCompact", "session_id": SID, "transcript_path": p,
                         "compact_summary": ""}, sdir, later)
    assert soma_compact.take_notice(SID, sdir, later).startswith("compacted ×2 ")


def test_notice_format_once_and_stale(tmp_path):
    sdir, st = _pre_post(tmp_path, summary="")
    seg = soma_compact.take_notice(SID, sdir, time.time())
    hm = time.strftime("%H:%M", time.localtime(st["ts"]))
    assert seg == (f"compacted {hm} (866k→38k) · dropped: 3 paths, 1 hash, 1 id, 1 url, 1 agent → "
                   f"{st['index_file']}")
    assert soma_compact.take_notice(SID, sdir, time.time()) is None
    (tmp_path / "b").mkdir()
    sdir2, _ = _pre_post(tmp_path / "b")
    assert soma_compact.take_notice(SID, sdir2, time.time() + 25 * 3600) is None


def test_nothing_dropped_and_unknown_tokens(tmp_path):
    sdir = str(tmp_path / "state")
    p = write(tmp_path / "t.jsonl", [user("hello")])
    soma_compact.handle({"hook_event_name": "PostCompact", "session_id": SID, "transcript_path": p}, sdir)
    seg = soma_compact.take_notice(SID, sdir)
    assert seg == f"compacted {time.strftime('%H:%M')} · nothing dropped"


def test_post_tokens_retried_at_announce(tmp_path):
    sdir = str(tmp_path / "state")
    now = time.time()
    p = write(tmp_path / "t.jsonl", entries_basic())
    soma_compact.handle({"hook_event_name": "PostCompact", "session_id": SID, "transcript_path": p}, sdir, now)
    write(p, entries_basic() + [boundary(now, pre=500000, post=20000)])
    assert "(500k→20k)" in soma_compact.take_notice(SID, sdir, now + 5)


def test_off_switch(tmp_path, monkeypatch):
    sdir, _ = _pre_post(tmp_path)
    monkeypatch.setenv("SOMA_COMPACT", "0")
    assert soma_compact.take_notice(SID, sdir) is None
    assert not soma_compact.handle({"hook_event_name": "PostCompact", "session_id": "s2",
                                    "transcript_path": "/nonexistent"}, sdir)


def test_caps(tmp_path):
    sdir = str(tmp_path / "state")
    many = [user(" ".join(f"/p/{i}x" for i in range(600)))] + [user(f"m{i} " + "w" * 3000) for i in range(210)]
    p = write(tmp_path / "t.jsonl", many)
    soma_compact.handle({"hook_event_name": "PostCompact", "session_id": SID, "transcript_path": p}, sdir)
    md = Path(soma_compact.read_state(SID, sdir)["index_file"]).read_text()
    assert "600 dropped, 500 listed" in md
    verb = md.split("User messages, verbatim")[1]
    assert "m209 " in verb and "m9 " not in verb and "m10 " in verb  # newest 200 kept
    assert "w" * 2001 not in verb


def test_pruning(tmp_path):
    sdir = str(tmp_path / "state")
    d = tmp_path / "state" / "soma-compact"
    d.mkdir(parents=True)
    old = time.time() - 4 * 86400
    for n in ("old.json", "old.pre.json", "old-1.md"):
        (d / n).write_text("{}")
        os.utime(d / n, (old, old))
    p = write(tmp_path / "t.jsonl", [user("/a/b")])
    soma_compact.handle({"hook_event_name": "PostCompact", "session_id": SID, "transcript_path": p}, sdir)
    assert sorted(x.name for x in d.iterdir() if not x.name.startswith(".")) == \
        sorted([f"{SID}.json", Path(soma_compact.read_state(SID, sdir)["index_file"]).name])


def test_large_transcript_inside_bound(tmp_path):
    p = tmp_path / "big.jsonl"
    filler = json.dumps(asst([{"type": "text", "text": "x /a/b/c " + "y" * 5000}]))
    with open(p, "w") as f:
        f.write(json.dumps(user("/first/line/path")) + "\n")
        while f.tell() < 50 * 1024 * 1024:
            f.write(filler + "\n")
        f.write(json.dumps(user("/last/line/path")) + "\n")
    t0 = time.monotonic()
    idx = soma_compact.build_index(str(p))
    took = time.monotonic() - t0
    print(f"50MB index: {took:.2f}s")
    assert "/last/line/path" in idx["paths"] and took < 10


# --- the command ---------------------------------------------------------------

@pytest.mark.parametrize("stdin", ["", "{", "[1,2]", "null", "\"x\"",
                                   json.dumps({"hook_event_name": "PreCompact", "session_id": "../../etc/x",
                                               "transcript_path": "/nonexistent"}),
                                   json.dumps({"hook_event_name": "PostCompact", "session_id": "s",
                                               "transcript_path": 5, "compact_summary": ["x"]}),
                                   "[" * 100000])
def test_command_silent_exit_zero(tmp_path, stdin):
    env = {**os.environ, "SOMA_STATE_DIR": str(tmp_path / "st")}
    r = subprocess.run([sys.executable, str(HOOKS / "soma-compact.py")], input=stdin, text=True,
                       capture_output=True, env=env, timeout=20)
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    assert not (tmp_path / "etc").exists()


def test_command_unwritable_state_dir(tmp_path):
    p = write(tmp_path / "t.jsonl", entries_basic())
    blocker = tmp_path / "file"
    blocker.write_text("")
    env = {**os.environ, "SOMA_STATE_DIR": str(blocker)}
    r = subprocess.run([sys.executable, str(HOOKS / "soma-compact.py")], text=True, capture_output=True,
                       input=json.dumps({"hook_event_name": "PostCompact", "session_id": SID,
                                         "transcript_path": p}), env=env, timeout=20)
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""


def test_kept_rule_whole_tokens():
    k = soma_compact._kept
    summary = "edited hooks/soma_lib.py. Also /a/bc and #12345 and eabf4e4ffff."
    assert k("paths", "/root/work/soma/hooks/soma_lib.py", summary)  # basename rule
    assert not k("paths", "/etc/x/b", summary)  # a one-letter basename inside a word is not kept
    assert not k("paths", "/a/b", summary)  # the prefix of a longer path is not kept
    assert k("paths", "/a/bc", summary)
    assert not k("ids", "#1234", summary) and k("ids", "#12345", summary)
    assert k("hashes", "eabf4e4", summary)


def test_read_is_bounded_and_stops_at_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(soma_compact, "CHUNK", 4096)
    monkeypatch.setattr(soma_compact, "READ_MAX_BYTES", 64 * 1024)
    pad = [user("w" * 1000) for _ in range(100)]
    p = write(tmp_path / "t.jsonl", [user("/beyond/the/bound")] + pad + [user("/inside/the/bound")])
    idx = soma_compact.build_index(p)
    assert "/inside/the/bound" in idx["paths"] and "/beyond/the/bound" not in idx["paths"]
    reads = []
    real = soma_compact._boundary
    monkeypatch.setattr(soma_compact, "_boundary", lambda ln: reads.append(1) or real(ln))
    p = write(tmp_path / "u.jsonl", pad + [boundary(time.time())] + [user("/after/b/x")])
    lines = soma_compact._read_lines(p, need=1)
    assert len(lines) < 10  # stopped at the first chunk that held the boundary


def test_system_reminders_and_meta_not_indexed(tmp_path):
    p = write(tmp_path / "t.jsonl", [user("<system-reminder>/root/CLAUDE.md/x</system-reminder>"),
                                     user("/meta/only/path", isMeta=True),
                                     user([{"type": "text", "text": "<system-reminder>/r/e/m</system-reminder>"},
                                           {"type": "text", "text": "typed /real/path/z"}])])
    assert set(soma_compact.build_index(p)["paths"]) == {"/real/path/z"}


# --- 0.11.0 pre-release fix round ---------------------------------------------

def _two_spans(now, b1_age=600):
    span1 = [user("first span /one/a/x.py"), asst([{"type": "text", "text": "abc1234f #111"}], uuid="K1")]
    span2 = [user("second span /two/b/y.py"), asst([{"type": "text", "text": "fedc4321 #222"}])]
    return span1 + [boundary(now - b1_age, uuids=["K1"])] + span2


def _post(sdir, p, now, summary="summary"):
    assert soma_compact.handle({"hook_event_name": "PostCompact", "session_id": SID, "transcript_path": p,
                                "compact_summary": summary}, sdir, now)
    st = soma_compact.read_state(SID, sdir)
    return st, Path(st["index_file"]).read_text().split("## User")[0]


def test_quick_second_compaction_with_pre_uses_span_two_and_no_old_tokens(tmp_path):
    sdir, now = str(tmp_path / "state"), time.time()
    p = write(tmp_path / "t.jsonl", _two_spans(now))
    assert soma_compact.handle({"hook_event_name": "PreCompact", "session_id": SID, "transcript_path": p}, sdir, now - 5)
    st, md = _post(sdir, p, now, summary="")
    assert "/two/b/y.py" in md and "fedc4321" in md and "#222" in md and "/one/a/x.py" not in md
    assert st["post_tokens"] is None and st["pre_tokens"] != 866000


def test_quick_second_compaction_without_pre_uses_previous_state(tmp_path):
    sdir, now = str(tmp_path / "state"), time.time()
    p = write(tmp_path / "t.jsonl", _two_spans(now)[:2] + [boundary(now - 600, uuids=["K1"])])
    _post(sdir, p, now - 599)           # compaction 1, handled by Soma 10 min ago
    write(p, _two_spans(now))
    st, md = _post(sdir, p, now)        # compaction 2, its boundary not yet written
    assert "/two/b/y.py" in md and "#222" in md and "/one/a/x.py" not in md
    assert st["count"] == 2 and st["post_tokens"] is None and st["pre_tokens"] is None


def test_boundary_written_before_post_with_pre_is_fresh(tmp_path):
    sdir, now = str(tmp_path / "state"), time.time()
    p = write(tmp_path / "t.jsonl", _two_spans(now))
    assert soma_compact.handle({"hook_event_name": "PreCompact", "session_id": SID, "transcript_path": p}, sdir, now - 5)
    write(p, _two_spans(now) + [boundary(now - 2, pre=500000, post=20000, uuids=["K2"]),
                                asst([{"type": "text", "text": "kept /two/b/y.py"}], uuid="K2")])
    st, md = _post(sdir, p, now, summary="")
    assert st["pre_tokens"] == 500000 and st["post_tokens"] == 20000 and "/two/b/y.py" not in md


def test_first_compaction_without_pre_uses_window(tmp_path):
    sdir, now = str(tmp_path / "state"), time.time()
    p = write(tmp_path / "t.jsonl", _two_spans(now, b1_age=14 * 60))
    st, md = _post(sdir, p, now)
    assert "/one/a/x.py" in md and "/two/b/y.py" not in md and st["post_tokens"] == 38000


@pytest.mark.parametrize("ts", ["2026-10-06T10:00:00", "not a time", None])
def test_naive_or_bad_boundary_timestamp_not_fresh(tmp_path, ts):
    sdir, now = str(tmp_path / "state"), time.time()
    ents = _two_spans(now)
    b = boundary(now - 60)
    b["timestamp"] = ts
    ents[2] = b
    p = write(tmp_path / "t.jsonl", ents)
    st, md = _post(sdir, p, now)
    assert "/two/b/y.py" in md and "/one/a/x.py" not in md and st["post_tokens"] is None


def _dropped(paths_text, summary):
    idx = soma_compact.new_index()
    soma_compact.index_text(idx, paths_text)
    return soma_compact.dropped_of(idx, summary)


def test_basename_rule_only_for_unique_files():
    many = " ".join(f"/var/www/site{i}/index.php" for i in range(20))
    assert len(_dropped(many, "we edited index.php and the tests")["paths"]) == 20
    assert not _dropped("/root/work/soma/hooks/soma_ctx.py /a/b/c.txt", "touched soma_ctx.py")["paths"].get(
        "/root/work/soma/hooks/soma_ctx.py", 0)
    assert "/root/work/soma/tests" in _dropped("/root/work/soma/tests", "ran the tests")["paths"]
    assert "/home/u/.bashrc" in _dropped("/home/u/.bashrc", "edited .bashrc")["paths"]
    assert not _dropped("/root/work/soma/tests", "ran /root/work/soma/tests")["paths"]


def test_uuid_and_hex_runs_are_not_hashes():
    idx = soma_compact.new_index()
    soma_compact.index_text(idx, "/tmp/claude-0/28b9e108-8897-469c-985e-96b2b9f9aeea/x and "
                                 "deadbeef1-1234-cafe0 and real eabf4e4")
    assert sorted(idx["hashes"]) == ["eabf4e4"]


def test_css_colours_are_not_ids():
    idx = soma_compact.new_index()
    soma_compact.index_text(idx, "a{color:#123456} b{x: #333;}\nborder-color #654321 here\n"
                                 "see #123 and #7275 in prose; also x#1234 and &#1234; not")
    assert sorted(idx["ids"]) == ["#123", "#7275"]


def test_tool_input_path_value_taken_whole(tmp_path):
    p = write(tmp_path / "t.jsonl", [asst([{"type": "tool_use", "name": "Read", "input": {
        "file_path": "/mnt/nas/Onedrive/BRF Ängssätra/x y.pdf", "command": "ls /a/b c",
        "old_string": "// a comment / here", "new_string": "/* x */ y/z"}}])])
    idx = soma_compact.build_index(p)
    assert set(idx["paths"]) == {"/mnt/nas/Onedrive/BRF Ängssätra/x y.pdf", "/a/b"}
    assert not soma_compact.dropped_of(idx, "read /mnt/nas/Onedrive/BRF Ängssätra/x y.pdf")["paths"].get(
        "/mnt/nas/Onedrive/BRF Ängssätra/x y.pdf")


def test_url_kept_only_as_whole_token():
    idx = soma_compact.new_index()
    soma_compact.index_text(idx, "https://github.com/x/y and https://github.com/x/z")
    d = soma_compact.dropped_of(idx, "see https://github.com/x/y/pull/3 and (https://github.com/x/z).")
    assert list(d["urls"]) == ["https://github.com/x/y"]


def test_lookbehinds_paths_and_ids():
    idx = soma_compact.new_index()
    soma_compact.index_text(idx, "~/x/y and s/x/y/g and &#1234; and x#1234 and /real/abs/p")
    assert list(idx["paths"]) == ["/real/abs/p"] and not idx["ids"]


def test_compact_summary_and_interruptions_skipped(tmp_path):
    p = write(tmp_path / "t.jsonl", [
        user("This session is being continued /prev/sum/file.py", isCompactSummary=True),
        user("[Request interrupted by user for tool use]"),
        user([{"type": "text", "text": "[Request interrupted by user]"}]),
        user("real typed /typed/a/b")])
    idx = soma_compact.build_index(p)
    assert list(idx["paths"]) == ["/typed/a/b"] and idx["user"] == ["real typed /typed/a/b"]


def test_parallel_takers_exactly_one_wins(tmp_path):
    import multiprocessing
    sdir, now = str(tmp_path / "state"), time.time()
    _post(sdir, write(tmp_path / "t.jsonl", entries_basic()), now)
    ctx = multiprocessing.get_context("fork")
    barrier, q = ctx.Barrier(8), ctx.Queue()

    def taker():
        barrier.wait()
        q.put(soma_compact.take_notice(SID, sdir, now + 1))

    procs = [ctx.Process(target=taker) for _ in range(8)]
    for pr in procs:
        pr.start()
    for pr in procs:
        pr.join(10)
    got = [q.get(timeout=5) for _ in procs]
    assert sum(1 for g in got if g) == 1


def test_singular_counts_and_modes(tmp_path):
    sdir, now = str(tmp_path / "state"), time.time()
    p = write(tmp_path / "t.jsonl", [asst([{"type": "text", "text": "/a/b/c.py eabf4e4 #123 https://x.org/a"}]),
                                     result("agentId: abcd1234")])
    st, _ = _post(sdir, p, now, summary="")
    assert "dropped: 1 path, 1 hash, 1 id, 1 url, 1 agent →" in soma_compact.segment(st)
    d = tmp_path / "state" / "soma-compact"
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(d / ".pruned").st_mode) == 0o600


@pytest.mark.parametrize("kind", ["fifo", "dir"])
def test_non_regular_transcript_skipped(tmp_path, kind):
    p = tmp_path / "t.jsonl"
    os.mkfifo(p) if kind == "fifo" else p.mkdir()
    for ev in ("PreCompact", "PostCompact"):
        assert soma_compact.handle({"hook_event_name": ev, "session_id": SID, "transcript_path": str(p)},
                                   str(tmp_path / "state")) is False


def test_pre_index_age_rule(tmp_path):
    sdir, now = str(tmp_path / "state"), time.time()
    p = write(tmp_path / "t.jsonl", [user("old /stale/pre/only.py")])
    soma_compact.handle({"hook_event_name": "PreCompact", "session_id": SID, "transcript_path": p}, sdir,
                        now - soma_compact.PRE_MAX_AGE_S - 1)
    write(p, [user("now /now/a/b.py")])
    _, md = _post(sdir, p, now)
    assert "/now/a/b.py" in md and "/stale/pre/only.py" not in md


def test_cap_limits_listing(tmp_path):
    sdir, now = str(tmp_path / "state"), time.time()
    p = write(tmp_path / "t.jsonl", [user(" ".join(f"/cap/d/f{i}.py" for i in range(soma_compact.CLASS_CAP + 7)))])
    st, md = _post(sdir, p, now, summary="")
    assert st["dropped"]["paths"] == soma_compact.CLASS_CAP + 7
    assert md.count("\n- /cap/d/") == soma_compact.CLASS_CAP


def test_tokens_shown_only_when_both_known():
    base = {"ts": time.time(), "count": 1, "dropped": {}, "index_file": "x"}
    assert "→" not in soma_compact.segment({**base, "pre_tokens": 866000, "post_tokens": None})
    assert "→" not in soma_compact.segment({**base, "pre_tokens": None, "post_tokens": 38000})
    assert "(866k→38k)" in soma_compact.segment({**base, "pre_tokens": 866000, "post_tokens": 38000})
