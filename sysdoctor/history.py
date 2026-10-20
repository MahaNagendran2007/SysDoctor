"""Layer 8 - Issue history and learning from previous fixes."""
import json

from .database import Database


class IssueHistory:
    def __init__(self, db: Database):
        self.db = db

    def record(self, issue, diagnosis, actions, verification):
        self.db.add_incident(
            alert_id=issue.alert_id, issue_type=issue.issue_type, process=issue.process,
            cause=diagnosis.root_cause if diagnosis else issue.details,
            actions=actions, outcome=verification.message if verification else "not verified",
            fixed=verification.resolved if verification else False,
            before=verification.before if verification else {}, after=verification.after if verification else {})

    def similar(self, issue, limit=5):
        """Previous incidents of the same type (same process name ranked first)."""
        rows = self.db.query("SELECT * FROM incidents WHERE issue_type=? ORDER BY ts DESC LIMIT 50",
                             (issue.issue_type,))
        rows.sort(key=lambda r: (r["process"] != issue.process, not r["fixed"], -r["ts"]))
        return rows[:limit]

    def previous_fix_hint(self, issue):
        for r in self.similar(issue):
            if r["fixed"]:
                try:
                    acts = json.loads(r["actions"] or "[]")
                except ValueError:
                    acts = []
                act_txt = ", ".join(f"{a.get('type')} on {a.get('process')}" for a in acts if a.get("type") not in
                                    (None, "none", "inspect")) or "the guided steps"
                same = " (same program)" if r["process"] == issue.process else ""
                return (f"A similar {issue.issue_type.replace('_', ' ')} happened before{same}; "
                        f"it was fixed by {act_txt}. Result: {r['outcome']}")
        return None

    def stats(self):
        rows = self.db.query("SELECT issue_type, COUNT(*) n, SUM(fixed) fixed FROM incidents GROUP BY issue_type")
        return {r["issue_type"]: {"count": r["n"], "fixed": r["fixed"] or 0} for r in rows}
