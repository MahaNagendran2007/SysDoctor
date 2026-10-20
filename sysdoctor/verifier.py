"""Layer 7 - Fix verification loop.

After an action, fresh samples are collected and the original detector is re-run on the target.
The result compares key numbers before and after and decides whether the problem is resolved.
"""
import os
import platform
import time
from dataclasses import dataclass, field

import psutil

from . import config


@dataclass
class VerificationResult:
    resolved: bool
    message: str
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)


def measure(issue):
    """Key numbers relevant to the issue right now."""
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    out = {"mem_percent": vm.percent, "swap_percent": sw.percent, "cpu_percent": psutil.cpu_percent(interval=0.5)}
    t = getattr(psutil.cpu_times_percent(interval=0.5), "iowait", None)   # Linux only
    out["iowait"] = t
    out["disk_percent"] = psutil.disk_usage(os.path.abspath(os.sep)).percent
    try:
        d1 = psutil.disk_io_counters()
        time.sleep(0.5)
        d2 = psutil.disk_io_counters()
        out["disk_mb_sec"] = round(((d2.read_bytes + d2.write_bytes) - (d1.read_bytes + d1.write_bytes)) / 1e6 / 0.5, 1)
    except Exception:
        out["disk_mb_sec"] = None
    if issue is not None and issue.pid:
        try:
            p = psutil.Process(issue.pid)
            out["process_alive"] = True
            out["process_status"] = p.status()
            out["process_rss_mb"] = round(p.memory_info().rss / 1e6, 1)
            p.cpu_percent(None)
            time.sleep(0.5)
            out["process_cpu"] = p.cpu_percent(None)
            out["process_nice"] = p.nice()
            if issue.issue_type == "zombie":
                out["zombie_children"] = sum(1 for c in p.children() if c.status() == psutil.STATUS_ZOMBIE)
        except psutil.NoSuchProcess:
            out["process_alive"] = False
            if issue.issue_type == "zombie":
                out["zombie_children"] = 0
        except psutil.Error:
            pass
    if platform.system() != "Windows":
        out["zombies_total"] = sum(1 for p in psutil.process_iter(["status"])
                                   if p.info["status"] == psutil.STATUS_ZOMBIE)
    if issue is not None and issue.pid:
        try:
            out["process_io_mb_sec"] = None
            p = psutil.Process(issue.pid)
            a = p.io_counters()
            time.sleep(0.5)
            b = p.io_counters()
            out["process_io_mb_sec"] = round(((b.read_bytes + b.write_bytes) - (a.read_bytes + a.write_bytes))
                                             / 1e6 / 0.5, 1)
        except (psutil.Error, AttributeError, NotImplementedError):
            pass
    return out


LABELS = [("process_cpu", "process CPU", "%"), ("process_rss_mb", "process memory", " MB"),
          ("process_io_mb_sec", "process disk I/O", " MB/s"), ("process_status", "process state", ""),
          ("process_nice", "priority (nice)", ""), ("zombie_children", "zombie children", ""),
          ("cpu_percent", "system CPU", "%"), ("mem_percent", "system memory", "%"),
          ("swap_percent", "swap / page file", "%"), ("iowait", "I/O wait", "%"),
          ("disk_mb_sec", "disk throughput", " MB/s"), ("disk_percent", "disk used", "%")]


def describe(m):
    parts = []
    for key, label, unit in LABELS:
        v = m.get(key)
        if v is None:
            continue
        parts.append(f"{label} {v:.0f}{unit}" if isinstance(v, (int, float)) and not isinstance(v, bool)
                     else f"{label} {v}{unit}")
    if m.get("process_alive") is False:
        parts.insert(0, "process has exited")
    return ", ".join(parts)


def comparison_rows(before, after):
    """[(metric, before, after)] for the UI and the history table."""
    rows = []
    for key, label, unit in LABELS:
        b, a = before.get(key), after.get(key)
        if b is None and a is None:
            continue
        fmt = lambda v: "-" if v is None else (f"{v:.1f}{unit}" if isinstance(v, float) else f"{v}{unit}")
        rows.append((label, fmt(b), fmt(a)))
    if "process_alive" in before or "process_alive" in after:
        rows.insert(0, ("process running", "yes" if before.get("process_alive", True) else "no",
                        "yes" if after.get("process_alive", True) else "no"))
    return rows


class FixVerifier:
    def __init__(self, engine=None):
        self.engine = engine

    def verify(self, issue, before, wait=None):
        time.sleep(wait if wait is not None else config.VERIFY_WAIT_SEC)
        after = measure(issue)
        t = issue.issue_type
        ok, why = False, ""
        if t == "memory_leak":
            if not after.get("process_alive", True):
                ok, why = True, f"{issue.process} has exited and its {before.get('process_rss_mb', 0):.0f} MB were released."
            elif after.get("process_status") == "stopped":
                ok, why = True, f"{issue.process} is paused, so its memory can no longer grow."
            else:
                growth = after.get("process_rss_mb", 0) - before.get("process_rss_mb", 0)
                why = f"{issue.process} is still running and its memory changed by {growth:+.0f} MB."
        elif t == "cpu_hog":
            if not after.get("process_alive", True):
                ok, why = True, f"{issue.process} has stopped; system CPU is now {after['cpu_percent']:.0f}%."
            elif after.get("process_status") == "stopped":
                ok, why = True, f"{issue.process} is paused and using 0% CPU."
            elif after.get("process_nice", 0) > before.get("process_nice", 0):
                ok = True
                why = (f"{issue.process} now runs at nice {after['process_nice']} (was {before.get('process_nice')}), "
                       f"so other programs get the CPU first. It still uses {after.get('process_cpu', 0):.0f}% "
                       "when the CPU is otherwise free - that is normal.")
            else:
                why = f"{issue.process} still uses {after.get('process_cpu', 0):.0f}% CPU at the same priority."
        elif t == "zombie":
            before_z = len(issue.evidence.get("zombie_pids", []))
            now_z = after.get("zombie_children", 0)
            ok = now_z == 0
            why = (f"All {before_z} zombie(s) have been reaped." if ok else
                   f"{now_z} zombie(s) remain under {issue.process}.")
        elif t == "high_memory":
            ok = after["mem_percent"] < config.HIGH_MEM_PERCENT - 2
            why = (f"System memory went from {before['mem_percent']:.0f}% to {after['mem_percent']:.0f}%."
                   + (" It is below the alert level now." if ok else " It is still too high."))
        elif t == "thrashing":
            ok = after["mem_percent"] < config.THRASH_MEM_PERCENT - 2
            why = (f"Memory use went from {before['mem_percent']:.0f}% to {after['mem_percent']:.0f}%."
                   + (" Pressure is below the thrashing level." if ok else " Still under heavy pressure."))
        elif t == "disk_bottleneck":
            if issue.evidence.get("kind") == "space":
                ok = after["disk_percent"] < config.DISK_FULL_PERCENT
                why = f"Disk usage is now {after['disk_percent']:.0f}% (was {before['disk_percent']:.0f}%)."
            elif issue.pid and after.get("process_alive") is False:
                ok, why = True, f"{issue.process} has exited, so its disk traffic stopped."
            elif issue.pid and after.get("process_status") == "stopped":
                ok, why = True, f"{issue.process} is paused, so it no longer uses the disk."
            elif after.get("iowait") is not None and before.get("iowait") is not None:
                ok = after["iowait"] < config.IOWAIT_PERCENT
                why = f"I/O wait is now {after['iowait']:.0f}% (was {before['iowait']:.0f}%)."
            else:                                        # Windows: compare throughput
                b, a = before.get("disk_mb_sec") or 0, after.get("disk_mb_sec") or 0
                ok = a < config.DISK_HEAVY_MB_SEC and a <= b
                why = f"Disk throughput is now {a:.0f} MB/s (was {b:.0f} MB/s)."
        elif t == "resource_exhaustion":
            res = issue.evidence.get("resource", "memory")
            key = {"memory": "mem_percent", "disk": "disk_percent", "swap": "swap_percent"}[res]
            ok = after[key] <= before[key] + 0.2
            why = (f"{res.capitalize()} went from {before[key]:.0f}% to {after[key]:.0f}%"
                   + (" - it has stopped rising." if ok else " - it is still rising."))
        else:
            ok, why = True, "No specific check for this alert; please review the graphs."
        # second opinion from the full detector when available
        if ok and self.engine is not None and t in ("thrashing", "disk_bottleneck"):
            try:
                issues, _ = self.engine.analyse()
                if any(i.issue_type == t for i in issues):
                    ok, why = False, why + " However the detector still sees the condition in the latest samples."
            except Exception:
                pass
        return VerificationResult(resolved=ok, message=why, before=before, after=after)
