"""Grounded default plans for each issue type.

They are used (a) as a reference the LLM sees in its prompt, (b) as the complete answer when no
LLM is reachable (offline mode), and (c) to repair LLM output that is missing valid actions.
"""
from .models import Action, Step

CONCEPTS = {
    "memory_leak": "Memory leak - heap/RSS growth without release; virtual memory and paging",
    "high_memory": "Memory management - physical memory (RAM) vs. virtual memory, paging and the page file / swap",
    "cpu_hog": "CPU scheduling - CFS fair sharing, nice priority and CPU starvation",
    "thrashing": "Thrashing - working set larger than RAM, excessive paging/swapping",
    "zombie": "Process lifecycle - exit(), wait() and reaping of zombie (defunct) children",
    "disk_bottleneck": "I/O scheduling - iowait, uninterruptible sleep (D state), disk saturation",
    "anomaly": "System behaviour baseline - statistical anomaly detection",
    "resource_exhaustion": "Resource exhaustion - OOM killer, disk full, capacity planning",
}

SIMPLE_CAUSE = {
    "memory_leak": ("{proc} keeps taking more memory and never gives it back. The OS cannot reuse memory "
                    "a program still holds, so free RAM keeps shrinking."),
    "high_memory": ("RAM has been almost full for a while. When RAM runs out the OS starts moving memory pages "
                    "to the page file (swap) on disk, which is much slower, so programs become sluggish."),
    "cpu_hog": ("{proc} is keeping a CPU core busy almost all the time, so other programs have to wait "
                "their turn in the scheduler's queue. That is why things feel slow."),
    "thrashing": ("Programs together need more memory than the RAM you have, so the OS keeps moving pages "
                  "between RAM and the disk. It spends its time copying instead of working, so everything freezes."),
    "zombie": ("{proc} started child processes that have finished, but it never collected their exit status "
               "with wait(). The finished children stay in the process table as zombies."),
    "disk_bottleneck": ("The disk cannot keep up with the reads and writes being asked of it, so programs are "
                        "left waiting for it (I/O wait) and the system feels stuck."),
    "anomaly": ("The monitoring model noticed a combination of values that is unusual for this computer. "
                "It is an early hint to take a look, not necessarily a fault."),
    "resource_exhaustion": ("A resource is filling up at a steady rate. If the trend continues it will run out, "
                            "and programs may crash or be killed by the OS."),
}


def _top_memory_proc(evidence_procs):
    return evidence_procs[0] if evidence_procs else None


def default_steps(issue, context=None):
    """Return an ordered list of Steps for an issue. context may include 'top_mem' and 'top_io' lists."""
    ctx = context or {}
    pid, proc = issue.pid, issue.process
    t = issue.issue_type
    if t == "memory_leak":
        return [
            Step(f"Check whether {proc} is still increasing its memory",
                 f"Read-only check of {proc}'s current memory. Compare it with the number in the alert: "
                 "a leak keeps rising and never comes back down.",
                 Action("inspect", pid, proc, reason="read-only check")),
            Step("Save your work in that program",
                 f"If {proc} is an app you are using, save anything open in it now. Restarting it is the fix."),
            Step("Close the leaking program gracefully",
                 f"Send {proc} a normal close request (SIGTERM). It can clean up and all its memory is returned.",
                 Action("terminate", pid, proc, reason="release leaked memory")),
            Step("Force-stop only if it ignored the request",
                 "If it is still running after the previous step, force-stop it (SIGKILL). Unsaved data is lost.",
                 Action("kill", pid, proc, reason="process ignored SIGTERM")),
        ]
    if t == "high_memory":
        top = ctx.get("top_mem") or []
        steps = [Step("Re-check memory now", "Measure memory again to confirm it is still high.",
                      Action("recheck", reason="confirm the condition"))]
        for p in top[:1]:
            steps += [Step(f"Check {p['name']} (uses {p['rss_mb']:.0f} MB)",
                           "Read-only look at the biggest memory user: is it something you are using?",
                           Action("inspect", p["pid"], p["name"], reason="read-only check")),
                      Step(f"Close {p['name']} if you don't need it",
                           "Closing it returns its memory to the system. Save your work in it first.",
                           Action("terminate", p["pid"], p["name"], reason="free RAM"))]
        return steps
    if t == "cpu_hog":
        return [
            Step("Check whether this CPU use is expected",
                 f"Compiling, rendering or games use a lot of CPU on purpose. A stuck loop does not stop.",
                 Action("inspect", pid, proc, reason="read-only check")),
            Step("Lower its priority",
                 f"Give {proc} nice 15. It keeps running but other programs get the CPU first. Nothing is lost.",
                 Action("renice", pid, proc, value=15, reason="reduce CPU starvation")),
            Step("Pause it if you need the CPU now",
                 f"Pausing (SIGSTOP) freezes {proc} without losing its work; it can be resumed later.",
                 Action("suspend", pid, proc, reason="free CPU immediately")),
            Step("End it if it is stuck",
                 f"If {proc} is in an endless loop, close it with SIGTERM.",
                 Action("terminate", pid, proc, reason="stuck process")),
        ]
    if t == "thrashing":
        top = ctx.get("top_mem") or []
        steps = [Step("See which programs use the most memory",
                      "The biggest memory users are listed below. They are the ones forcing the swapping.")]
        for p in top[:2]:
            steps.append(Step(f"Pause {p['name']} (uses {p['rss_mb']:.0f} MB)",
                              "Pausing frees pressure right away and keeps its work so you can resume it later.",
                              Action("suspend", p["pid"], p["name"], reason="reduce working set")))
        if top:
            p = top[0]
            steps.append(Step(f"Close {p['name']} if the system is still slow",
                              "Closing the biggest program returns its memory to the system.",
                              Action("terminate", p["pid"], p["name"], reason="free RAM")))
        return steps
    if t == "zombie":
        return [
            Step("Understand that zombies cannot be killed",
                 "They are already dead. Only their parent can remove them by calling wait()."),
            Step(f"Ask the parent {proc} to reap its children",
                 f"Send SIGCHLD to {proc} (PID {pid}). A well-written parent will clean them up.",
                 Action("signal_parent", pid, proc, reason="trigger wait()")),
            Step(f"Restart the parent {proc}",
                 "If the zombies remain, closing the parent hands them to init/systemd, which reaps them.",
                 Action("terminate", pid, proc, reason="re-parent zombies to init")),
        ]
    if t == "disk_bottleneck":
        if issue.evidence.get("kind") == "space":
            return [Step("Find what is using the space",
                         "Run `du -sh ~/* | sort -h` and `df -h` to see the largest folders."),
                    Step("Remove safe-to-delete files",
                         "Empty the trash, clear ~/.cache, run `sudo apt clean`, delete old downloads."),
                    Step("Check that space was freed", "SysDoctor will re-measure free space.")]
        top = ctx.get("top_io") or []
        steps = [Step("Refresh the disk measurements",
                      "Take a fresh sample to confirm the disk is still busy.",
                      Action("refresh_metrics", reason="confirm the condition"))]
        for p in top[:1]:
            steps.append(Step(f"Check {p['name']} (PID {p['pid']})",
                              "Read-only look at the process doing the most disk I/O.",
                              Action("inspect", p["pid"], p["name"], reason="read-only check")))
            steps.append(Step(f"Lower {p['name']}'s priority",
                              "Background disk jobs slow down, the rest of the system stays usable.",
                              Action("renice", p["pid"], p["name"], value=19, reason="reduce contention")))
            steps.append(Step(f"Pause {p['name']} for now",
                              "Stops its disk traffic immediately; resume it later when the machine is idle.",
                              Action("suspend", p["pid"], p["name"], reason="stop disk flood")))
        return steps
    if t == "resource_exhaustion":
        if issue.evidence.get("resource") == "disk":
            return [Step("Find what is filling the disk",
                         "Run `du -sh ~/* | sort -h` and `df -h`. Look for a folder or log file that keeps growing."),
                    Step("Stop the program that is writing", "If one program is writing non-stop, close it normally."),
                    Step("Free space early", "Empty the trash, clear ~/.cache, run `sudo apt clean`.")]
        top = ctx.get("top_mem") or []
        steps = [Step("Look at what is growing",
                      "Memory is filling at a steady rate. The biggest and fastest-growing programs are listed below.")]
        if top:
            p = top[0]
            steps += [Step(f"Check {p['name']} (uses {p['rss_mb']:.0f} MB)", "Read-only look at the biggest user.",
                           Action("inspect", p["pid"], p["name"], reason="read-only check")),
                      Step(f"Pause {p['name']} before memory runs out",
                           "Pausing stops it from taking more memory and keeps its work.",
                           Action("suspend", p["pid"], p["name"], reason="stop growth")),
                      Step(f"Close {p['name']}", "Closing it returns all of its memory to the system.",
                           Action("terminate", p["pid"], p["name"], reason="free memory"))]
        return steps
    return [Step("Review the live graphs", "Compare the unusual values with what you were doing at that moment."),
            Step("Check the busiest processes", "Sort the process table by CPU and memory to spot the cause.")]
