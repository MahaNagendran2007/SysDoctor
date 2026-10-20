"""Layer 6b - Action Executor. Runs only actions the safety layer approved and the user confirmed."""
import logging
import platform
import signal
import time

import psutil

from . import config
from .database import Database
from .models import Action
from .safety import validate

logging.basicConfig(filename=str(config.ACTION_LOG), level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sysdoctor.actions")
IS_WINDOWS = platform.system() == "Windows"


class ActionExecutor:
    """Runs ONLY predefined actions (see safety.RISK). There is no code path that runs a shell command or
    any text produced by the LLM: each action type maps to one fixed psutil call."""

    def __init__(self, db: Database, refresh_fn=None):
        self.db = db
        self.refresh_fn = refresh_fn          # collector.collect_once, used by "refresh_metrics"
        self.last_action_id = None

    def execute(self, action: Action, alert_id=None, confirmed=False, issue=None):
        """Returns (ok: bool, message: str). self.last_action_id = row id in the actions table (or None)."""
        self.last_action_id = None
        self._issue_type = issue.issue_type if issue else None
        decision = validate(action)                       # re-validate at execution time
        if not decision.allowed:
            self._log(alert_id, action, decision.risk, "blocked", "not_run", decision.reason, approved=confirmed)
            return False, decision.reason
        if decision.requires_confirmation and not confirmed:
            msg = "This action needs your explicit approval first."
            self._log(alert_id, action, decision.risk, "awaiting_approval", "not_run", msg, approved=False)
            return False, msg
        if action.type == "none":
            return True, "Nothing to run."
        if action.type in ("inspect", "refresh_metrics", "recheck"):
            msg = self._read_only(action, issue)
            self._log(alert_id, action, decision.risk, "read_only", "success", msg, approved=None)
            return True, msg
        try:
            p = psutil.Process(action.pid)
            if action.type == "renice":
                v = max(0, min(19, action.value if action.value is not None else 10))
                if IS_WINDOWS:
                    p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if v < 15 else psutil.IDLE_PRIORITY_CLASS)
                else:
                    p.nice(max(v, p.nice()))
                msg = f"Lowered priority of {p.name()} (PID {p.pid}) to nice {p.nice()}."
            elif action.type == "suspend":
                p.suspend()
                msg = f"Paused {p.name()} (PID {p.pid}). It can be resumed later."
            elif action.type == "resume":
                p.resume()
                msg = f"Resumed {p.name()} (PID {p.pid})."
            elif action.type == "terminate":
                name = p.name()
                p.terminate()
                try:
                    p.wait(timeout=5)
                    msg = f"{name} (PID {action.pid}) closed after SIGTERM."
                except psutil.TimeoutExpired:
                    msg = f"Sent SIGTERM to {name} (PID {action.pid}) but it is still running after 5 s."
            elif action.type == "kill":
                name = p.name()
                p.kill()
                try:
                    p.wait(timeout=5)
                except psutil.TimeoutExpired:
                    pass
                msg = f"Force-stopped {name} (PID {action.pid}) with SIGKILL."
            elif action.type == "signal_parent":
                if IS_WINDOWS:
                    return False, "Zombie reaping via SIGCHLD is a Linux/Unix feature."
                p.send_signal(signal.SIGCHLD)
                time.sleep(1)
                msg = f"Sent SIGCHLD to {p.name()} (PID {p.pid}) asking it to reap its children."
            else:
                return False, f"Unknown action {action.type}"
            self._log(alert_id, action, decision.risk, "approved", "success", msg)
            return True, msg
        except psutil.NoSuchProcess:
            msg = f"PID {action.pid} ended before the action ran."
            self._log(alert_id, action, decision.risk, "approved", "gone", msg)
            return True, msg
        except psutil.AccessDenied:
            msg = f"Permission denied for PID {action.pid}. It may belong to root or another user."
        except Exception as exc:
            msg = f"Action failed: {exc}"
        self._log(alert_id, action, decision.risk, "approved", "failed", msg)
        return False, msg

    def _read_only(self, action, issue):
        if action.type == "inspect":
            return self._inspect(action.pid) if action.pid else "Nothing to inspect."
        if action.type == "refresh_metrics":
            if self.refresh_fn:
                m, _ = self.refresh_fn()
                return (f"Fresh sample: CPU {m['cpu_percent']:.0f}%, memory {m['mem_percent']:.0f}%, "
                        f"swap/page file {m['swap_percent']:.0f}%, disk read {m['disk_read_mb_sec']:.1f} MB/s, "
                        f"write {m['disk_write_mb_sec']:.1f} MB/s.")
            return "Metrics are refreshed automatically every few seconds."
        from .verifier import measure, describe          # recheck
        return "Re-check: " + describe(measure(issue) if issue else measure(None))

    def _inspect(self, pid):
        try:
            p = psutil.Process(pid)
            cpu = p.cpu_percent(interval=0.5)
            with p.oneshot():
                mi = p.memory_info()
                return (f"{p.name()} PID {pid}: state={p.status()}, CPU={cpu:.0f}%, "
                        f"RSS={mi.rss/1e6:.0f} MB, threads={p.num_threads()}, nice={p.nice()}, "
                        f"user={p.username()}, parent={p.ppid()}")
        except psutil.Error as e:
            return f"Could not inspect PID {pid}: {e}"

    def _log(self, alert_id, action, risk, decision, result, message, approved=True):
        self.last_action_id = self.db.log_action(alert_id, action.type, action.pid, action.process, risk, decision,
                                                 result, message, issue_type=getattr(self, "_issue_type", None),
                                                 approved=approved)
        log.info("alert=%s action=%s pid=%s proc=%s risk=%s decision=%s result=%s msg=%s",
                 alert_id, action.type, action.pid, action.process, risk, decision, result, message)
