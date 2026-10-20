# Running Out of Resources and the OOM Killer

## Out-Of-Memory killer
When RAM and swap are both exhausted and the kernel cannot reclaim pages, the OOM killer selects a
process (usually the one with the highest oom_score, which is roughly the one using the most memory)
and kills it with SIGKILL. Messages appear in `dmesg` or `journalctl -k` as "Out of memory: Killed
process". Because the victim has no chance to save data, it is much better to act before this happens.

## Predicting exhaustion
If a resource grows at a roughly constant rate, a straight line fitted to recent samples (linear
regression) estimates when it reaches its limit: time_left = (limit - current) / growth_rate. The
estimate is only trustworthy when the trend is consistent (high R squared) and should be refreshed
continuously because usage patterns change.

## Disk space exhaustion
When a file system reaches 100%, programs fail to write files, logs stop, databases may corrupt, and
the desktop may refuse to log in. Keep at least 10% free. Common space consumers: logs in /var/log,
package caches, the trash, downloads, old snapshots and core dumps.

## Early actions
- Memory: close unused applications, restart leaking programs, pause heavy background jobs.
- Disk: delete temporary and cached files, move large files elsewhere, stop runaway writers.
- Swap: rising swap together with falling available memory is the early sign of thrashing.
