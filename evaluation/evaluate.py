"""Evaluation harness for the metrics in the project document.

Runs each stress scenario, measures:
  * detection accuracy  - was the right issue type raised?
  * detection time      - seconds from launch to alert
  * prediction accuracy - predicted vs actual time for memory to reach the limit (memory leak run)
  * fix success rate    - does the guided plan (auto-approved here) resolve the issue?
  * explanation quality - LLM-only vs LLM+RAG answers, scored by OS-keyword coverage (a proxy for
                          the human rating described in the report)
Results are written to evaluation/results.csv and printed as a table.

    python evaluation/evaluate.py                    # all Linux scenarios
    python evaluation/evaluate.py --only cpu_hog zombie
Run inside a Linux VM. The thrashing scenario is skipped unless --with-thrashing is given.
"""
import argparse
import csv
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SYSDOCTOR_INTERVAL", "2")
os.environ.setdefault("SYSDOCTOR_DATA_DIR", str(ROOT / "data" / "eval"))

from sysdoctor.core import SysDoctor          # noqa: E402
from sysdoctor.models import Issue            # noqa: E402

ST = ROOT / "stress_tests"
PY = sys.executable
SCENARIOS = {
    "cpu_hog": ([PY, str(ST / "cpu_hog.py")], "cpu_hog", 60),
    "memory_leak": ([PY, str(ST / "memory_leak.py"), "--rate", "15", "--max", "1500"], "memory_leak", 120),
    "zombie": ([PY, str(ST / "zombie.py"), "--count", "6", "--reap-on-signal"], "zombie", 45),
    "disk_bottleneck": ([PY, str(ST / "disk_io.py"), "--mb", "256"], "disk_bottleneck", 90),
    "thrashing": ([PY, str(ST / "thrashing.py"), "--fraction", "1.2", "--seconds", "150"], "thrashing", 150),
}
KEYWORDS = {
    "cpu_hog": ["scheduler", "nice", "priority", "starvation", "run queue", "time slice", "cfs"],
    "memory_leak": ["rss", "heap", "free", "virtual memory", "swap", "oom", "resident"],
    "zombie": ["wait", "parent", "sigchld", "reap", "exit", "process table", "init"],
    "disk_bottleneck": ["iowait", "i/o", "uninterruptible", "iostat", "throughput", "d state", "disk"],
    "thrashing": ["working set", "page fault", "swap", "paging", "ram", "major fault"],
}


def wait_for_alert(doc, issue_type, t0, timeout, pid=None):
    while time.time() - t0 < timeout:
        for a in doc.db.open_alerts():
            if a["issue_type"] == issue_type and a["ts"] >= t0:
                return a, a["ts"] - t0
        time.sleep(1)
    return None, None


def keyword_score(text, issue_type):
    t = text.lower()
    kws = KEYWORDS.get(issue_type, [])
    return round(sum(k in t for k in kws) / max(len(kws), 1), 2)


def prediction_check(doc, t0, limit_timeout=180):
    """Compare predicted minutes-to-limit (first prediction) with the observed trend continuation."""
    preds = doc.db.query("SELECT * FROM predictions WHERE resource='memory' AND ts>=? AND minutes_to_limit IS NOT NULL"
                         " ORDER BY ts LIMIT 1", (t0,))
    if not preds:
        return None
    p = preds[0]
    target_ts = p["ts"] + p["minutes_to_limit"] * 60
    # actual: fit the real memory curve after the prediction and see when it crossed the limit (or extrapolate)
    rows = doc.db.query("SELECT ts, mem_percent FROM metrics WHERE ts>=? ORDER BY ts", (p["ts"],))
    crossed = next((r["ts"] for r in rows if r["mem_percent"] >= 95), None)
    if crossed is None and len(rows) > 5:
        import numpy as np
        t = np.array([r["ts"] for r in rows]); y = np.array([r["mem_percent"] for r in rows])
        slope, icpt = np.polyfit(t - t[0], y, 1)
        crossed = t[0] + (95 - icpt) / slope if slope > 0 else None
    if crossed is None:
        return None
    crossed = float(crossed)
    err_min = abs(target_ts - crossed) / 60
    actual_min = (crossed - p["ts"]) / 60
    return {"predicted_min": round(p["minutes_to_limit"], 1), "actual_min": round(actual_min, 1),
            "abs_error_min": round(err_min, 1)}


def run(names, with_thrashing=False):
    doc = SysDoctor(start_monitor=True)
    time.sleep(8)                                  # warm-up baseline
    results = []
    for name in names:
        if name == "thrashing" and not with_thrashing:
            continue
        if name in ("zombie",) and os.name == "nt":
            continue
        cmd, expected, timeout = SCENARIOS[name]
        print(f"\n=== {name}: {' '.join(cmd[1:])}")
        t0 = time.time()
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        alert, dt = wait_for_alert(doc, expected, t0, timeout)
        row = {"scenario": name, "detected": alert is not None,
               "detection_time_s": round(dt, 1) if dt is not None else "", "fixed": "", "fix_message": "",
               "rag_keyword_score": "", "llm_only_keyword_score": "", "prediction": ""}
        if name == "memory_leak":
            time.sleep(20)
            row["prediction"] = prediction_check(doc, t0) or ""
        if alert:
            issue = Issue.from_alert(alert)
            live = doc.pipeline.n_collect({"issue": issue})
            docs = doc.kb.search(doc.kb.query_for_issue(issue))
            with_rag = doc.llm.diagnose(issue, live["live"], docs, context=live["context"], use_rag=True)
            without = doc.llm.diagnose(issue, live["live"], [], context=live["context"], use_rag=False)
            row["rag_keyword_score"] = keyword_score(with_rag.root_cause + " " + with_rag.os_concept, name)
            row["llm_only_keyword_score"] = keyword_score(without.root_cause + " " + without.os_concept, name)
            # guided fix with automatic approval (simulates a user who accepts every step)
            s = doc.start_guide(alert)
            while s.status == "guiding" and s.current_step:
                s.approve()
            row["fixed"] = s.status == "resolved"
            row["fix_message"] = s.last_verification.message if s.last_verification else s.status
        if proc.poll() is None:
            proc.kill()
        subprocess.run(["pkill", "-f", "sysdoctor_disk_stress"], capture_output=True) if os.name != "nt" else None
        print(row)
        results.append(row)
        time.sleep(10)                             # let the system settle
    doc.monitor.stop()
    out = ROOT / "evaluation" / "results.csv"
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()) if results else ["scenario"])
        w.writeheader()
        w.writerows(results)
    det = [r for r in results if r["detected"]]
    print("\n================ SUMMARY ================")
    print(f"Detection accuracy : {len(det)}/{len(results)}")
    if det:
        print(f"Mean detection time: {sum(r['detection_time_s'] for r in det)/len(det):.1f} s")
        fixed = [r for r in det if r["fixed"]]
        print(f"Fix success rate   : {len(fixed)}/{len(det)}")
        rag = [r["rag_keyword_score"] for r in det if r["rag_keyword_score"] != ""]
        llm = [r["llm_only_keyword_score"] for r in det if r["llm_only_keyword_score"] != ""]
        if rag:
            print(f"OS-concept coverage: LLM+RAG {sum(rag)/len(rag):.2f} vs LLM-only {sum(llm)/len(llm):.2f}")
    print(f"Results written to {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=list(SCENARIOS))
    ap.add_argument("--with-thrashing", action="store_true")
    a = ap.parse_args()
    run(a.only, a.with_thrashing)
