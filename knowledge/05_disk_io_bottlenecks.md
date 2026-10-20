# Disk I/O Bottlenecks and Full Disks

## I/O wait
iowait is the percentage of time a CPU was idle while at least one process was waiting for disk I/O.
High iowait (above about 20%) means the storage device cannot keep up with requests. Processes waiting
for disk are in state D (uninterruptible sleep) and cannot be interrupted, even by SIGKILL, until the
I/O finishes.

## Why disks become a bottleneck
- One program performing huge sequential writes (copying, backups, logging, downloads).
- Many small random reads/writes, which are especially slow on hard disks.
- Heavy swapping (see thrashing) - swap traffic competes with normal file I/O.
- A nearly full disk: file systems slow down and applications fail when free space reaches zero.

## Reading iostat
`iostat -x 2` shows per-device statistics: r/s and w/s (requests), rkB/s and wkB/s (throughput),
await (average time a request waits, in ms) and %util (percentage of time the device was busy).
%util near 100% with high await means the device is saturated.

## The page cache and writeback
Linux buffers writes in RAM (dirty pages) and writes them to disk later. When too many dirty pages
accumulate, writers are throttled and the flush threads (kworker/flush) cause bursts of disk
activity. The sync command forces dirty pages to disk.

## Safe troubleshooting steps
1. Find which process is doing the I/O (`iotop -o`, or `pidstat -d 2`).
2. If it is a background job, lower its I/O priority with `ionice -c3 -p PID` (idle class) and its
   CPU priority with renice.
3. Pause the job (SIGSTOP) if the system must be responsive right now, and resume later (SIGCONT).
4. If the disk is almost full: remove temporary files, empty the trash, clear package caches
   (`sudo apt clean`), and find large files with `du -sh ~/* | sort -h`.
5. Stop a runaway writer (for example a program filling a log file) with SIGTERM.

## Useful commands
- `df -h` shows free space per file system.
- `iostat -x 2`, `iotop -o`, `vmstat 2` (wa column).
