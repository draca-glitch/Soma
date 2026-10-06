"""Host preconditions (0.12.0 part C): stale network mounts, package manager busy, reboot pending."""
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS))
import soma_host  # noqa: E402
import soma_lib  # noqa: E402

MOUNTS = """\
/dev/md2 / ext4 rw,relatime 0 0
tmpfs /run tmpfs rw 0 0
//192.168.1.5/public /mnt/nas cifs ro,relatime 0 0
srv:/export /mnt/nfs nfs4 rw 0 0
me@box:/ /mnt/my\\040box fuse.sshfs rw 0 0
srv:/x /mnt/old nfs rw 0 0
//h/s /mnt/s3 smb3 rw 0 0
host0 /mnt/vm 9p rw 0 0
apt-cacher /mnt/fake fuse.apt-cacher rw 0 0
"""


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k in ("SOMA_NET_MOUNTS", "SOMA_PKG", "SOMA_REBOOT", "SOMA_NET_TIMEOUT_MS", "SOMA_MODE",
              "SOMA_PULSE", "SOMA_MOUNTS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SOMA_LOG", str(tmp_path / "log.jsonl"))
    monkeypatch.setattr(soma_host, "REBOOT_FILE", str(tmp_path / "no-reboot-required"))
    monkeypatch.setattr(soma_host, "PROBE", lambda p: None)


def _never(path):
    threading.Event().wait()


def _proc(tmp_path, procs=(), mounts="", name="proc"):
    """A healthy fake /proc. procs: (pid, ppid, comm[, state[, cmdline]])."""
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    (root / "meminfo").write_text("MemTotal: 64000000 kB\nMemAvailable: 40000000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n")
    (root / "loadavg").write_text("0.50 0.40 0.30 1/100 1\n")
    if mounts:
        (root / "mounts").write_text(mounts)
    for p in procs:
        pid, ppid, comm = p[:3]
        st = p[3] if len(p) > 3 else "S"
        d = root / str(pid)
        d.mkdir()
        (d / "stat").write_text(f"{pid} ({comm}) {st} {ppid} " + " ".join(["0"] * 17) + " 100 0\n")
        (d / "statm").write_text("1000 100 10 1 0 100 0\n")
        if len(p) > 4:
            (d / "cmdline").write_bytes(p[4])
    return str(root)


def _table(procs):
    return {p[0]: {"ppid": p[1], "comm": p[2], "state": p[3] if len(p) > 3 else "S"} for p in procs}


def _line(tmp_path, proc, mode="pressure", now=1000.0, sid=None, agent=False):
    hi = {"session_id": sid} if sid else None
    if agent:
        hi["agent_id"] = "a1"
    return soma_lib.line_for_mode(mode, proc_root=proc, mounts=[], services=[], hwmon_root=str(tmp_path / "hw"),
                                  sys_root=str(tmp_path / "sys"), state_dir=str(tmp_path / "st"), now=now,
                                  hook_input=hi)


def _pulse(tmp_path, proc, now, sid="s1", agent=False, hold_s=0):
    hi = {"session_id": sid}
    if agent:
        hi["agent_id"] = "a1"
    return soma_lib.pulse_line(proc_root=proc, mounts=[], services=[], hwmon_root=str(tmp_path / "hw"),
                               sys_root=str(tmp_path / "sys"), state_dir=str(tmp_path / "st"), now=now,
                               hook_input=hi, hold_s=hold_s)


def _state(tmp_path):
    return json.loads((tmp_path / "st" / "soma-state.json").read_text())


# --- discovery -------------------------------------------------------------------------------

def test_net_mounts_from_proc_mounts(tmp_path):
    proc = _proc(tmp_path, mounts=MOUNTS)
    assert soma_host.net_mounts(proc) == ["/mnt/nas", "/mnt/nfs", "/mnt/my box", "/mnt/old", "/mnt/s3", "/mnt/vm"]


def test_net_mounts_absent_file_is_empty(tmp_path):
    assert soma_host.net_mounts(_proc(tmp_path)) == []


def test_net_mounts_override_and_off(tmp_path, monkeypatch):
    proc = _proc(tmp_path, mounts=MOUNTS)
    monkeypatch.setenv("SOMA_NET_MOUNTS", " /a, /b ,")
    assert soma_host.net_mounts(proc) == ["/a", "/b"]
    for off in ("0", "off", "false", "no"):
        monkeypatch.setenv("SOMA_NET_MOUNTS", off)
        assert soma_host.net_mounts(proc) == []


# --- stale probe, bound, back-off --------------------------------------------------------------

def test_hung_probe_is_stale_inside_the_bound():
    t = time.monotonic()
    stale, rec = soma_host.stale_mounts(["/m"], {}, 1000.0, probe=_never, timeout_s=0.2)
    assert time.monotonic() - t < 0.6
    assert stale == ["/m"] and rec == {"/m": 1000.0}


def test_healthy_and_failing_probes_are_not_stale():
    def boom(p):
        raise OSError("gone")
    assert soma_host.stale_mounts(["/a", "/b"], {}, 1000.0, probe=lambda p: None) == ([], {})
    # an error that returns (ENOENT, EACCES) is an answer: the mount is not hanging
    assert soma_host.stale_mounts(["/a"], {}, 1000.0, probe=boom) == ([], {})


def test_backoff_then_recovery():
    calls = []

    def counting(p):
        calls.append(p)
    stale, rec = soma_host.stale_mounts(["/m"], {}, 1000.0, probe=_never, timeout_s=0.05)
    assert stale == ["/m"]
    # inside 60 s the stale mount is not probed again and stays stale
    stale, rec = soma_host.stale_mounts(["/m"], rec, 1059.0, probe=counting, timeout_s=0.05)
    assert calls == [] and stale == ["/m"] and rec == {"/m": 1000.0}
    # after 60 s it is probed again; an answer clears it
    stale, rec = soma_host.stale_mounts(["/m"], rec, 1060.0, probe=counting, timeout_s=0.05)
    assert calls == ["/m"] and stale == [] and rec == {}


def test_backoff_rearms_while_still_stale():
    stale, rec = soma_host.stale_mounts(["/m"], {"/m": 1000.0}, 1070.0, probe=_never, timeout_s=0.05)
    assert stale == ["/m"] and rec == {"/m": 1070.0}


def test_clock_backwards_and_junk_record_reprobe():
    calls = []
    for rec in ({"/m": 5000.0}, {"/m": "x"}, {"/m": float("nan")}, ["/m"], "junk", None, {"/m": True}):
        calls.clear()
        stale, new = soma_host.stale_mounts(["/m"], rec, 1000.0, probe=calls.append, timeout_s=0.05)
        assert calls == ["/m"] and stale == [] and new == {}, rec


def test_mount_gone_is_dropped_from_record():
    stale, rec = soma_host.stale_mounts(["/b"], {"/a": 990.0}, 1000.0, probe=lambda p: None)
    assert stale == [] and rec == {}


def test_process_exits_promptly_with_a_probe_stuck(tmp_path):
    proc = _proc(tmp_path)
    code = f"""
import sys, threading
sys.path.insert(0, {str(HOOKS)!r})
import soma_host, soma_lib
soma_host.PROBE = lambda p: threading.Event().wait()
soma_host.REBOOT_FILE = {str(tmp_path / 'none')!r}
print(soma_lib.line_for_mode("pressure", proc_root={proc!r}, mounts=[], services=[],
      hwmon_root={str(tmp_path / 'hw')!r}, sys_root={str(tmp_path / 'sys')!r}, state_dir={str(tmp_path / 'st')!r}))
"""
    env = {"PATH": "/usr/bin:/bin", "SOMA_NET_MOUNTS": "/fake/m", "SOMA_LOG": str(tmp_path / "l.jsonl")}
    t = time.monotonic()
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10, env=env)
    wall = time.monotonic() - t
    assert r.returncode == 0, r.stderr
    assert "/fake/m STALE" in r.stdout
    assert wall < 2.0, wall


# --- stale through the hooks --------------------------------------------------------------------

def test_stale_forces_the_prompt_line_and_is_a_body_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("SOMA_NET_MOUNTS", "/mnt/nas")
    monkeypatch.setattr(soma_host, "PROBE", _never)
    monkeypatch.setenv("SOMA_NET_TIMEOUT_MS", "50")
    line = _line(tmp_path, _proc(tmp_path))
    assert line and " · /mnt/nas STALE" in line
    st = _state(tmp_path)
    assert "STALE" in st["last_flags"] and st["stale"] == {"/mnt/nas": 1000.0}


def test_stale_backoff_survives_through_state_and_pulse_recovers(tmp_path, monkeypatch):
    monkeypatch.setenv("SOMA_NET_MOUNTS", "/mnt/nas")
    monkeypatch.setenv("SOMA_NET_TIMEOUT_MS", "50")
    proc = _proc(tmp_path)
    monkeypatch.setattr(soma_host, "PROBE", _never)
    first = _pulse(tmp_path, proc, 1000.0)
    assert first and "/mnt/nas STALE" in first
    calls = []
    monkeypatch.setattr(soma_host, "PROBE", calls.append)
    assert _pulse(tmp_path, proc, 1030.0) is None  # backed off, still stale, nothing new
    assert calls == []
    rec = _pulse(tmp_path, proc, 1061.0)  # re-probed, answered: recovery (hold 0)
    assert calls == ["/mnt/nas"] and rec and "STALE" not in rec
    assert "stale" not in _state(tmp_path)


def test_stale_subagent_first_then_main_agent_hears_it(tmp_path, monkeypatch):
    monkeypatch.setenv("SOMA_NET_MOUNTS", "/mnt/nas")
    monkeypatch.setenv("SOMA_NET_TIMEOUT_MS", "50")
    monkeypatch.setattr(soma_host, "PROBE", _never)
    proc = _proc(tmp_path)
    assert _pulse(tmp_path, proc, 1000.0, agent=True) is None
    line = _pulse(tmp_path, proc, 1001.0)
    assert line and "/mnt/nas STALE" in line
    # a second session is told on its first contact, the first is not told again
    assert "/mnt/nas STALE" in _pulse(tmp_path, proc, 1002.0, sid="s2")
    assert _pulse(tmp_path, proc, 1003.0) is None


def test_stale_silent_prompt_between_holds_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("SOMA_NET_MOUNTS", "/mnt/nas")
    monkeypatch.setenv("SOMA_NET_TIMEOUT_MS", "50")
    proc = _proc(tmp_path)
    monkeypatch.setattr(soma_host, "PROBE", _never)
    assert "/mnt/nas STALE" in _pulse(tmp_path, proc, 1000.0)
    monkeypatch.setattr(soma_host, "PROBE", lambda p: None)
    assert _line(tmp_path, proc, now=1070.0, sid="s1") is None  # healthy: a silent prompt
    assert _pulse(tmp_path, proc, 1100.0, hold_s=300) is None   # absent 30 s of a 300 s hold
    assert "STALE" not in _pulse(tmp_path, proc, 1400.0, hold_s=300)


def test_old_state_file_without_stale_record(tmp_path, monkeypatch):
    (tmp_path / "st").mkdir()
    (tmp_path / "st" / "soma-state.json").write_text(json.dumps({"counters": {}, "last_flags": ["NUMB"],
                                                                 "stale": "junk"}))
    monkeypatch.setenv("SOMA_NET_MOUNTS", "/mnt/nas")
    assert _line(tmp_path, _proc(tmp_path)) is None


# --- package manager ---------------------------------------------------------------------------

@pytest.mark.parametrize("procs,want", [
    ([(5, 1, "unattended-upgr")], "unattended-upgr"),
    ([(5, 1, "apt-get"), (6, 5, "dpkg")], "apt-get,dpkg"),
    # plesk_installer is no process name: Plesk's updater runs as autoinstaller
    ([(5, 1, "apt"), (7, 1, "aptitude"), (8, 1, "plesk_installer"), (9, 1, "autoinstaller")],
     "apt,aptitude,autoinstaller"),
    ([(5, 1, "packagekitd"), (6, 5, "dpkg")], "dpkg,packagekitd"),
    ([(5, 1, "packagekitd")], None),
    ([(5, 1, "packagekitd"), (6, 5, "bash")], None),
    ([(5, 1, "apt-cacher"), (6, 1, "apt-cacher-ng"), (7, 1, "dpkg-query"), (8, 1, "aptd"), (9, 1, "xapt")], None),
    ([(5, 1, "apt", "Z")], None),
    ([], None),
])
def test_pkg_holder_by_comm(procs, want, tmp_path):
    procs = [p if p[2] != "unattended-upgr" else p[:3] + ("S", WORKER) for p in procs]
    assert soma_host.pkg_busy(_table(procs), _proc(tmp_path, procs)) == want


WORKER = b"/usr/bin/python3\0/usr/bin/unattended-upgrade\0--download-only\0"
SHUTDOWN = b"/usr/bin/python3\0/usr/share/unattended-upgrades/unattended-upgrade-shutdown\0--wait-for-signal\0"


def test_pkg_unattended_upgrade_shutdown_daemon_is_not_busy(tmp_path):
    # the always-on daemon shares the truncated comm; it holds no lock
    procs = [(1967, 1, "unattended-upgr", "S", SHUTDOWN)]
    assert soma_host.pkg_busy(_table(procs), _proc(tmp_path, procs)) is None
    # unreadable cmdline (exited mid-scan): not counted
    assert soma_host.pkg_busy(_table(procs), str(tmp_path / "nowhere")) is None
    # daemon plus a real worker: busy
    procs.append((2000, 1, "unattended-upgr", "S", WORKER))
    assert soma_host.pkg_busy(_table(procs), _proc(tmp_path, procs, name="p2")) == "unattended-upgr"


def test_pkg_off_switch_and_no_table(monkeypatch):
    assert soma_host.pkg_busy(None) is None
    monkeypatch.setenv("SOMA_PKG", "0")
    assert soma_host.pkg_busy(_table([(5, 1, "dpkg")])) is None


def test_pkg_through_the_prompt_and_the_pulse(tmp_path):
    busy = _proc(tmp_path, [(1, 0, "systemd"), (5, 1, "unattended-upgr", "S", WORKER)])
    line = _line(tmp_path, busy)
    assert line and line.endswith(" · apt busy (unattended-upgr)")
    assert "PKG" in _state(tmp_path)["last_flags"]
    assert "apt busy (unattended-upgr)" in _pulse(tmp_path, busy, 1001.0)
    assert _pulse(tmp_path, busy, 1002.0) is None
    calm = _proc(tmp_path, [(1, 0, "systemd"), (5, 1, "unattended-upgr", "S", SHUTDOWN)], name="calm")
    rec = _pulse(tmp_path, calm, 1003.0)  # hold 0: the recovery is announced, without the segment
    assert rec and "apt busy" not in rec and "PKG" not in _state(tmp_path)["last_flags"]


# --- reboot pending ---------------------------------------------------------------------------

def test_reboot_pending_only_rides_on_an_emitted_line(tmp_path, monkeypatch):
    flag = tmp_path / "reboot-required"
    flag.write_text("*** System restart required ***\n")
    monkeypatch.setattr(soma_host, "REBOOT_FILE", str(flag))
    proc = _proc(tmp_path)
    assert soma_host.reboot_pending() is True
    assert _line(tmp_path, proc) is None  # healthy box: no line for a reboot alone
    line = _line(tmp_path, proc, mode="always", now=1001.0)
    assert line.endswith(" · reboot pending")
    assert "REBOOT" not in json.dumps(_state(tmp_path)["last_flags"])
    monkeypatch.setenv("SOMA_REBOOT", "0")
    assert soma_host.reboot_pending() is False


def test_reboot_absent(tmp_path):
    assert soma_host.reboot_pending() is False
    assert "reboot" not in _line(tmp_path, _proc(tmp_path), mode="always")


# --- nothing to say: byte-identical, and without the module ----------------------------------

def test_line_byte_identical_without_the_module(tmp_path, monkeypatch):
    proc = _proc(tmp_path, [(1, 0, "systemd")], mounts=MOUNTS)
    with_mod = _line(tmp_path, proc, mode="always")
    code = f"""
import sys
sys.path.insert(0, {str(HOOKS)!r})
sys.modules["soma_host"] = None
import soma_lib
print(soma_lib.line_for_mode("always", proc_root={proc!r}, mounts=[], services=[],
      hwmon_root={str(tmp_path / 'hw')!r}, sys_root={str(tmp_path / 'sys')!r}, state_dir={str(tmp_path / 'st2')!r},
      now=1000.0))
"""
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10,
                       env={"PATH": "/usr/bin:/bin", "SOMA_LOG": str(tmp_path / "l2.jsonl")})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == with_mod
    assert "STALE" not in with_mod and "apt busy" not in with_mod


# --- 0.12.0 pre-release fixes: statvfs, one probe per source, interval, stamp first ------------

def test_probe_is_statvfs():
    import os
    assert soma_host.DEFAULT_PROBE is os.statvfs  # the autouse fixture swaps PROBE itself


def test_mount_table_carries_the_source(tmp_path):
    proc = _proc(tmp_path, mounts="//nas/public /mnt/nas cifs ro 0 0\n//nas/public /mnt/nas-rw cifs rw 0 0\n"
                                  "/dev/md2 / ext4 rw 0 0\n")
    assert soma_host.net_mount_table(proc) == [("//nas/public", "/mnt/nas"), ("//nas/public", "/mnt/nas-rw")]


def test_one_probe_per_source_applied_to_every_mount_point():
    calls = []

    def hang(p):
        calls.append(p)
        threading.Event().wait()
    entries = [("//nas/public", "/mnt/nas"), ("//nas/public", "/mnt/nas-rw"), ("srv:/x", "/mnt/x")]
    stale, rec = soma_host.stale_mounts(entries, {}, 1000.0, probe=lambda p: calls.append(p) if p == "/mnt/x" else hang(p),
                                        timeout_s=0.1)
    assert sorted(calls) == ["/mnt/nas", "/mnt/x"]
    assert stale == ["/mnt/nas", "/mnt/nas-rw"] and rec == {"//nas/public": 1000.0}


def test_no_probe_inside_the_interval_and_stamp_before_the_call():
    order, stamps = [], {}

    def probe(p):
        order.append(("probe", dict(stamps)))
        raise OSError("answer")

    def stamp(s):
        order.append(("stamp", dict(s)))
    e = [("//nas/public", "/mnt/nas")]
    soma_host.stale_mounts(e, {}, 1000.0, probe=probe, timeout_s=0.1, stamps=stamps, interval_s=30, stamp=stamp)
    assert order == [("stamp", {"//nas/public": 1000.0}), ("probe", {"//nas/public": 1000.0})]
    for t in (1001.0, 1010.0, 1029.9):
        soma_host.stale_mounts(e, {}, t, probe=probe, timeout_s=0.1, stamps=stamps, interval_s=30, stamp=stamp)
    assert len(order) == 2
    soma_host.stale_mounts(e, {}, 1030.0, probe=probe, timeout_s=0.1, stamps=stamps, interval_s=30, stamp=stamp)
    assert len(order) == 4 and stamps == {"//nas/public": 1030.0}


def test_between_probes_the_last_result_stands():
    e = [("//nas/public", "/mnt/nas")]
    stamps = {}
    stale, rec = soma_host.stale_mounts(e, {}, 1000.0, probe=_never, timeout_s=0.05, stamps=stamps, interval_s=30)
    assert stale == ["/mnt/nas"]
    # past the 60 s back-off but another session stamped a probe 5 s ago: still stale, no probe
    stamps["//nas/public"] = 1065.0
    stale, rec = soma_host.stale_mounts(e, rec, 1070.0, probe=lambda p: 1 / 0, timeout_s=0.05, stamps=stamps,
                                        interval_s=30)
    assert stale == ["/mnt/nas"]


def test_probe_interval_through_the_hook_state(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(soma_host, "PROBE", lambda p: calls.append(p))
    proc = _proc(tmp_path, mounts="//nas/public /mnt/nas cifs ro 0 0\n//nas/public /mnt/nas-rw cifs rw 0 0\n")
    for t in (1000.0, 1005.0, 1020.0):
        _line(tmp_path, proc, now=t)
    assert calls == ["/mnt/nas"]
    assert _state(tmp_path)["net_probe"] == {"//nas/public": 1000.0}
    _line(tmp_path, proc, now=1031.0)
    assert len(calls) == 2
