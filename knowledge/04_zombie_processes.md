# Zombie Processes

## Process creation and termination
On Unix-like systems a parent creates a child with fork() (and often exec() to run a new program).
When the child finishes with exit(), the kernel frees almost all of its resources but keeps a small
entry in the process table containing its PID and exit status. The parent is expected to read this
status with wait() or waitpid(). This is called reaping.

## What a zombie is
A zombie (state Z, shown as <defunct> in ps) is a child that has exited but has not yet been reaped.
It uses no CPU and no memory apart from the process-table entry, so a few zombies are harmless.
Many zombies indicate a bug in the parent program, and in extreme cases they can exhaust the
maximum number of PIDs (see /proc/sys/kernel/pid_max), preventing new processes from starting.

## Important: a zombie cannot be killed
Sending SIGKILL to a zombie does nothing because it is already dead. The fix always involves the
parent process:
1. Send SIGCHLD to the parent (`kill -s SIGCHLD PPID`). A correctly written parent that handles
   SIGCHLD will call wait() and reap its children.
2. If that does not work, restart or terminate the parent (SIGTERM). When the parent exits, the
   zombies are re-parented to init/systemd (PID 1), which reaps them automatically.
3. Never terminate PID 1 or other critical system processes.

## Orphan processes
An orphan is a still-running child whose parent has exited. Orphans are adopted by init/systemd and
are not a problem. Zombies are the opposite case: dead child, living parent that ignores it.

## Preventing zombies (for developers)
- Call wait()/waitpid() for each child, or install a SIGCHLD handler that loops on waitpid(-1, ..., WNOHANG).
- Or set SIGCHLD to SIG_IGN so the kernel reaps children automatically.

## Useful commands
- `ps -eo pid,ppid,stat,cmd | awk '$3 ~ /Z/'` lists zombies and their parents.
- `ps -o pid,cmd -p PPID` shows the parent.
