# Signals and Safe Process Control

Signals are software interrupts delivered to processes. The safety order for acting on a process,
from least to most disruptive, is: observe, renice, pause, terminate, kill.

## Key signals
- SIGTERM (15): polite request to terminate. The program can save data and clean up. Default for `kill`.
- SIGKILL (9): immediate termination by the kernel. Cannot be caught or ignored. Unsaved data is lost
  and temporary files or locks may be left behind.
- SIGSTOP (19): pauses the process. Cannot be caught. The process keeps its memory.
- SIGCONT (18): resumes a stopped process.
- SIGCHLD (17): sent to a parent when a child stops or exits. Prompts the parent to reap children.
- SIGHUP (1): hang-up; many daemons reload configuration on SIGHUP.

## Priority (nice)
`renice +N -p PID` lowers a process's CPU priority. Normal users can only increase the nice value
(lower priority). Making a process more important (negative nice) requires root.

## Processes that must never be killed
PID 1 (systemd/init), kernel threads (kthreadd, kworker, ksoftirqd, migration), the display server
(Xorg, Wayland compositor, gnome-shell), login and session managers (systemd-logind, gdm), dbus-daemon,
NetworkManager, sshd when connected remotely, and on Windows: System, csrss.exe, wininit.exe,
winlogon.exe, services.exe, lsass.exe, smss.exe. Killing these can crash the desktop, log you out,
or halt the machine.

## Permissions
A normal user can signal only their own processes. Processes owned by root or other users require
sudo; a troubleshooting assistant should not escalate privileges automatically.
