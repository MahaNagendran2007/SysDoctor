"""Shared data structures."""
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any

ISSUE_TYPES = ["memory_leak", "high_memory", "cpu_hog", "thrashing", "zombie", "disk_bottleneck", "anomaly",
               "resource_exhaustion"]

ISSUE_LABELS = {
    "memory_leak": "Memory leak",
    "high_memory": "High memory usage",
    "cpu_hog": "CPU hog",
    "thrashing": "Thrashing",
    "zombie": "Zombie process",
    "disk_bottleneck": "Disk bottleneck",
    "anomaly": "Unusual behaviour",
    "resource_exhaustion": "Resource running out",
}


@dataclass
class Issue:
    issue_type: str
    severity: str                      # info | warning | critical
    title: str
    details: str
    pid: Optional[int] = None
    process: Optional[str] = None
    evidence: Dict[str, Any] = field(default_factory=dict)
    alert_id: Optional[int] = None

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_alert(cls, row):
        import json
        ev = row.get("evidence") or "{}"
        try:
            ev = json.loads(ev)
        except (TypeError, ValueError):
            ev = {}
        return cls(issue_type=row["issue_type"], severity=row["severity"], title=row["title"],
                   details=row["details"], pid=row["pid"], process=row["process"], evidence=ev,
                   alert_id=row["id"])


@dataclass
class Prediction:
    resource: str                      # memory | disk | swap
    current: float
    slope_per_min: float
    minutes_to_limit: Optional[float]
    r2: float
    limit: float = 95.0
    status: str = "stable"             # insufficient | rising | stable | falling | noisy | at_limit
    n_samples: int = 0
    span_min: float = 0.0
    message: str = ""

    @property
    def is_warning(self):
        from . import config
        return (self.status == "rising" and self.minutes_to_limit is not None
                and self.minutes_to_limit <= config.PREDICT_WARN_MINUTES)


@dataclass
class Action:
    """A structured action proposed by the LLM. It is only data - never executed directly."""
    # none | inspect | refresh_metrics | recheck | renice | suspend | resume | terminate | kill | signal_parent
    type: str
    pid: Optional[int] = None
    process: Optional[str] = None
    value: Optional[int] = None        # e.g. nice value for renice
    reason: str = ""

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        if not d or not isinstance(d, dict):
            return cls(type="none")
        pid = d.get("pid")
        try:
            pid = int(pid) if pid is not None and str(pid).strip() not in ("", "null", "None") else None
        except (TypeError, ValueError):
            pid = None
        val = d.get("value")
        try:
            val = int(val) if val is not None else None
        except (TypeError, ValueError):
            val = None
        return cls(type=str(d.get("type") or "none").lower().strip(), pid=pid,
                   process=d.get("process"), value=val, reason=str(d.get("reason") or ""))


@dataclass
class Step:
    title: str
    detail: str
    action: Action = field(default_factory=lambda: Action(type="none"))

    def to_dict(self):
        return {"title": self.title, "detail": self.detail, "action": self.action.to_dict()}


@dataclass
class Diagnosis:
    summary: str
    root_cause: str
    os_concept: str
    steps: List[Step]
    sources: List[str] = field(default_factory=list)
    backend: str = "offline"
    previous_fix: Optional[str] = None
    used_llm: bool = False                     # True only if the text really came from the LLM
    llm_error: Optional[str] = None            # why the LLM was not used (shown in the UI)
    rag_query: str = ""                        # the query sent to the vector index
    passages: List[Dict[str, Any]] = field(default_factory=list)   # retrieved chunks (source, section, score, text)
