# Linux Monitoring Sources and Tools

## /proc file system
- /proc/stat - cumulative CPU time in user, nice, system, idle, iowait, irq, softirq modes; context switches.
- /proc/meminfo - MemTotal, MemAvailable, Buffers, Cached, SwapTotal, SwapFree.
- /proc/vmstat - pgfault, pgmajfault, pswpin, pswpout and many more paging counters.
- /proc/loadavg - load averages for 1, 5 and 15 minutes.
- /proc/PID/status - per-process state, VmRSS, VmSwap, Threads, voluntary context switches.
- /proc/PID/fd - open file descriptors of the process.

## Tools
- top / htop: live list of processes with CPU, memory, state and priority.
- vmstat: r (run queue), b (blocked), si/so (swap), bi/bo (block I/O), us/sy/id/wa CPU split.
- iostat: per-device throughput, await and utilisation.
- free: RAM and swap summary. `available` is the best estimate of memory usable without swapping.
- ps: snapshot of processes; `ps aux --sort=-%mem`.
- uptime: load average.

## Interpreting memory numbers
"Used" memory on Linux includes page cache that the kernel frees instantly when needed. The
MemAvailable value is what really matters. High cache usage is healthy; low MemAvailable with rising
swap is the real warning sign.

## Load average
Load average counts runnable tasks plus tasks in uninterruptible sleep. A load equal to the number of
CPU cores means the machine is fully used; much higher values indicate queuing. High load with low
CPU usage often means processes are stuck waiting for disk (state D).
