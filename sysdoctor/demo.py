"""Starts / stops the controlled demonstration programs in stress_tests/ from the dashboard.

Only these fixed commands can be launched - nothing is built from user or LLM text. Stop only ever
terminates processes this manager started itself.
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import config

ST = config.BASE_DIR / "stress_tests"
DEMOS = {
    "cpu": {"label": "CPU hog", "args": [str(ST / "cpu_hog.py"), "--seconds", "180"],
            "expect": "CPU hog alert after ~15-20 s (5 samples at 3 s)"},
    "memory": {"label": "Memory growth", "args": [str(ST / "memory_leak.py"), "--rate", "20", "--seconds", "180"],
               "expect": "Memory-growth alert after ~40-60 s, and a rising memory prediction"},
    "disk": {"label": "Disk pressure", "args": [str(ST / "disk_io.py"), "--mb", "256", "--seconds", "120"],
             "expect": "Disk alert when throughput stays above "
                       f"{config.DISK_HEAVY_MB_SEC:.0f} MB/s (or latency above {config.DISK_LATENCY_MS:.0f} ms)"},
}


class DemoManager:
    def __init__(self):
        self.procs = {}          # name -> (Popen, start_ts)

    def start(self, name):
        if name not in DEMOS:
            raise ValueError(name)
        if self.running(name):
            return self.procs[name][0].pid
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        p = subprocess.Popen([sys.executable] + DEMOS[name]["args"], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, creationflags=flags)
        self.procs[name] = (p, time.time())
        return p.pid

    def running(self, name):
        entry = self.procs.get(name)
        return bool(entry and entry[0].poll() is None)

    def pid(self, name):
        return self.procs[name][0].pid if self.running(name) else None

    def stop(self, name):
        entry = self.procs.pop(name, None)
        if entry and entry[0].poll() is None:
            entry[0].terminate()
            try:
                entry[0].wait(5)
            except subprocess.TimeoutExpired:
                entry[0].kill()
        if name == "disk":     # on Windows terminate() skips the script's cleanup, so remove its temp file
            f = Path(tempfile.gettempdir()) / "sysdoctor_disk_stress.bin"
            for _ in range(5):
                try:
                    if f.exists():
                        f.unlink()
                    break
                except OSError:
                    time.sleep(0.5)

    def stop_all(self):
        for n in list(self.procs):
            self.stop(n)
