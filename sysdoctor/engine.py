"""Runs detection + prediction on the latest data and turns new findings into alerts."""
import time

import psutil

from . import config
from .database import Database
from .detector import Detector
from .predictor import predict, predictions_to_issues


class MonitorEngine:
    def __init__(self, db: Database):
        self.db = db
        self.detector = Detector()
        self.cpu_count = psutil.cpu_count() or 1
        self.latest_issues = []
        self.latest_predictions = []

    def _proc_snapshots(self, n):
        rows = self.db.query("SELECT DISTINCT ts FROM processes ORDER BY ts DESC LIMIT ?", (n,))
        stamps = sorted(r["ts"] for r in rows)
        if not stamps:
            return []
        data = self.db.query("SELECT * FROM processes WHERE ts >= ? ORDER BY ts", (stamps[0],))
        snaps = {}
        for r in data:
            snaps.setdefault(r["ts"], []).append(r)
        return [snaps[t] for t in stamps if t in snaps]

    def analyse(self):
        """Run every detector and return (issues, predictions) without saving alerts."""
        metrics = self.db.recent_metrics(400)
        if not metrics:
            return [], []
        snaps = self._proc_snapshots(max(config.CPU_HOG_SAMPLES * 4, 20))   # enough to measure duration
        procs = snaps[-1] if snaps else []
        leak_window = config.SAMPLE_INTERVAL_SEC * max(config.LEAK_MIN_SAMPLES * 3, 60)
        proc_hist = self.db.process_history_since(time.time() - leak_window)

        d = self.detector
        issues = []
        issues += d.detect_memory_leaks(proc_hist)
        issues += d.detect_high_memory(metrics, procs)
        issues += d.detect_cpu_hogs(snaps, self.cpu_count, metrics[-1]["cpu_percent"])
        issues += d.detect_thrashing(metrics)
        issues += d.detect_zombies(procs)
        issues += d.detect_disk_bottleneck(metrics, procs)
        preds = predict(metrics)
        issues += predictions_to_issues(preds)
        if not issues:                      # ML check only adds value when rules found nothing
            issues += d.detect_anomaly(metrics)
        return issues, preds

    def step(self):
        issues, preds = self.analyse()
        for p in preds:
            self.db.add_prediction(p)
        new = []
        now = time.time()
        for issue in issues:
            last = self.db.last_alert_of(issue.issue_type, issue.pid)
            if last and last["status"] in ("open", "in_progress"):
                issue.alert_id = last["id"]
                continue
            if last and now - last["ts"] < config.ALERT_COOLDOWN_SEC:
                continue
            issue.alert_id = self.db.add_alert(issue)
            new.append(issue)
        # auto-resolve open alerts whose condition has disappeared
        active = {(i.issue_type, i.pid) for i in issues}
        for a in self.db.open_alerts():
            if (a["issue_type"], a["pid"]) not in active and a["status"] == "open" and now - a["ts"] > 30:
                self.db.set_alert_status(a["id"], "cleared")
        self.latest_issues, self.latest_predictions = issues, preds
        return new
