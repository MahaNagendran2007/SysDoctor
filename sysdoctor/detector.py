"""Layer 2a - Anomaly detection.

Rule-based detectors for the five target problems plus an Isolation Forest that learns the
machine's normal behaviour and flags unusual combinations fixed rules may miss.
"""
import platform
import time
from collections import defaultdict

import numpy as np

from . import config
from .models import Issue

try:
    from sklearn.ensemble import IsolationForest
except ImportError:                     # pragma: no cover
    IsolationForest = None

IF_FEATURES = ["cpu_percent", "iowait", "mem_percent", "swap_percent", "swap_in_sec", "swap_out_sec",
               "major_faults_sec", "page_faults_sec", "disk_read_mb_sec", "disk_write_mb_sec",
               "disk_latency_ms", "load1", "proc_running"]

# Processes that legitimately use lots of CPU or memory for long periods (not "leaks").
BENIGN_GROWERS = {"kswapd0", "kworker", "systemd-journald", "ollama", "ollama_llama_server"}


def linear_fit(x, y):
    """Return slope, intercept, r2 of least-squares line."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if len(x) < 2 or np.ptp(x) == 0:
        return 0.0, float(y[-1]) if len(y) else 0.0, 0.0
    slope, intercept = np.polyfit(x, y, 1)
    pred = slope * x + intercept
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return float(slope), float(intercept), float(r2)


def _vals(rows, key):
    """Values of a metric that the OS actually provided (None = not available on this OS)."""
    v = [r.get(key) for r in rows if r.get(key) is not None]
    return v if v else None


def _mean(rows, key):
    v = _vals(rows, key)
    return float(np.mean(v)) if v else None


def _run_length(rows, pred):
    """How many of the most recent rows satisfy pred, counting backwards, and their time span (s)."""
    n = 0
    for r in reversed(rows):
        if not pred(r):
            break
        n += 1
    if n >= 2 and rows[-1].get("ts") is not None and rows[-n].get("ts") is not None:
        span = rows[-1]["ts"] - rows[-n]["ts"]
    else:
        span = 0.0
    return n, span


class Detector:
    def __init__(self):
        self.iforest = None
        self._if_trained_at = 0
        self._if_n = 0
        self.system = platform.system()

    # ------------------------------------------------------------------ rules
    def detect_memory_leaks(self, proc_rows):
        """proc_rows: list of dicts with ts, pid, name, rss_mb, create_time (ordered by ts)."""
        series = defaultdict(list)
        for r in proc_rows:
            series[(r["pid"], r["create_time"])].append(r)
        issues = []
        for (pid, _), rows in series.items():
            if len(rows) < config.LEAK_MIN_SAMPLES:
                continue
            name = rows[-1]["name"]
            if name in BENIGN_GROWERS:
                continue
            t = np.array([r["ts"] for r in rows])
            rss = np.array([r["rss_mb"] for r in rows])
            slope_sec, _, r2 = linear_fit(t - t[0], rss)
            slope_min = slope_sec * 60
            growth = rss[-1] - rss[0]
            # monotonic-ish: at least 80 % of steps non-decreasing
            steps = np.diff(rss)
            mono = float(np.mean(steps >= -0.5)) if len(steps) else 0
            if (slope_min >= config.LEAK_MIN_SLOPE_MB_MIN and r2 >= config.LEAK_MIN_R2
                    and growth >= config.LEAK_MIN_GROWTH_MB and mono >= 0.8):
                severity = "critical" if slope_min >= 5 * config.LEAK_MIN_SLOPE_MB_MIN else "warning"
                issues.append(Issue(
                    issue_type="memory_leak", severity=severity, pid=pid, process=name,
                    title=f"Abnormal memory growth in {name} (PID {pid})",
                    details=(f"Resident memory grew from {rss[0]:.0f} MB to {rss[-1]:.0f} MB over "
                             f"{(t[-1]-t[0])/60:.1f} min (~{slope_min:.0f} MB/min) and never came back down."),
                    evidence={"metric": "Memory growth", "value": round(slope_min, 1), "unit": "MB/min",
                              "threshold": f">= {config.LEAK_MIN_SLOPE_MB_MIN:.0f} MB/min, steady (R² >= "
                                           f"{config.LEAK_MIN_R2}) and >= {config.LEAK_MIN_GROWTH_MB:.0f} MB total",
                              "duration_s": round(float(t[-1] - t[0])),
                              "rss_start_mb": round(float(rss[0]), 1), "rss_now_mb": round(float(rss[-1]), 1),
                              "growth_mb_per_min": round(slope_min, 1), "r2": round(r2, 3),
                              "window_min": round((t[-1] - t[0]) / 60, 1)}))
        return issues

    def detect_high_memory(self, metrics, procs=None):
        """Sustained high system memory usage (all operating systems)."""
        need = config.HIGH_MEM_SAMPLES
        if len(metrics) < need:
            return []
        n, span = _run_length(metrics, lambda m: m["mem_percent"] >= config.HIGH_MEM_PERCENT)
        if n < need:
            return []
        cur = metrics[-1]
        top = sorted([p for p in (procs or []) if p["pid"] > 4], key=lambda p: p["rss_mb"], reverse=True)[:3]
        top_txt = ", ".join(f"{p['name']} ({p['rss_mb']:.0f} MB)" for p in top) or "unknown"
        return [Issue(
            issue_type="high_memory", severity="critical" if cur["mem_percent"] >= 95 else "warning",
            process=top[0]["name"] if top else None,
            title=f"Memory has stayed above {config.HIGH_MEM_PERCENT:.0f}% for {span:.0f} s",
            details=(f"RAM is {cur['mem_percent']:.0f}% used ({cur['mem_available_mb']:.0f} MB available). "
                     f"Biggest users: {top_txt}."),
            evidence={"metric": "System memory", "value": cur["mem_percent"], "unit": "%",
                      "threshold": f">= {config.HIGH_MEM_PERCENT:.0f}% for {need} samples",
                      "duration_s": round(span), "mem_available_mb": cur["mem_available_mb"],
                      "top_processes": [f"{p['name']}({p['pid']})" for p in top]})]

    def detect_cpu_hogs(self, recent_proc_snapshots, cpu_count, system_cpu):
        """recent_proc_snapshots: list (oldest->newest) of lists of process dicts."""
        need = config.CPU_HOG_SAMPLES
        if len(recent_proc_snapshots) < need:
            return []
        per_pid = defaultdict(list)
        for snap in recent_proc_snapshots:
            for p in snap:
                if p["status"] != "zombie" and p["pid"] != 0:
                    per_pid[(p["pid"], p["create_time"])].append(p)
        issues = []
        for (pid, _), rows in per_pid.items():
            n, span = _run_length(rows, lambda r: r["cpu_percent"] >= config.CPU_HOG_PERCENT)
            if n < need:
                continue
            vals = [r["cpu_percent"] for r in rows[-n:]]
            avg = float(np.mean(vals))
            name = rows[-1]["name"]
            if name.lower() in ("idle", "system idle process"):
                continue
            severity = "critical" if system_cpu >= 90 else "warning"
            issues.append(Issue(
                issue_type="cpu_hog", severity=severity, pid=pid, process=name,
                title=f"{name} (PID {pid}) is hogging the CPU",
                details=(f"It used on average {avg:.0f}% of a CPU core for the last {span:.0f} s "
                         f"(system CPU {system_cpu:.0f}% across {cpu_count} cores)."),
                evidence={"metric": "Process CPU", "value": round(rows[-1]["cpu_percent"], 1), "unit": "% of one core",
                          "threshold": f">= {config.CPU_HOG_PERCENT:.0f}% of a core for {need} consecutive samples",
                          "duration_s": round(span),
                          "avg_cpu_percent": round(avg, 1), "min_cpu_percent": round(min(vals), 1),
                          "samples": n, "system_cpu": system_cpu, "cores": cpu_count,
                          "nice": rows[-1].get("nice"), "threads": rows[-1].get("num_threads")}))
        return issues

    def detect_thrashing(self, metrics):
        if len(metrics) < 3:
            return []
        last = metrics[-3:]
        mem = _mean(last, "mem_percent")
        cpu = _mean(last, "cpu_percent")
        swap_in, swap_out = _mean(last, "swap_in_sec"), _mean(last, "swap_out_sec")
        majflt = _mean(last, "major_faults_sec")
        if swap_in is not None and swap_out is not None:            # Linux: real swap counters
            swap_rate = swap_in + swap_out
            majflt = majflt or 0.0
            iowait = _mean(last, "iowait") or 0.0
            if mem >= config.THRASH_MEM_PERCENT and (swap_rate >= config.THRASH_SWAP_PAGES_SEC
                                                     or majflt >= config.THRASH_MAJFLT_SEC):
                severity = "critical" if swap_rate >= 5 * config.THRASH_SWAP_PAGES_SEC else "warning"
                return [Issue(
                    issue_type="thrashing", severity=severity,
                    title="System is thrashing (constant swapping)",
                    details=(f"RAM is {mem:.0f}% full and the kernel is moving ~{swap_rate:.0f} pages/s to and "
                             f"from swap with {majflt:.0f} major page faults/s. CPU {cpu:.0f}%, I/O wait {iowait:.0f}%."),
                    evidence={"metric": "Swap traffic", "value": round(swap_rate, 1), "unit": "pages/s",
                              "threshold": f"RAM >= {config.THRASH_MEM_PERCENT:.0f}% and swap >= "
                                           f"{config.THRASH_SWAP_PAGES_SEC:.0f} pages/s or major faults >= "
                                           f"{config.THRASH_MAJFLT_SEC:.0f}/s",
                              "duration_s": round(last[-1]["ts"] - last[0]["ts"]),
                              "mem_percent": round(mem, 1), "swap_pages_per_sec": round(swap_rate, 1),
                              "major_faults_per_sec": round(majflt, 1), "cpu_percent": round(cpu, 1),
                              "iowait": round(iowait, 1), "swap_percent": last[-1]["swap_percent"]})]
            return []
        # Windows approximation: no swap-in/out counters exist, so combine memory pressure, a rising
        # page file, a page-fault storm (vs. this machine's own baseline) and disk reads.
        if len(metrics) < 20:
            return []
        faults = _vals(metrics[:-3], "page_faults_sec")
        now_faults = _mean(last, "page_faults_sec")
        if not faults or now_faults is None:
            return []
        baseline = float(np.median(faults)) or 1.0
        swap_rise = last[-1]["swap_percent"] - metrics[-20]["swap_percent"]
        reads = _mean(last, "disk_read_mb_sec") or 0.0
        if (mem >= config.THRASH_MEM_PERCENT and swap_rise > 0.5
                and now_faults >= config.WIN_THRASH_FAULT_FACTOR * baseline and reads >= 5):
            return [Issue(
                issue_type="thrashing", severity="warning",
                title="Likely thrashing (heavy paging)",
                details=(f"RAM is {mem:.0f}% full, the page file grew {swap_rise:.1f} points, page faults are "
                         f"{now_faults/baseline:.1f}x this machine's normal rate and the disk is reading "
                         f"{reads:.0f} MB/s. (Windows approximation: swap-in/out counters are available on Linux only.)"),
                evidence={"metric": "Page faults", "value": round(now_faults), "unit": "/s",
                          "threshold": f"RAM >= {config.THRASH_MEM_PERCENT:.0f}%, page file rising, faults >= "
                                       f"{config.WIN_THRASH_FAULT_FACTOR:.0f}x baseline ({baseline:.0f}/s)",
                          "duration_s": round(last[-1]["ts"] - last[0]["ts"]), "mem_percent": round(mem, 1),
                          "swap_percent": last[-1]["swap_percent"], "approximation": True})]
        return []

    def detect_zombies(self, procs):
        if self.system == "Windows":          # Windows has no zombie state
            return []
        zombies = [p for p in procs if p["status"] == "zombie"]
        if len(zombies) < config.ZOMBIE_MIN_COUNT:
            return []
        by_parent = defaultdict(list)
        for z in zombies:
            by_parent[z["ppid"]].append(z)
        names = {p["pid"]: p["name"] for p in procs}
        issues = []
        for ppid, zs in by_parent.items():
            parent = names.get(ppid)
            if parent is None:
                try:
                    import psutil
                    parent = psutil.Process(ppid).name()
                except Exception:
                    parent = "unknown"
            severity = "warning" if len(zs) >= 5 else "info"
            issues.append(Issue(
                issue_type="zombie", severity=severity, pid=ppid, process=parent,
                title=f"{len(zs)} zombie process(es) left by {parent} (PID {ppid})",
                details=(f"Child processes {', '.join(str(z['pid']) for z in zs[:10])} have finished but their "
                         f"parent {parent} has not collected their exit status with wait()."),
                evidence={"metric": "Zombie children", "value": len(zs), "unit": "",
                          "threshold": f">= {config.ZOMBIE_MIN_COUNT} defunct child", "duration_s": None,
                          "parent_pid": ppid, "parent": parent, "zombie_pids": [z["pid"] for z in zs],
                          "zombie_count": len(zs)}))
        return issues

    def detect_disk_bottleneck(self, metrics, procs=None):
        if len(metrics) < 3:
            return []
        last = metrics[-3:]
        iowait = _mean(last, "iowait")               # Linux only
        busy = _mean(last, "disk_busy_percent")      # Linux only
        latency = _mean(last, "disk_latency_ms")     # Windows + Linux
        wr = _mean(last, "disk_write_mb_sec") or 0.0
        rd = _mean(last, "disk_read_mb_sec") or 0.0
        full = last[-1]["disk_percent"]
        io_procs = sorted([p for p in (procs or []) if (p.get("io_mb_sec") or 0) > 0.5],
                          key=lambda p: p["io_mb_sec"], reverse=True)
        top = io_procs[0] if io_procs else None
        blocked = [p for p in (procs or []) if p["status"] == "disk-sleep"]
        issues = []
        reasons = []
        if iowait is not None and iowait >= config.IOWAIT_PERCENT:
            reasons.append(("I/O wait", iowait, "%", f">= {config.IOWAIT_PERCENT:.0f}%"))
        if busy is not None and busy >= config.DISK_BUSY_PERCENT:
            reasons.append(("Disk busy", busy, "%", f">= {config.DISK_BUSY_PERCENT:.0f}%"))
        if latency is not None and latency >= config.DISK_LATENCY_MS and (rd + wr) > 0.5:
            reasons.append(("Disk latency", latency, "ms/request", f">= {config.DISK_LATENCY_MS:.0f} ms"))
        heavy_n, heavy_span = _run_length(
            metrics, lambda m: (m.get("disk_read_mb_sec") or 0) + (m.get("disk_write_mb_sec") or 0)
            >= config.DISK_HEAVY_MB_SEC)
        if reasons or heavy_n >= config.DISK_HEAVY_SAMPLES:
            if reasons:
                metric, value, unit, thr = reasons[0]
                severity = "critical" if (iowait or 0) >= 2 * config.IOWAIT_PERCENT or (busy or 0) >= 98 or \
                    (latency or 0) >= 4 * config.DISK_LATENCY_MS else "warning"
                title = "Disk is the bottleneck (slow I/O)"
            else:
                metric, value, unit = "Disk throughput", rd + wr, "MB/s"
                thr = f">= {config.DISK_HEAVY_MB_SEC:.0f} MB/s for {config.DISK_HEAVY_SAMPLES} samples"
                severity = "warning"
                title = "Sustained heavy disk activity"
            who = f" Top I/O process: {top['name']} (PID {top['pid']}, {top['io_mb_sec']:.1f} MB/s)." if top else ""
            parts = [f"reading {rd:.1f} MB/s and writing {wr:.1f} MB/s"]
            if latency is not None:
                parts.append(f"{latency:.1f} ms per request")
            if iowait is not None:
                parts.append(f"I/O wait {iowait:.0f}%")
            if busy is not None:
                parts.append(f"disk busy {busy:.0f}%")
            issues.append(Issue(
                issue_type="disk_bottleneck", severity=severity, title=title,
                pid=top["pid"] if top else None, process=top["name"] if top else None,
                details="Disk is " + ", ".join(parts) + "." + who,
                evidence={"metric": metric, "value": round(float(value), 1), "unit": unit, "threshold": thr,
                          "duration_s": round(heavy_span) if not reasons else round(last[-1]["ts"] - last[0]["ts"]),
                          "iowait": None if iowait is None else round(iowait, 1),
                          "disk_busy_percent": None if busy is None else round(busy, 1),
                          "latency_ms": None if latency is None else round(latency, 1),
                          "read_mb_sec": round(rd, 1), "write_mb_sec": round(wr, 1),
                          "top_io_process": f"{top['name']}({top['pid']})" if top else None,
                          "blocked_processes": [f"{p['name']}({p['pid']})" for p in blocked[:8]]}))
        if full >= config.DISK_FULL_PERCENT:
            issues.append(Issue(
                issue_type="disk_bottleneck", severity="critical" if full >= 97 else "warning",
                title=f"Disk almost full ({full:.0f}%)",
                details=f"Only {last[-1]['disk_free_gb']:.1f} GB free on the system drive.",
                evidence={"metric": "Disk used", "value": full, "unit": "%",
                          "threshold": f">= {config.DISK_FULL_PERCENT:.0f}%", "duration_s": None,
                          "disk_percent": full, "disk_free_gb": last[-1]["disk_free_gb"], "kind": "space"}))
        return issues

    # ----------------------------------------------------------- ML anomaly
    def detect_anomaly(self, metrics):
        if IsolationForest is None or len(metrics) < config.ISOLATION_MIN_SAMPLES:
            return []
        # only features this OS really provides (columns that are all NULL are dropped, not zero-filled)
        feats = [f for f in IF_FEATURES if any(m.get(f) is not None for m in metrics)]
        X = np.array([[m.get(f) if m.get(f) is not None else 0.0 for f in feats] for m in metrics], dtype=float)
        # retrain every 50 new samples
        if self.iforest is None or len(metrics) - self._if_n >= 50 or len(metrics) < self._if_n \
                or getattr(self, "_if_feats", None) != feats:
            self.iforest = IsolationForest(n_estimators=150, contamination=config.ISOLATION_CONTAMINATION,
                                           random_state=42)
            self.iforest.fit(X[:-5] if len(X) > 70 else X)
            self._if_n = len(metrics)
            self._if_feats = feats
            self._if_trained_at = time.time()
        recent = X[-3:]
        preds = self.iforest.predict(recent)
        scores = self.iforest.decision_function(recent)
        if np.all(preds == -1):
            mu, sd = X[:-3].mean(axis=0), X[:-3].std(axis=0) + 1e-6
            z = (recent[-1] - mu) / sd
            top = np.argsort(-np.abs(z))[:3]
            drivers = {feats[i]: round(float(recent[-1][i]), 2) for i in top}
            return [Issue(
                issue_type="anomaly", severity="info",
                title="Unusual system behaviour detected",
                details=("The Isolation Forest model says the last few samples do not look like this machine's "
                         "normal behaviour. Biggest differences: " +
                         ", ".join(f"{k}={v}" for k, v in drivers.items())),
                evidence={"metric": "Anomaly score", "value": round(float(scores[-1]), 4), "unit": "",
                          "threshold": "Isolation Forest outlier (3 samples in a row)", "duration_s": None,
                          "anomaly_score": round(float(scores[-1]), 4), "drivers": drivers})]
        return []
