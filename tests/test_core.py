"""Unit tests: run with  python -m pytest -q"""
import os
import sys
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sysdoctor.detector import Detector, linear_fit          # noqa: E402
from sysdoctor.knowledge_base import KnowledgeBase, chunk_text  # noqa: E402
from sysdoctor.models import Action, Issue                   # noqa: E402
from sysdoctor.predictor import predict                      # noqa: E402
from sysdoctor.safety import validate                        # noqa: E402


def metric(ts, **kw):
    base = dict(ts=ts, cpu_percent=10, iowait=1, load1=0.5, mem_percent=40, swap_percent=0, swap_used_mb=0,
                swap_in_sec=0, swap_out_sec=0, major_faults_sec=0, disk_read_mb_sec=1, disk_write_mb_sec=1,
                disk_busy_percent=5, disk_percent=50, disk_free_gb=100, proc_running=2)
    base.update(kw)
    return base


# ---------------- safety layer ----------------
def test_blocks_pid_1():
    assert not validate(Action("kill", 1, None)).allowed


def test_blocks_self():
    assert not validate(Action("terminate", os.getpid(), psutil.Process().name())).allowed


def test_blocks_unknown_action():
    assert not validate(Action("rm_rf", 12345)).allowed


def test_blocks_missing_pid():
    assert not validate(Action("kill", None)).allowed


def test_blocks_pid_reuse():
    import subprocess
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        d = validate(Action("terminate", p.pid, "definitely_not_this_name"))
        assert not d.allowed and "reused" in d.reason
        ok = validate(Action("terminate", p.pid, psutil.Process(p.pid).name()))
        assert ok.allowed and ok.requires_confirmation and ok.risk == "high"
    finally:
        p.kill()


def test_negative_nice_blocked():
    import subprocess
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert not validate(Action("renice", p.pid, value=-5)).allowed
    finally:
        p.kill()


# ---------------- detection ----------------
def test_linear_fit():
    s, i, r2 = linear_fit([0, 1, 2, 3], [1, 3, 5, 7])
    assert abs(s - 2) < 1e-9 and r2 > 0.999


def test_memory_leak_detected():
    rows = [dict(ts=t * 3.0, pid=42, name="leaky", rss_mb=100 + t * 5, create_time=1.0) for t in range(20)]
    issues = Detector().detect_memory_leaks(rows)
    assert issues and issues[0].issue_type == "memory_leak" and issues[0].pid == 42


def test_stable_memory_not_leak():
    rows = [dict(ts=t * 3.0, pid=42, name="ok", rss_mb=300 + (t % 3), create_time=1.0) for t in range(30)]
    assert Detector().detect_memory_leaks(rows) == []


def test_cpu_hog():
    snaps = [[dict(ts=i * 3.0, pid=7, name="spin", status="running", cpu_percent=99, create_time=1, nice=0,
                   num_threads=1)] for i in range(6)]
    hog = Detector().detect_cpu_hogs(snaps, 4, 30)[0]
    assert hog.pid == 7 and hog.evidence["duration_s"] == 15 and hog.evidence["value"] == 99


def test_thrashing():
    m = [metric(i, mem_percent=95, swap_in_sec=800, swap_out_sec=900, major_faults_sec=400) for i in range(5)]
    assert Detector().detect_thrashing(m)[0].issue_type == "thrashing"


def test_zombie():
    procs = [dict(pid=10, ppid=5, name="child", status="zombie"), dict(pid=5, ppid=1, name="parent", status="sleeping")]
    i = Detector().detect_zombies(procs)[0]
    assert i.pid == 5 and i.evidence["zombie_pids"] == [10]


def test_disk_bottleneck():
    m = [metric(i, iowait=45, disk_busy_percent=99) for i in range(5)]
    assert any(x.issue_type == "disk_bottleneck" for x in Detector().detect_disk_bottleneck(m))


def test_prediction():
    now = time.time()
    m = [metric(now + i * 3, mem_percent=50 + i * 0.5) for i in range(40)]
    p = [x for x in predict(m) if x.resource == "memory"][0]
    # 0.5 %/sample, 20 samples/min -> 10 %/min; from 69.5 to 95 -> ~2.55 min
    assert p.minutes_to_limit is not None and 2.0 < p.minutes_to_limit < 3.2


def test_isolation_forest_flags_outlier():
    import random
    random.seed(0)
    m = [metric(i, cpu_percent=random.uniform(5, 15), mem_percent=random.uniform(38, 42)) for i in range(120)]
    m += [metric(200 + i, cpu_percent=100, iowait=60, mem_percent=97, swap_in_sec=900, disk_write_mb_sec=300,
                 load1=12) for i in range(3)]
    assert Detector().detect_anomaly(m)


# ---------------- RAG ----------------
def test_chunking_overlap():
    text = "word " * 1000
    chunks = chunk_text(text, size=300, overlap=50)
    assert len(chunks) > 3 and all(len(c) <= 300 for c in chunks)


def test_retrieval_relevance():
    kb = KnowledgeBase()
    hits = kb.search("child process exited but parent never called wait defunct")
    assert hits and "zombie" in hits[0]["source"]
    hits = kb.search("pages swapped in and out constantly system frozen")
    assert "thrashing" in hits[0]["source"]
