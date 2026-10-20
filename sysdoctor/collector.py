"""Layer 1 - System Data Collector.

Samples CPU, memory, swap, paging, disk and per-process information every few seconds using
psutil plus Linux sources (/proc/vmstat, /proc/diskstats via psutil, iowait from /proc/stat).
"""
import os
import platform
import threading
import time

import psutil

from . import config
from .database import Database

SYSTEM = platform.system()
IS_LINUX = SYSTEM == "Linux"
IS_WINDOWS = SYSTEM == "Windows"
MB = 1024 * 1024

# Which metrics this operating system really provides. Anything False is stored as NULL (never a fake 0)
# and the dashboard shows "Available on Linux only".
CAPABILITIES = {
    "iowait": IS_LINUX,                   # /proc/stat iowait column
    "swap_activity": IS_LINUX,            # pswpin / pswpout from /proc/vmstat
    "major_faults": IS_LINUX,             # pgmajfault from /proc/vmstat
    "page_faults_total": IS_LINUX or IS_WINDOWS,   # Linux: vmstat, Windows: sum of per-process fault counters
    "disk_busy": IS_LINUX,                # busy_time from /proc/diskstats
    "disk_latency": True,                 # read_time/write_time are available on Windows and Linux
    "zombies": SYSTEM in ("Linux", "Darwin"),       # Windows has no zombie process state
    "process_io": IS_LINUX or IS_WINDOWS,
}
LINUX_ONLY_LABEL = "Available on Linux only"


def read_vmstat():
    """Return cumulative paging counters from /proc/vmstat (Linux only)."""
    wanted = {"pswpin", "pswpout", "pgmajfault", "pgfault"}
    out = {}
    if not IS_LINUX:
        return out
    try:
        with open("/proc/vmstat") as f:
            for line in f:
                key, _, val = line.partition(" ")
                if key in wanted:
                    out[key] = int(val)
    except OSError:
        pass
    return out


class Collector:
    def __init__(self, db: Database = None):
        self.db = db or Database()
        self._prev = None            # (ts, vmstat, disk_io, ctx)
        self._primed = False
        self.root_path = os.path.abspath(os.sep)
        self.own_pid = os.getpid()
        self._proc_faults = {}       # Windows: pid -> cumulative page faults
        self._faults_ts = None
        self._proc_io = {}           # (pid, create_time) -> (ts, read+write bytes)

    # -------------------------------------------------------------- system
    def _rates(self, now, vm, dio, ctx):
        rates = dict(swap_in_sec=None, swap_out_sec=None, major_faults_sec=None, minor_faults_sec=None,
                     disk_read_mb_sec=0.0, disk_write_mb_sec=0.0, disk_busy_percent=None,
                     disk_latency_ms=None, ctx_switches_sec=0.0)
        if self._prev:
            pts, pvm, pdio, pctx = self._prev
            dt = max(now - pts, 1e-6)
            if vm and pvm:
                rates["swap_in_sec"] = (vm.get("pswpin", 0) - pvm.get("pswpin", 0)) / dt
                rates["swap_out_sec"] = (vm.get("pswpout", 0) - pvm.get("pswpout", 0)) / dt
                maj = vm.get("pgmajfault", 0) - pvm.get("pgmajfault", 0)
                rates["major_faults_sec"] = maj / dt
                rates["minor_faults_sec"] = max(vm.get("pgfault", 0) - pvm.get("pgfault", 0) - maj, 0) / dt
            if dio and pdio:
                rates["disk_read_mb_sec"] = (dio.read_bytes - pdio.read_bytes) / MB / dt
                rates["disk_write_mb_sec"] = (dio.write_bytes - pdio.write_bytes) / MB / dt
                busy_ms = getattr(dio, "busy_time", None)
                pbusy_ms = getattr(pdio, "busy_time", None)
                if IS_LINUX and busy_ms is not None and pbusy_ms is not None:
                    rates["disk_busy_percent"] = min(100.0, (busy_ms - pbusy_ms) / (dt * 1000) * 100)
                # average time per I/O request (ms): works on Windows and Linux
                d_ops = (dio.read_count - pdio.read_count) + (dio.write_count - pdio.write_count)
                d_time = (getattr(dio, "read_time", 0) - getattr(pdio, "read_time", 0)) + \
                         (getattr(dio, "write_time", 0) - getattr(pdio, "write_time", 0))
                rates["disk_latency_ms"] = (d_time / d_ops) if d_ops > 0 else 0.0
            rates["ctx_switches_sec"] = (ctx - pctx) / dt
        self._prev = (now, vm, dio, ctx)
        return {k: (round(max(v, 0.0), 3) if v is not None else None) for k, v in rates.items()}

    def system_snapshot(self, now):
        cpu = psutil.cpu_percent(interval=None)
        times = psutil.cpu_times_percent(interval=None)
        iowait = getattr(times, "iowait", None) if CAPABILITIES["iowait"] else None
        try:
            load1 = psutil.getloadavg()[0]          # emulated on Windows by psutil
        except (AttributeError, OSError):
            load1 = None
        vmem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        disk = psutil.disk_usage(self.root_path)
        try:
            dio = psutil.disk_io_counters()
        except Exception:
            dio = None
        ctx = psutil.cpu_stats().ctx_switches
        rates = self._rates(now, read_vmstat(), dio, ctx)
        return {
            "ts": now,
            "cpu_percent": cpu, "load1": round(load1, 2) if load1 is not None else None,
            "iowait": round(iowait, 2) if iowait is not None else None,
            "mem_percent": vmem.percent, "mem_used_mb": round((vmem.total - vmem.available) / MB, 1),
            "mem_available_mb": round(vmem.available / MB, 1), "mem_total_mb": round(vmem.total / MB, 1),
            "swap_percent": swap.percent, "swap_used_mb": round(swap.used / MB, 1),
            "disk_percent": disk.percent, "disk_free_gb": round(disk.free / 1024 ** 3, 2),
            **rates,
        }

    # ------------------------------------------------------------ processes
    def process_snapshot(self, now=None):
        now = now or time.time()
        attrs = ["pid", "ppid", "name", "username", "status", "cpu_percent", "memory_info",
                 "memory_percent", "num_threads", "nice", "create_time", "cmdline"]
        if CAPABILITIES["process_io"]:
            attrs.append("io_counters")
        if IS_WINDOWS:
            attrs.append("num_handles")
        procs, counts = [], {"total": 0, "running": 0, "sleeping": 0, "zombie": 0, "threads": 0}
        faults_now, io_now = {}, {}
        for p in psutil.process_iter(attrs, ad_value=None):
            info = p.info
            counts["total"] += 1
            status = info.get("status") or "unknown"
            if status == psutil.STATUS_RUNNING:
                counts["running"] += 1
            elif status in (psutil.STATUS_SLEEPING, psutil.STATUS_IDLE, psutil.STATUS_DISK_SLEEP):
                counts["sleeping"] += 1
            elif status == psutil.STATUS_ZOMBIE:
                counts["zombie"] += 1
            counts["threads"] += info.get("num_threads") or 0
            mi = info.get("memory_info")
            if IS_WINDOWS and mi is not None and hasattr(mi, "num_page_faults"):
                faults_now[info["pid"]] = mi.num_page_faults
            io_rate = None
            ioc = info.get("io_counters")
            key = (info["pid"], info.get("create_time") or 0.0)
            if ioc is not None:
                total = ioc.read_bytes + ioc.write_bytes
                io_now[key] = (now, total)
                prev = self._proc_io.get(key)
                if prev and now > prev[0]:
                    io_rate = round(max(total - prev[1], 0) / MB / (now - prev[0]), 2)
            procs.append({
                "pid": info["pid"], "ppid": info.get("ppid") or 0,
                "name": info.get("name") or "?", "username": info.get("username") or "?",
                "status": status, "cpu_percent": round(info.get("cpu_percent") or 0.0, 1),
                "rss_mb": round(mi.rss / MB, 1) if mi else 0.0,
                "mem_percent": round(info.get("memory_percent") or 0.0, 2),
                "num_threads": info.get("num_threads") or 0, "nice": info.get("nice"),
                "open_files": info.get("num_handles") if IS_WINDOWS else None,
                "create_time": info.get("create_time") or 0.0,
                "cmdline": " ".join(info.get("cmdline") or [])[:300],
                "io_mb_sec": io_rate,
                "_proc": p,
            })
        self._proc_io = io_now
        # Windows page-fault rate = sum of per-process increases (processes that ended are ignored)
        page_faults_sec = None
        if IS_WINDOWS and faults_now:
            if self._faults_ts is not None and now > self._faults_ts:
                inc = sum(max(f - self._proc_faults[pid], 0) for pid, f in faults_now.items()
                          if pid in self._proc_faults)
                page_faults_sec = round(inc / (now - self._faults_ts), 1)
            self._proc_faults, self._faults_ts = faults_now, now
        counts["page_faults_sec"] = page_faults_sec
        # keep top N by CPU, memory and disk I/O, plus every zombie
        by_cpu = sorted(procs, key=lambda x: x["cpu_percent"], reverse=True)[:config.TOP_PROCESSES]
        by_mem = sorted(procs, key=lambda x: x["rss_mb"], reverse=True)[:config.TOP_PROCESSES]
        by_io = sorted(procs, key=lambda x: x["io_mb_sec"] or 0, reverse=True)[:10]
        zombies = [x for x in procs if x["status"] == psutil.STATUS_ZOMBIE]
        chosen = {x["pid"]: x for x in by_cpu + by_mem + by_io + zombies}
        for x in chosen.values():
            if IS_LINUX and x["status"] != psutil.STATUS_ZOMBIE:
                try:
                    x["open_files"] = len(x["_proc"].open_files())
                except (psutil.Error, OSError):
                    x["open_files"] = None
            x.pop("_proc", None)
        return list(chosen.values()), counts

    # ----------------------------------------------------------------- run
    def prime(self):
        """cpu_percent needs one call before it returns meaningful values."""
        psutil.cpu_percent(interval=None)
        psutil.cpu_times_percent(interval=None)
        for p in psutil.process_iter():
            try:
                p.cpu_percent(interval=None)
            except psutil.Error:
                pass
        self.system_snapshot(time.time())
        self._primed = True

    def collect_once(self):
        if not self._primed:
            self.prime()
            time.sleep(1.0)
        now = time.time()
        sysm = self.system_snapshot(now)
        procs, counts = self.process_snapshot(now)
        pf = counts["page_faults_sec"]
        if IS_LINUX and sysm["major_faults_sec"] is not None:
            pf = round(sysm["major_faults_sec"] + (sysm["minor_faults_sec"] or 0), 1)
        sysm.update(proc_total=counts["total"], proc_running=counts["running"],
                    proc_sleeping=counts["sleeping"],
                    proc_zombie=counts["zombie"] if CAPABILITIES["zombies"] else None,
                    thread_total=counts["threads"], page_faults_sec=pf)
        self.db.insert_metrics(sysm)
        self.db.insert_processes(now, procs)
        return sysm, procs


class CollectorThread(threading.Thread):
    """Background loop: collect -> detect -> predict, at a fixed interval."""

    def __init__(self, db, engine=None, interval=None):
        super().__init__(daemon=True, name="sysdoctor-collector")
        self.db = db
        self.collector = Collector(db)
        self.engine = engine            # MonitorEngine (detection + prediction), optional
        self.interval = interval or config.SAMPLE_INTERVAL_SEC
        self._stop = threading.Event()
        self.last_error = None
        self.samples = 0

    def run(self):
        last_prune = time.time()
        while not self._stop.is_set():
            start = time.time()
            try:
                self.collector.collect_once()
                self.samples += 1
                if self.engine:
                    self.engine.step()
                if time.time() - last_prune > 600:
                    self.db.prune()
                    last_prune = time.time()
                self.last_error = None
            except Exception as exc:          # keep monitoring alive whatever happens
                self.last_error = f"{type(exc).__name__}: {exc}"
            self._stop.wait(max(0.2, self.interval - (time.time() - start)))

    def stop(self):
        self._stop.set()
