# Memory Leaks

A memory leak happens when a program keeps allocating memory (with malloc, new, or by growing
lists and caches in higher-level languages) but never releases memory it no longer needs. The
operating system cannot reclaim that memory because, from its point of view, the process still owns
it. Over time the process's resident set size (RSS) climbs steadily until RAM runs short.

## How a leak looks in monitoring data
- The process's RSS rises in a nearly straight line over many minutes and never drops back.
- Short spikes that return to a baseline are normal (buffers, temporary work) and are not leaks.
- System-wide available memory falls, then swap usage begins to rise.
- If nothing is done, the kernel eventually swaps heavily (thrashing) or the Out-Of-Memory (OOM)
  killer terminates a process.

## Virtual memory background
Each process has its own virtual address space divided into pages (usually 4 KB). The heap grows
with brk/sbrk or mmap. RSS counts only pages currently held in physical RAM. VSZ (virtual size)
counts everything mapped, even pages never touched, so RSS is the better signal for leaks.

## Common causes
- Objects added to a global list, dictionary or cache and never removed.
- Forgetting free() / delete for every allocation path, especially on error paths.
- Event listeners or callbacks registered repeatedly and never unregistered.
- Reference cycles in garbage-collected languages that keep objects alive.

## Safe troubleshooting steps
1. Confirm the trend: watch the process RSS for a few minutes (top, ps -o pid,rss,cmd -p PID).
2. Check whether the application is supposed to use that much memory (for example a database cache).
3. Save the user's work in that application if it is interactive.
4. Restart the application gracefully (close it normally, or send SIGTERM) - this releases all memory.
5. If it does not respond to SIGTERM within a few seconds, SIGKILL can be used as a last resort.
6. For a long-term fix, report the bug or update the program; developers can use valgrind, heaptrack
   or Python's tracemalloc to find the leaking allocation.

## Useful commands
- `ps -o pid,ppid,rss,vsz,cmd --sort=-rss | head` lists the biggest memory users.
- `cat /proc/PID/status | grep -E 'VmRSS|VmSwap'` shows resident and swapped memory for one process.
- `pmap -x PID` shows the memory mappings of a process.
