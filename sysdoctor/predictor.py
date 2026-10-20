"""Layer 2b - Resource exhaustion prediction from real historical samples.

* Uses only the most recent *contiguous* run of samples (a restart of SysDoctor creates a gap; data
  from before the gap is not mixed in).
* Refuses to predict until there are enough samples over enough time ("insufficient").
* Trend = Theil-Sen slope (median of pairwise slopes), which ignores short spikes that would fool
  ordinary least squares. R² of that line tells whether the trend is steady.
* A time-to-limit is shown only when the resource is rising steadily; otherwise the status says why.
"""
import numpy as np

from . import config
from .models import Prediction, Issue

RESOURCES = {"memory": "mem_percent", "disk": "disk_percent", "swap": "swap_percent"}
LABEL = {"memory": "Memory", "disk": "Disk", "swap": "Swap / page file"}


def contiguous_tail(metrics, max_gap=None):
    """Samples after the last gap longer than max_gap seconds."""
    if not metrics:
        return []
    max_gap = max_gap or config.SAMPLE_INTERVAL_SEC * 5
    start = 0
    for i in range(1, len(metrics)):
        if metrics[i]["ts"] - metrics[i - 1]["ts"] > max_gap:
            start = i
    return metrics[start:]


def theil_sen(x, y, max_points=120):
    x, y = np.asarray(x, float), np.asarray(y, float)
    if len(x) > max_points:                           # keep it O(n²) small
        idx = np.linspace(0, len(x) - 1, max_points).astype(int)
        x, y = x[idx], y[idx]
    i, j = np.triu_indices(len(x), k=1)
    dx = x[j] - x[i]
    ok = dx > 0
    if not ok.any():
        return 0.0, float(np.median(y)), 0.0, 0.0
    slope = float(np.median((y[j] - y[i])[ok] / dx[ok]))
    intercept = float(np.median(y - slope * x))
    resid = y - (slope * x + intercept)
    noise = 1.4826 * float(np.median(np.abs(resid - np.median(resid))))   # robust std (MAD)
    keep = np.abs(resid) <= max(3 * noise, 0.5)
    yk, rk = y[keep], resid[keep]                       # R² ignoring isolated spikes
    ss_res, ss_tot = float(np.sum(rk ** 2)), float(np.sum((yk - yk.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-9 else 0.0
    return slope, intercept, max(r2, 0.0), noise


def predict(metrics, window=None, limit=None):
    window = window or config.PREDICT_WINDOW_SAMPLES
    limit = limit or config.PREDICT_LIMIT_PERCENT
    rows = contiguous_tail(metrics)[-window:]
    out = []
    n = len(rows)
    span = (rows[-1]["ts"] - rows[0]["ts"]) / 60.0 if n >= 2 else 0.0
    for resource, col in RESOURCES.items():
        if resource == "swap" and rows and all((r.get("swap_used_mb") or 0) == 0 and (r.get(col) or 0) == 0
                                               for r in rows):
            continue                                   # no swap / page file in use: nothing to predict
        current = float(rows[-1][col]) if rows else 0.0
        label = LABEL[resource]
        if n < config.PREDICT_MIN_SAMPLES or span < config.PREDICT_MIN_SPAN_MIN:
            need_n = max(config.PREDICT_MIN_SAMPLES - n, 0)
            need_s = max(config.PREDICT_MIN_SPAN_MIN * 60 - span * 60,
                         need_n * config.SAMPLE_INTERVAL_SEC)
            out.append(Prediction(resource, round(current, 1), 0.0, None, 0.0, limit, "insufficient", n,
                                  round(span, 2),
                                  f"Not enough data yet ({n}/{config.PREDICT_MIN_SAMPLES} samples). "
                                  f"A trend needs about {need_s:.0f} s more of monitoring."))
            continue
        t = np.array([r["ts"] for r in rows])
        t = (t - t[0]) / 60.0
        y = np.array([r.get(col) or 0.0 for r in rows])
        slope, _, r2, noise = theil_sen(t, y)
        # a trend only counts if the change over the window is clearly bigger than the noise
        significant = abs(slope) * span > max(2 * noise, 0.5)
        minutes, status = None, "stable"
        if current >= limit:
            status = "at_limit"
            msg = f"{label} is already at {current:.0f}% (limit {limit:.0f}%). Free some now."
        elif not significant:
            msg = (f"{label} is stable ({current:.0f}%, change {slope:+.2f} pts/min is within normal noise). "
                   "No exhaustion expected.")
        elif slope >= config.PREDICT_MIN_SLOPE and r2 >= config.PREDICT_MIN_R2:
            status = "rising"
            minutes = (limit - current) / slope
            msg = (f"{label} is rising steadily by {slope:.2f} points per minute over the last {span:.1f} min. "
                   f"If nothing changes it reaches {limit:.0f}% in about {minutes:.0f} min"
                   + (" - act soon." if minutes <= config.PREDICT_WARN_MINUTES else "."))
        elif slope >= config.PREDICT_MIN_SLOPE:
            status = "noisy"
            msg = (f"{label} is going up on average ({slope:+.2f} pts/min) but too unevenly (R² {r2:.2f}) "
                   "to give a reliable time estimate.")
        elif slope <= -config.PREDICT_MIN_SLOPE:
            status = "falling"
            msg = f"{label} is going down ({slope:+.2f} pts/min). No exhaustion expected."
        else:
            msg = f"{label} is stable ({slope:+.2f} pts/min over {span:.1f} min). No exhaustion expected."
        out.append(Prediction(resource=resource, current=round(current, 1), slope_per_min=round(slope, 3),
                              minutes_to_limit=round(minutes, 1) if minutes is not None else None,
                              r2=round(r2, 3), limit=limit, status=status, n_samples=n, span_min=round(span, 2),
                              message=msg))
    return out


def predictions_to_issues(preds):
    issues = []
    for p in preds:
        if p.is_warning:
            issues.append(Issue(
                issue_type="resource_exhaustion",
                severity="critical" if p.minutes_to_limit < 5 else "warning",
                title=f"{LABEL[p.resource]} may run out in ~{p.minutes_to_limit:.0f} min",
                details=p.message,
                evidence={"metric": f"{LABEL[p.resource]} used", "value": p.current, "unit": "%",
                          "threshold": f"predicted to reach {p.limit:.0f}% within {config.PREDICT_WARN_MINUTES:.0f} min",
                          "duration_s": round(p.span_min * 60),
                          "resource": p.resource, "current_percent": p.current,
                          "slope_per_min": p.slope_per_min, "minutes_to_limit": p.minutes_to_limit,
                          "r2": p.r2, "samples": p.n_samples}))
    return issues
