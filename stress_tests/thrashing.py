"""Controlled memory pressure to study thrashing. ONLY run inside a VM with swap enabled.

Allocates a working set larger than free RAM and repeatedly touches every page, forcing the kernel to
swap pages in and out. A safety cap keeps it to --fraction of (RAM + swap).

    python thrashing.py --fraction 1.15      # working set = 115% of currently available RAM
"""
import argparse
import os
import time

import psutil

ap = argparse.ArgumentParser()
ap.add_argument("--fraction", type=float, default=1.15)
ap.add_argument("--seconds", type=int, default=120)
a = ap.parse_args()

vm, sw = psutil.virtual_memory(), psutil.swap_memory()
target = int(vm.available * a.fraction)
cap = int((vm.available + sw.free) * 0.8)
size = min(target, cap)
if sw.total == 0:
    print("No swap configured - this would trigger the OOM killer instead of thrashing. Aborting.")
    raise SystemExit(1)
print(f"PID {os.getpid()} allocating {size/2**20:.0f} MB (available RAM {vm.available/2**20:.0f} MB, "
      f"free swap {sw.free/2**20:.0f} MB)", flush=True)
blocks = []
for _ in range(size // (64 * 2**20)):
    blocks.append(bytearray(64 * 2**20))
end = time.time() + a.seconds
while time.time() < end:
    for b in blocks:                            # touch each page -> working set > RAM -> swapping
        for i in range(0, len(b), 4096):
            b[i] = (b[i] + 1) & 0xFF
print("done")
