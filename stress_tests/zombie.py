"""Creates zombie processes (Linux/macOS): the parent forks children that exit immediately
but never calls wait(), so they remain as <defunct> entries.

    python zombie.py --count 5
SysDoctor should suggest SIGCHLD to this parent; this script reaps on SIGCHLD only if --reap-on-signal
is given (to demonstrate a well-behaved parent), otherwise terminating the parent is required.
"""
import argparse
import os
import signal
import sys
import time

if not hasattr(os, "fork"):
    sys.exit("Zombie processes are a Unix concept - run this inside Linux.")

ap = argparse.ArgumentParser()
ap.add_argument("--count", type=int, default=5)
ap.add_argument("--reap-on-signal", action="store_true")
a = ap.parse_args()

signal.signal(signal.SIGCHLD, signal.SIG_DFL)    # default: do NOT auto-reap


def reap(signum, frame):
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return
        print(f"reaped {pid}", flush=True)


print("Parent PID", os.getpid(), flush=True)
children = []
for _ in range(a.count):
    pid = os.fork()
    if pid == 0:
        os._exit(0)                            # child exits immediately
    children.append(pid)
for pid in children:                           # wait until each child has exited WITHOUT reaping it
    os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)
    print("child", pid, "exited -> zombie", flush=True)

if a.reap_on_signal:
    # install the handler only after the children are already zombies, so they stay
    # defunct until SysDoctor sends SIGCHLD
    signal.signal(signal.SIGCHLD, reap)
try:
    while True:
        time.sleep(1)                          # parent keeps running and never calls wait()
except KeyboardInterrupt:
    pass
