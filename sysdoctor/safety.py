"""Layer 6a - Safety Layer.

The LLM only ever produces a *structured* Action. This module validates it before anything runs:
  * only whitelisted action types are allowed
  * critical/system processes are always blocked (PID 0/1, kernel threads, desktop, Windows core)
  * SysDoctor never acts on itself or its parent
  * processes owned by other users are blocked (no privilege escalation)
  * PID-reuse protection: the process name must still match what the diagnosis saw
  * risky actions (suspend, terminate, kill, signal_parent) always need explicit user approval
"""
import getpass
import os
import platform
from dataclasses import dataclass

import psutil

from .models import Action

RISK = {
    "none": "none", "inspect": "none", "refresh_metrics": "none", "recheck": "none",
    "renice": "low", "resume": "low",
    "suspend": "medium", "signal_parent": "medium",
    "terminate": "high", "kill": "critical",
}
NEEDS_CONFIRMATION = {"suspend", "signal_parent", "terminate", "kill", "renice"}

CRITICAL_NAMES = {
    # Linux core
    "systemd", "init", "kthreadd", "ksoftirqd", "kswapd0", "migration", "rcu_sched", "rcu_preempt",
    "systemd-journald", "systemd-logind", "systemd-udevd", "systemd-resolved", "systemd-networkd",
    "dbus-daemon", "dbus-broker", "networkmanager", "polkitd", "udisksd", "sshd", "login", "agetty",
    "xorg", "xwayland", "gnome-shell", "gdm", "gdm3", "gdm-session-worker", "lightdm", "sddm",
    "kwin_wayland", "kwin_x11", "plasmashell", "pipewire", "wireplumber", "pulseaudio", "cron", "crond",
    "rsyslogd", "auditd", "containerd", "dockerd", "snapd", "bash", "zsh", "sh", "fish",
    "gnome-terminal-server", "konsole",
    # Windows core
    "system", "system idle process", "registry", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe",
    "services.exe", "lsass.exe", "svchost.exe", "dwm.exe", "explorer.exe", "fontdrvhost.exe",
    "memory compression", "secure system", "lsaiso.exe", "msmpeng.exe", "conhost.exe",
    "spoolsv.exe", "sihost.exe", "taskhostw.exe", "runtimebroker.exe", "ctfmon.exe", "audiodg.exe",
    "startmenuexperiencehost.exe", "shellexperiencehost.exe", "searchhost.exe", "searchindexer.exe",
    "textinputhost.exe", "wudfhost.exe", "dllhost.exe", "securityhealthservice.exe", "wmiprvse.exe",
    "system interrupts", "vmmem", "vmmemwsl", "sqlservr.exe",
    # macOS core
    "launchd", "kernel_task", "windowserver", "loginwindow",
    # tools the assistant itself depends on
    "ollama", "ollama_llama_server",
}
CRITICAL_PREFIXES = ("kworker", "ksoftirqd", "migration/", "rcu_", "irq/", "watchdog", "cpuhp", "idle_inject")


@dataclass
class SafetyDecision:
    allowed: bool
    requires_confirmation: bool
    risk: str
    reason: str
    command_preview: str = ""


def _current_user():
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER") or os.environ.get("USERNAME") or ""


def is_critical(proc: psutil.Process):
    try:
        name = (proc.name() or "").lower()
    except psutil.Error:
        name = ""
    if proc.pid in (0, 1, 2):
        return True, f"PID {proc.pid} is a core system process"
    if name in CRITICAL_NAMES or name.startswith(CRITICAL_PREFIXES):
        return True, f"'{name}' is a critical system/desktop process"
    own = os.getpid()
    if proc.pid == own:
        return True, "SysDoctor will not act on itself"
    try:
        if proc.pid in {p.pid for p in psutil.Process(own).parents()}:
            return True, "SysDoctor will not act on the program that launched it"
    except psutil.Error:
        pass
    if platform.system() == "Linux":
        try:                       # kernel threads have no executable/cmdline and are children of kthreadd
            if proc.ppid() == 2:
                return True, "kernel threads cannot be controlled from user space"
        except psutil.Error:
            pass
    return False, ""


def command_preview(action: Action):
    pid = action.pid
    return {
        "renice": f"renice +{action.value or 10} -p {pid}",
        "suspend": f"kill -STOP {pid}",
        "resume": f"kill -CONT {pid}",
        "terminate": f"kill -TERM {pid}",
        "kill": f"kill -KILL {pid}",
        "signal_parent": f"kill -s SIGCHLD {pid}",
        "inspect": f"ps -o pid,ppid,stat,ni,pcpu,rss,cmd -p {pid}" if pid else "(observe only)",
        "refresh_metrics": "(collect a fresh sample - read only)",
        "recheck": "(re-measure CPU / memory / disk - read only)",
    }.get(action.type, "(no command)")


def validate(action: Action) -> SafetyDecision:
    t = action.type
    if t not in RISK:
        return SafetyDecision(False, False, "blocked", f"Action type '{t}' is not on the allowed list.")
    risk = RISK[t]
    if t == "none":
        return SafetyDecision(True, False, risk, "Information only - nothing will be executed.")
    if t in ("refresh_metrics", "recheck"):
        return SafetyDecision(True, False, risk, "Read-only measurement - nothing on the system changes.",
                              command_preview(action))
    if action.pid is None:
        if t == "inspect":
            return SafetyDecision(True, False, risk, "Read-only check.", command_preview(action))
        return SafetyDecision(False, False, "blocked", "The action does not name a target process.")
    try:
        proc = psutil.Process(action.pid)
        name = proc.name()
        status = proc.status()
    except psutil.NoSuchProcess:
        return SafetyDecision(False, False, "blocked", f"PID {action.pid} no longer exists (it may already have ended).")
    except psutil.AccessDenied:
        return SafetyDecision(False, False, "blocked", f"No permission to inspect PID {action.pid}.")

    if t == "inspect":
        return SafetyDecision(True, False, risk, "Read-only check.", command_preview(action))

    critical, why = is_critical(proc)
    if critical:
        return SafetyDecision(False, False, "blocked", f"Blocked: {why}.")
    if action.process and action.process.lower() != name.lower():
        return SafetyDecision(False, False, "blocked",
                              f"PID {action.pid} is now '{name}', not '{action.process}' - the PID was reused. "
                              "Refusing to act on the wrong process.")
    try:
        owner = proc.username()
    except psutil.Error:
        owner = None
    me = _current_user()
    is_admin = (hasattr(os, "geteuid") and os.geteuid() == 0)
    if owner and me and not is_admin and owner.split("\\")[-1] != me.split("\\")[-1]:
        return SafetyDecision(False, False, "blocked",
                              f"Process belongs to user '{owner}'. SysDoctor will not act on other users' "
                              "processes or escalate privileges.")
    if status == psutil.STATUS_ZOMBIE and t in ("terminate", "kill", "suspend"):
        return SafetyDecision(False, False, "blocked",
                              "This is a zombie - it is already dead and cannot be killed. Act on its parent instead.")
    if t == "renice":
        v = action.value if action.value is not None else 10
        if v < 0:
            return SafetyDecision(False, False, "blocked", "Raising priority (negative nice) needs root and is not allowed.")
        if v > 19:
            action.value = 19
    if t == "resume" and status != psutil.STATUS_STOPPED:
        return SafetyDecision(False, False, "blocked", "The process is not paused, so there is nothing to resume.")
    return SafetyDecision(True, t in NEEDS_CONFIRMATION, risk,
                          {"renice": "Lowers CPU priority. Nothing is lost and it can be undone.",
                           "resume": "Resumes a paused process.",
                           "suspend": "Pauses the process. Its memory is kept and it can be resumed later.",
                           "signal_parent": "Asks the parent to collect its finished children. Harmless if ignored.",
                           "terminate": "Asks the program to close. Unsaved work in it may be lost.",
                           "kill": "Forces the program to stop immediately. Unsaved work WILL be lost.",
                           }[t], command_preview(action))
