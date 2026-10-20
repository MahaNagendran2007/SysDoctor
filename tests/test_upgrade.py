"""Tests for the Windows-compatibility, RAG, LLM, safety, verification and history upgrades.
Run:  python -m pytest -q      (or: python tests/run_tests.py  if pytest is not installed)"""
import json
import os
import sys
import tempfile
import time
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("SYSDOCTOR_DATA_DIR", tempfile.mkdtemp(prefix="sysdoctor_test_"))

import psutil                                             # noqa: E402

from sysdoctor import collector as C, config              # noqa: E402
from sysdoctor.database import Database                   # noqa: E402
from sysdoctor.detector import Detector                   # noqa: E402
from sysdoctor.llm import LLMClient                       # noqa: E402
from sysdoctor.models import Action, Issue                # noqa: E402
from sysdoctor.predictor import predict                   # noqa: E402
from sysdoctor.safety import is_critical, validate        # noqa: E402


def metric(ts, **kw):
    base = dict(ts=ts, cpu_percent=10, iowait=None, load1=None, mem_percent=40, mem_available_mb=8000,
                swap_percent=5, swap_used_mb=500, swap_in_sec=None, swap_out_sec=None, major_faults_sec=None,
                page_faults_sec=2000, disk_read_mb_sec=1, disk_write_mb_sec=1, disk_busy_percent=None,
                disk_latency_ms=1.0, disk_percent=50, disk_free_gb=100, proc_running=2)
    base.update(kw)
    return base


# ------------------------------------------------------------ Windows collector
class FakeProc:
    def __init__(self, info):
        self.info = info

    def cpu_percent(self, interval=None):
        return 0.0


def test_windows_collector_uses_real_substitutes_and_no_fake_linux_metrics(monkeypatch=None):
    saved = (C.IS_LINUX, C.IS_WINDOWS, dict(C.CAPABILITIES), psutil.process_iter, psutil.disk_io_counters,
             psutil.cpu_times_percent)
    Mem = namedtuple("pmem", "rss vms num_page_faults")
    IO = namedtuple("pio", "read_bytes write_bytes")
    Disk = namedtuple("sdiskio", "read_count write_count read_bytes write_bytes read_time write_time")
    state = {"n": 0}

    def procs(attrs, ad_value=None):
        n = state["n"]
        for pid, name in ((4242, "python.exe"), (100, "brave.exe")):
            yield FakeProc({"pid": pid, "ppid": 1, "name": name, "username": "PC\\user", "status": "running",
                            "cpu_percent": 5.0, "memory_info": Mem(200 * 2**20, 0, 1000 + n * 3000),
                            "memory_percent": 1.0, "num_threads": 4, "nice": 32, "create_time": 1.0,
                            "cmdline": [name], "num_handles": 321,
                            "io_counters": IO(0, n * 60 * 2**20 if pid == 4242 else 0)})

    def disk():
        n = state["n"]
        return Disk(100 + n * 100, 100 + n * 100, 0, n * 100 * 2**20, n * 1000, n * 1000)
    try:
        C.IS_LINUX, C.IS_WINDOWS = False, True
        C.CAPABILITIES.update(iowait=False, swap_activity=False, major_faults=False, disk_busy=False,
                              zombies=False, page_faults_total=True, process_io=True, disk_latency=True)
        psutil.process_iter, psutil.disk_io_counters = procs, disk
        psutil.cpu_times_percent = lambda interval=None: SimpleNamespace(user=5, system=2, idle=93)  # no iowait
        c = C.Collector(Database(Path(os.environ["SYSDOCTOR_DATA_DIR"]) / "win.db"))
        c._primed = True
        m1, _ = c.collect_once()
        state["n"] = 1
        time.sleep(1.0)
        m2, p2 = c.collect_once()
    finally:
        (C.IS_LINUX, C.IS_WINDOWS, caps, psutil.process_iter, psutil.disk_io_counters,
         psutil.cpu_times_percent) = saved
        C.CAPABILITIES.clear(); C.CAPABILITIES.update(caps)
    # Linux-only metrics are NULL, never 0
    for k in ("iowait", "swap_in_sec", "swap_out_sec", "major_faults_sec", "disk_busy_percent", "proc_zombie"):
        assert m2[k] is None, k
    # Windows substitutes are measured
    assert m2["page_faults_sec"] and m2["page_faults_sec"] > 1000          # 2 procs x 3000 faults / ~1 s
    assert abs(m2["disk_latency_ms"] - 10.0) < 0.01                        # 2000 ms / 200 requests
    py = [p for p in p2 if p["pid"] == 4242][0]
    assert py["open_files"] == 321 and py["io_mb_sec"] > 30               # handles + per-process disk rate


def test_windows_disk_pressure_detected_without_iowait():
    procs = [dict(pid=4242, name="python.exe", status="running", io_mb_sec=180.0),
             dict(pid=100, name="brave.exe", status="running", io_mb_sec=0.1)]
    m = [metric(i * 3, disk_write_mb_sec=160, disk_read_mb_sec=5, disk_latency_ms=3) for i in range(8)]
    issues = Detector().detect_disk_bottleneck(m, procs)
    assert issues and issues[0].title == "Sustained heavy disk activity"
    assert issues[0].pid == 4242 and issues[0].evidence["iowait"] is None


def test_windows_disk_latency_bottleneck():
    m = [metric(i * 3, disk_latency_ms=120, disk_read_mb_sec=3) for i in range(4)]
    iss = Detector().detect_disk_bottleneck(m, [])
    assert iss and iss[0].evidence["metric"] == "Disk latency"


def test_quiet_windows_disk_no_alert():
    assert Detector().detect_disk_bottleneck([metric(i * 3) for i in range(10)], []) == []


def test_windows_thrashing_approximation():
    base = [metric(i * 3, mem_percent=93, swap_percent=20, page_faults_sec=2000) for i in range(20)]
    storm = [metric(60 + i * 3, mem_percent=94, swap_percent=24, page_faults_sec=9000, disk_read_mb_sec=40)
             for i in range(3)]
    iss = Detector().detect_thrashing(base + storm)
    assert iss and iss[0].evidence.get("approximation") is True
    calm = [metric(60 + i * 3, mem_percent=94, swap_percent=20, page_faults_sec=2100) for i in range(3)]
    assert Detector().detect_thrashing(base + calm) == []


def test_zombie_detection_disabled_on_windows():
    d = Detector()
    d.system = "Windows"
    assert d.detect_zombies([dict(pid=5, ppid=1, name="x", status="zombie")]) == []


def test_high_memory_sustained():
    m = [metric(i * 3, mem_percent=93) for i in range(12)]
    procs = [dict(pid=4242, name="python.exe", rss_mb=5000.0), dict(pid=10, name="brave.exe", rss_mb=900.0)]
    iss = Detector().detect_high_memory(m, procs)
    assert iss and iss[0].process == "python.exe" and iss[0].evidence["duration_s"] == 33
    assert Detector().detect_high_memory([metric(i * 3, mem_percent=93) for i in range(5)], procs) == []


# ---------------------------------------------------------------- prediction
def test_prediction_refuses_with_too_few_samples():
    p = [x for x in predict([metric(i * 3, mem_percent=80 + i) for i in range(5)]) if x.resource == "memory"][0]
    assert p.status == "insufficient" and p.minutes_to_limit is None


def test_prediction_robust_to_spike_and_noise():
    import random
    rows = [metric(i * 3, mem_percent=50 + i * 0.5) for i in range(40)]
    rows[20]["mem_percent"] = 99
    p = [x for x in predict(rows) if x.resource == "memory"][0]
    assert p.status == "rising" and 2.0 < p.minutes_to_limit < 3.2
    random.seed(3)
    noise = [metric(i * 3, mem_percent=60 + random.uniform(-3, 3)) for i in range(40)]
    assert [x for x in predict(noise) if x.resource == "memory"][0].status == "stable"


def test_prediction_ignores_data_before_a_gap():
    rows = [metric(i * 3, mem_percent=90) for i in range(30)] + [metric(5000 + i * 3, mem_percent=50) for i in range(5)]
    assert [x for x in predict(rows) if x.resource == "memory"][0].status == "insufficient"


# -------------------------------------------------------------------- safety
def test_llm_shell_action_is_blocked():
    assert not validate(Action.from_dict({"type": "shell", "command": "rm -rf /"})).allowed
    assert not validate(Action.from_dict({"type": "exec", "pid": os.getpid()})).allowed


def test_read_only_actions_allowed_without_confirmation():
    for t in ("refresh_metrics", "recheck"):
        d = validate(Action(t))
        assert d.allowed and not d.requires_confirmation


def test_windows_critical_processes_blocked():
    for name in ("explorer.exe", "csrss.exe", "winlogon.exe", "svchost.exe", "lsass.exe", "dwm.exe"):
        fake = SimpleNamespace(pid=9999, name=lambda n=name: n, ppid=lambda: 500)
        assert is_critical(fake)[0], name


def test_executor_has_no_shell_path():
    src = (ROOT / "sysdoctor" / "executor.py").read_text()
    assert "subprocess" not in src and "os.system" not in src and "shell=True" not in src


# ----------------------------------------------------------------------- LLM
def test_model_name_resolution():
    r = LLMClient.resolve_model
    assert r("llama3:8b", ["llama3:latest"]) == "llama3:latest"
    assert r("llama3", ["llama3:8b"]) == "llama3:8b"
    assert r("mistral:7b", ["llama3:latest"]) is None
    assert r("llama3:70b", ["llama3:latest"]) is None


def test_ollama_status_and_real_request():
    from fake_ollama import FakeOllama
    old = config.OLLAMA_URL
    f = FakeOllama(models=["llama3:latest"], reply=json.dumps({"summary": "s", "root_cause": "r",
                                                               "os_concept": "c", "steps": []}))
    try:
        config.OLLAMA_URL = f.url
        c = LLMClient("ollama", "llama3:8b")
        assert c.status().online and c.status().resolved_model == "llama3:latest"
        iss = Issue("cpu_hog", "warning", "t", "d", pid=1, process="p")
        docs = [{"text": "S\nUNIQUE-RAG-MARKER passage", "source": "a.md", "section": "S", "score": 1}]
        d = c.diagnose(iss, {}, docs, rag_query="q")
        assert d.used_llm and "UNIQUE-RAG-MARKER" in f.requests[-1]["messages"][1]["content"]
        f.fail_chat = True
        d2 = c.diagnose(iss, {}, docs)
        assert not d2.used_llm and "HTTP 500" in d2.llm_error
    finally:
        f.stop()
        config.OLLAMA_URL = old
    config.OLLAMA_URL = "http://127.0.0.1:9"
    try:
        st = LLMClient("ollama", "llama3:8b").status()
        assert not st.online and "ollama pull llama3:8b" in st.hint
    finally:
        config.OLLAMA_URL = old


def test_offline_is_labelled_and_uses_rag_passages():
    iss = Issue("cpu_hog", "warning", "t", "d", pid=1, process="p")
    docs = [{"text": "CPU\nA CPU hog keeps a core busy.", "source": "02.md", "section": "CPU", "score": 1}]
    d = LLMClient("offline").diagnose(iss, {}, docs)
    assert not d.used_llm and "A CPU hog keeps a core busy." in d.os_concept and d.passages == docs


# ---------------------------------------------------------------- database
def test_old_database_is_migrated_in_place():
    import sqlite3
    path = Path(os.environ["SYSDOCTOR_DATA_DIR"]) / "old.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE actions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, alert_id INTEGER, action TEXT,"
              " pid INTEGER, process TEXT, risk TEXT, decision TEXT, result TEXT, message TEXT)")
    c.execute("INSERT INTO actions (ts, action) VALUES (1, 'renice')")
    c.commit(); c.close()
    db = Database(path)
    row = db.recent_actions()[0]
    assert row["action"] == "renice" and "resolved" in row and "before" in row
