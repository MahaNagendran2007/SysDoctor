# Thrashing, Paging and Swap

## Virtual memory and paging
Processes use virtual addresses. The MMU translates them to physical frames using page tables.
When a process touches a page that is not in RAM, a page fault occurs. A minor fault is resolved
without disk access (the page is already in memory, for example in the page cache). A major fault
needs to read the page from disk or swap, which is thousands of times slower than RAM.

## Swap
When RAM is short, the kernel moves rarely used anonymous pages to swap space on disk (swap-out) and
brings them back when needed (swap-in). The counters pswpin and pswpout in /proc/vmstat, and the si
and so columns of vmstat, show this activity.

## What thrashing is
Thrashing happens when the total working set of the running processes - the pages they actively need -
is larger than physical RAM. Pages are swapped out and almost immediately needed again. The system
spends most of its time moving pages between RAM and disk instead of doing useful work. Symptoms:
- Very high memory use and rising swap use.
- Continuous swap-in and swap-out and many major page faults per second.
- High I/O wait; CPU utilisation for useful work drops even though the machine feels frozen.
- Mouse and keyboard respond very slowly.

This is the classic result from OS theory: as the degree of multiprogramming rises past a point,
CPU utilisation collapses because of page-fault overhead. The working-set model and page-fault
frequency control are the textbook solutions - reduce the number of active processes until the
working sets fit in memory.

## Safe troubleshooting steps
1. Find the processes with the largest resident memory (they are usually the cause).
2. Close applications you do not need, starting with the biggest non-critical memory user.
3. If a single process is growing without limit, treat it as a memory leak and restart it.
4. Pausing (SIGSTOP) a large background job can immediately free the pressure while keeping it
   resumable later; its pages can then be swapped out once instead of repeatedly.
5. Long term: add RAM, reduce the number of simultaneous heavy programs, or tune vm.swappiness.

## Useful commands
- `vmstat 2` (watch si, so, wa columns).
- `free -h` shows RAM and swap.
- `sar -B 2` shows paging statistics including major faults.
