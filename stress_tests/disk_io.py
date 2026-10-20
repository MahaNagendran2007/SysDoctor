"""Controlled disk-pressure demo: repeatedly writes and syncs ONE temporary file, then deletes it.

    python stress_tests/disk_io.py                  # 256 MB file rewritten in a loop, stops after 120 s
    python stress_tests/disk_io.py --mb 128 --seconds 60
Safety: writes only its own file (sysdoctor_disk_stress.bin in the temp folder), never touches other files,
refuses to run if less than 2 GB would remain free, and deletes the file when it stops.
"""
import argparse
import os
import shutil
import tempfile
import time

ap = argparse.ArgumentParser()
ap.add_argument("--mb", type=int, default=256, help="size of the temp file in MB (max 1024)")
ap.add_argument("--dir", default=tempfile.gettempdir())
ap.add_argument("--seconds", type=int, default=120)
a = ap.parse_args()
size_mb = max(16, min(a.mb, 1024))
path = os.path.join(a.dir, "sysdoctor_disk_stress.bin")
if shutil.disk_usage(a.dir).free / 2**20 < size_mb + 2048:
    raise SystemExit("Not enough free disk space for a safe demo (needs the file size + 2 GB).")
chunk = os.urandom(4 * 1024 * 1024)
end = time.time() + a.seconds
print("PID", os.getpid(), f"- writing {size_mb} MB passes to {path} for {a.seconds} s", flush=True)
try:
    while time.time() < end:
        with open(path, "wb") as f:
            for _ in range(size_mb // 4):
                f.write(chunk)
                f.flush()
                os.fsync(f.fileno())           # force every block to the device
                if time.time() >= end:
                    break
except KeyboardInterrupt:
    pass
finally:
    if os.path.exists(path):
        os.remove(path)
print("disk demo finished - temp file removed", flush=True)
