"""Controlled memory-growth demo: keeps allocating memory and never frees it (a simulated leak).

    python stress_tests/memory_leak.py                     # +20 MB/s, capped, stops after 180 s
    python stress_tests/memory_leak.py --rate 15 --max 1200
Safety: the total is capped at --max MB AND at 25% of the RAM that is free when it starts, so it cannot
push the machine into trouble. All memory is released when it exits (Ctrl+C or the dashboard Stop button).
"""
import argparse
import os
import time

import psutil

ap = argparse.ArgumentParser()
ap.add_argument("--rate", type=float, default=20, help="MB leaked per second")
ap.add_argument("--max", type=float, default=1500, help="upper cap in MB")
ap.add_argument("--seconds", type=int, default=180)
a = ap.parse_args()

cap = int(min(a.max, psutil.virtual_memory().available / 2**20 * 0.25))
leak = []                                   # the "forgotten" cache that is never cleared
end = time.time() + a.seconds
print(f"PID {os.getpid()} - leaking {a.rate} MB/s up to {cap} MB for {a.seconds} s", flush=True)
last = 0
try:
    while time.time() < end:
        if len(leak) < cap:
            block = bytearray(1024 * 1024)
            for i in range(0, len(block), 4096):        # touch every page so it is really resident
                block[i] = 1
            leak.append(block)
        time.sleep(1.0 / a.rate)
        if time.time() - last >= 5:
            print(f"{len(leak)} MB held", flush=True)
            last = time.time()
except KeyboardInterrupt:
    pass
print("memory demo finished - memory released", flush=True)
