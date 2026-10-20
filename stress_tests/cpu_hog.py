"""Controlled CPU hog demo: a busy loop on one core (or more). Stops by itself.

    python stress_tests/cpu_hog.py                 # 1 core, stops after 180 s
    python stress_tests/cpu_hog.py --cores 2 --seconds 120
Stop early with Ctrl+C (or the Stop button in the dashboard). It only burns CPU: no files, no memory.
"""
import argparse
import multiprocessing as mp
import os
import time


def spin(deadline):
    x = 0
    while time.time() < deadline:      # busy-wait: never sleeps, never yields voluntarily
        x = (x * 31 + 7) % 1000003


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cores", type=int, default=1)
    ap.add_argument("--seconds", type=int, default=180)
    a = ap.parse_args()
    cores = max(1, min(a.cores, (os.cpu_count() or 2) - 1 or 1))   # always leave one core free
    deadline = time.time() + a.seconds
    print("PID", os.getpid(), f"- busy on {cores} core(s) for {a.seconds} s", flush=True)
    workers = [mp.Process(target=spin, args=(deadline,), daemon=True) for _ in range(cores - 1)]
    for w in workers:
        w.start()
    try:
        spin(deadline)
    except KeyboardInterrupt:
        pass
    print("CPU demo finished", flush=True)
