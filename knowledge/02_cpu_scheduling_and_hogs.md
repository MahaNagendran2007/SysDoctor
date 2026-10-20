# CPU Hogs and CPU Scheduling

A CPU hog is a process that keeps a CPU core busy close to 100% for a long time. Common reasons are
an infinite loop, a busy-wait (spinning while waiting instead of sleeping), a runaway background job,
or genuinely heavy work such as compiling or video encoding.

## How the Linux scheduler shares the CPU
Linux uses the Completely Fair Scheduler (CFS, replaced by EEVDF in kernel 6.6). Each runnable task
receives a share of CPU time weighted by its nice value. Nice values range from -20 (highest
priority) to 19 (lowest). A process with nice 19 still runs, but whenever other tasks want the CPU
they receive a much larger share. The load average counts tasks that are running or waiting to run.

## CPU starvation
When one or more hogs keep every core busy, other processes spend a long time in the run queue.
The desktop feels laggy, keyboard input is delayed and new programs start slowly. This is called
CPU starvation of the other processes.

## Process states
- R (running/runnable) - on a CPU or waiting in the run queue.
- S (interruptible sleep) - waiting for an event such as input.
- D (uninterruptible sleep) - waiting for I/O, usually disk.
- T (stopped) - paused by SIGSTOP or a debugger.
- Z (zombie) - finished but not yet reaped by its parent.

## Safe troubleshooting steps
1. Identify the process and check whether the high CPU is expected (a compile or a game is fine).
2. Lower its priority with `renice +10 -p PID` (or up to 19). This keeps it running but lets the rest of
   the system stay responsive. No data is lost.
3. If you need the CPU back immediately but want to keep the work, pause it with SIGSTOP
   (`kill -STOP PID`) and resume later with SIGCONT (`kill -CONT PID`).
4. If the process is stuck in a loop, end it with SIGTERM (`kill PID`) so it can clean up.
5. Use SIGKILL (`kill -9 PID`) only if SIGTERM does not work; the process gets no chance to save data.

## Useful commands
- `top -o %CPU` or `htop` shows the busiest processes.
- `ps -o pid,ni,pri,pcpu,stat,cmd -p PID` shows priority and state.
- `pidstat -u 2` shows per-process CPU every 2 seconds.
