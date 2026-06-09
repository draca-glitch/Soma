import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
import soma_lib  # noqa: E402


def test_parse_meminfo():
    text = (
        "MemTotal:       64000000 kB\n"
        "MemFree:         1000000 kB\n"
        "MemAvailable:   36000000 kB\n"
        "SwapTotal:       2000000 kB\n"
        "SwapFree:        1000000 kB\n"
    )
    m = soma_lib.parse_meminfo(text)
    assert m["MemTotal"] == 64000000
    assert m["MemAvailable"] == 36000000
    assert m["SwapTotal"] == 2000000
    assert m["SwapFree"] == 1000000


def test_parse_loadavg():
    assert soma_lib.parse_loadavg("1.20 2.30 3.40 1/234 5678") == (1.2, 2.3, 3.4)
    assert soma_lib.parse_loadavg("garbage") == (0.0, 0.0, 0.0)


def test_human_kb():
    assert soma_lib.human_kb(512) == "0.5M"
    assert soma_lib.human_kb(50000) == "49M"
    assert soma_lib.human_kb(6500000) == "6.2G"
    assert soma_lib.human_kb(16252928) == "15.5G"
    assert soma_lib.human_kb(110 * 1024 * 1024) == "110G"  # decimals dropped above 100G


def test_top_rss(tmp_path):
    for pid, pages, name in [("100", 50, "small"), ("200", 500, "big"), ("300", 10, "tiny")]:
        d = tmp_path / pid
        d.mkdir()
        (d / "statm").write_text(f"1000 {pages} 100 1 0 200 0\n")
        (d / "comm").write_text(name + "\n")
    (tmp_path / "notapid").mkdir()  # non-numeric, ignored
    top = soma_lib.top_rss(str(tmp_path))
    assert top["name"] == "big"
    assert top["rss_kb"] == int(500 * soma_lib.PAGE_KB)


def test_top_rss_empty(tmp_path):
    assert soma_lib.top_rss(str(tmp_path)) is None


def test_assess_low_mem():
    state = {
        "cores": 16,
        "mem": {"MemTotal": 1000, "MemAvailable": 50, "SwapTotal": 0, "SwapFree": 0},
        "load": (0.5, 0, 0), "top": None, "disks": [], "services": [],
    }
    a = soma_lib.assess(state)
    assert "LOW_MEM" in a["flags"]


def test_assess_healthy_is_silent():
    state = {
        "cores": 16,
        "mem": {"MemTotal": 64000000, "MemAvailable": 36000000, "SwapTotal": 2000000, "SwapFree": 2000000},
        "load": (1.2, 0, 0),
        "top": {"name": "x", "rss_kb": 1000000},
        "disks": [{"mount": "/", "pct": 50.0, "free_kb": 1}],
        "services": [],
    }
    assert soma_lib.assess(state)["flags"] == set()


def test_assess_top_dominant():
    # 16252928 kB / 64000000 kB = 25.4% > 25% default
    state = {
        "cores": 16,
        "mem": {"MemTotal": 64000000, "MemAvailable": 36000000, "SwapTotal": 0, "SwapFree": 0},
        "load": (1.0,), "top": {"name": "mnemos-mcp", "rss_kb": 16252928},
        "disks": [], "services": [],
    }
    assert "TOP" in soma_lib.assess(state)["flags"]


def test_assess_swap_load_disk_svc():
    state = {
        "cores": 4,
        "mem": {"MemTotal": 1000000, "MemAvailable": 500000, "SwapTotal": 1000000, "SwapFree": 0},
        "load": (8.0, 0, 0), "top": None,
        "disks": [{"mount": "/", "pct": 90.0, "free_kb": 1}],
        "services": [("mariadb", "failed")],
    }
    flags = soma_lib.assess(state)["flags"]
    assert {"SWAP", "LOAD", "DISK", "SVC"} <= flags


def test_assess_unknown_service_not_flagged():
    state = {
        "cores": 16, "mem": {"MemTotal": 100, "MemAvailable": 90},
        "load": (0.1,), "top": None, "disks": [], "services": [("x", "unknown")],
    }
    assert "SVC" not in soma_lib.assess(state)["flags"]


def test_render_carries_flags():
    state = {
        "cores": 16,
        "mem": {"MemTotal": 64000000, "MemAvailable": 3000000, "SwapTotal": 1000000, "SwapFree": 0},
        "load": (20.0, 0, 0),
        "top": {"name": "mnemos-mcp", "rss_kb": 16252928},
        "disks": [{"mount": "/", "pct": 90.0, "free_kb": 1}],
        "services": [],
    }
    a = soma_lib.assess(state)
    line = soma_lib.render(state, a)
    assert line.startswith("[system-state]")
    assert "(LOW)" in line and "(TOP)" in line and "(HIGH)" in line
    assert "mnemos-mcp" in line


def _fake_proc(tmp_path, avail=36000000, total=64000000, swap_total=0, swap_free=0, load="1.0"):
    (tmp_path / "meminfo").write_text(
        f"MemTotal: {total} kB\nMemAvailable: {avail} kB\n"
        f"SwapTotal: {swap_total} kB\nSwapFree: {swap_free} kB\n"
    )
    (tmp_path / "loadavg").write_text(f"{load} 1.0 1.0 1/1 1\n")
    d = tmp_path / "1"
    d.mkdir()
    (d / "statm").write_text("100 10 1 1 0 1 0\n")
    (d / "comm").write_text("init\n")
    return tmp_path


def test_line_for_mode_off():
    assert soma_lib.line_for_mode("off") is None


def _lfm(mode, proc, **kw):
    return soma_lib.line_for_mode(
        mode, proc_root=str(proc), mounts=[], services=[],
        hwmon_root=str(proc / "no-hwmon"), sys_root=str(proc / "no-sys"),
        state_dir=str(proc / "state"), **kw)


def test_line_for_mode_pressure_silent_on_healthy(tmp_path):
    assert _lfm("pressure", _fake_proc(tmp_path)) is None


def test_line_for_mode_always_emits_on_healthy(tmp_path):
    line = _lfm("always", _fake_proc(tmp_path))
    assert line and line.startswith("[system-state]")


def test_line_for_mode_pressure_emits_on_swap(tmp_path):
    proc = _fake_proc(tmp_path, swap_total=2000000, swap_free=0)  # ~1.9G swap used > 256M
    line = _lfm("pressure", proc)
    assert line and "swap" in line


def _fake_hwmon(tmp_path, chips):
    root = tmp_path / "hwmon"
    root.mkdir(exist_ok=True)
    for i, (name, temps) in enumerate(chips):
        d = root / f"hwmon{i}"
        d.mkdir()
        (d / "name").write_text(name + "\n")
        for j, milli in enumerate(temps, start=1):
            (d / f"temp{j}_input").write_text(f"{milli}\n")
    return str(root)


def test_read_temps_classifies_and_takes_max(tmp_path):
    root = _fake_hwmon(tmp_path, [
        ("nvme", [27000, 26000]),
        ("nvme", [30000]),
        ("k10temp", [42000]),
        ("amdgpu", [39000]),
        ("acpitz", [55000]),  # unknown chip class, ignored
    ])
    assert soma_lib.read_temps(root) == {"disk": 30.0, "cpu": 42.0, "gpu": 39.0}


def test_read_temps_missing_root(tmp_path):
    assert soma_lib.read_temps(str(tmp_path / "absent")) == {}


def test_read_temps_malformed_skipped(tmp_path):
    root = _fake_hwmon(tmp_path, [("k10temp", [])])
    (Path(root) / "hwmon0" / "temp1_input").write_text("garbage\n")
    assert soma_lib.read_temps(root) == {}


def _healthy_state(**overrides):
    state = {
        "cores": 16,
        "mem": {"MemTotal": 64000000, "MemAvailable": 36000000, "SwapTotal": 0, "SwapFree": 0},
        "load": (0.1, 0, 0), "top": None, "disks": [], "services": [], "temps": {},
    }
    state.update(overrides)
    return state


def test_assess_flags_hot():
    a = soma_lib.assess(_healthy_state(temps={"cpu": 91.0, "disk": 35.0}))
    assert "HOT" in a["flags"]
    assert a["hot"] == {"cpu"}


def test_assess_temp_class_zero_disables(monkeypatch):
    monkeypatch.setenv("SOMA_TEMP_CPU", "0")
    a = soma_lib.assess(_healthy_state(temps={"cpu": 99.0}))
    assert "HOT" not in a["flags"]


def test_render_temp_segment_and_hot_tag():
    state = _healthy_state(temps={"cpu": 91.0, "disk": 30.0, "gpu": 39.0})
    a = soma_lib.assess(state)
    line = soma_lib.render(state, a)
    assert "temp cpu 91(HOT) disk 30 gpu 39\u00b0C" in line


def test_render_no_temp_segment_when_absent():
    state = _healthy_state()
    a = soma_lib.assess(state)
    assert "temp" not in soma_lib.render(state, a)


def _fake_psi(proc, cpu="0.00", mem="0.00", io="0.00"):
    d = proc / "pressure"
    d.mkdir(exist_ok=True)
    for res, v in (("cpu", cpu), ("memory", mem), ("io", io)):
        (d / res).write_text(
            f"some avg10={v} avg60=0.00 avg300=0.00 total=1\n"
            f"full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
    return proc


def test_read_psi(tmp_path):
    _fake_psi(tmp_path, cpu="1.50", io="38.20")
    assert soma_lib.read_psi(str(tmp_path)) == {"cpu": 1.5, "memory": 0.0, "io": 38.2}


def test_read_psi_missing(tmp_path):
    assert soma_lib.read_psi(str(tmp_path)) == {}


def _fake_sys(tmp_path, ce=None, ue=None, degraded=None):
    sys_root = tmp_path / "sys"
    if ce is not None or ue is not None:
        mc = sys_root / "devices/system/edac/mc/mc0"
        mc.mkdir(parents=True)
        if ce is not None:
            (mc / "ce_count").write_text(f"{ce}\n")
        if ue is not None:
            (mc / "ue_count").write_text(f"{ue}\n")
    if degraded is not None:
        md = sys_root / "block/md0/md"
        md.mkdir(parents=True)
        (md / "degraded").write_text(f"{degraded}\n")
    return str(sys_root)


def test_read_counters(tmp_path):
    (tmp_path / "vmstat").write_text("nr_free_pages 100\noom_kill 7\n")
    sys_root = _fake_sys(tmp_path, ce=3, ue=0, degraded=1)
    c = soma_lib.read_counters(str(tmp_path), sys_root)
    assert c == {"oom_kill": 7, "edac_ce": 3, "edac_ue": 0, "md_degraded": 1}


def test_read_counters_absent_sources(tmp_path):
    assert soma_lib.read_counters(str(tmp_path), str(tmp_path / "sys")) == {}


def test_diff_events():
    cur = {"oom_kill": 9, "edac_ce": 5, "edac_ue": 0, "md_degraded": 0}
    prev = {"oom_kill": 7, "edac_ce": 5, "edac_ue": 0, "md_degraded": 0}
    assert soma_lib.diff_events(cur, prev) == {"oom_kill": 2}
    assert soma_lib.diff_events(cur, {}) == {}  # first run, baseline only
    reset = {"oom_kill": 1, "edac_ce": 0, "edac_ue": 0}
    assert soma_lib.diff_events(reset, prev) == {}  # counter reset, no negative pain
    assert soma_lib.diff_events({"md_degraded": 1}, {"md_degraded": 1}) == {"md_degraded": 1}  # chronic


def test_assess_strain_and_pain_flags():
    state = _healthy_state(psi={"cpu": 1.0, "memory": 0.0, "io": 40.0})
    a = soma_lib.assess(state, events={"oom_kill": 1, "edac_ce": 2})
    assert {"STRAIN", "OOM", "ECC"} <= a["flags"]
    assert a["strained"] == {"io"}


def test_render_psi_and_pain_segments():
    state = _healthy_state(psi={"cpu": 1.0, "memory": 0.0, "io": 40.0})
    a = soma_lib.assess(state, events={"oom_kill": 2, "md_degraded": 1})
    line = soma_lib.render(state, a)
    assert "psi 1/0/40%(STRAIN:io)" in line
    assert "pain oom-kill 2 raid-degraded" in line


def test_state_roundtrip(tmp_path):
    soma_lib.save_state({"counters": {"oom_kill": 1}}, str(tmp_path))
    assert soma_lib.load_state(str(tmp_path)) == {"counters": {"oom_kill": 1}}
    (tmp_path / "soma-state.json").write_text("not json{")
    assert soma_lib.load_state(str(tmp_path)) == {}


def test_line_for_mode_pressure_emits_on_oom_delta(tmp_path):
    proc = _fake_proc(tmp_path)
    (proc / "vmstat").write_text("oom_kill 5\n")
    assert _lfm("pressure", proc) is None  # first run: baseline, no pain
    (proc / "vmstat").write_text("oom_kill 6\n")
    line = _lfm("pressure", proc)
    assert line and "pain oom-kill 1" in line
    assert _lfm("pressure", proc) is None  # delta consumed, silent again
