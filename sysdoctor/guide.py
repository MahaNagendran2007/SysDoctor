"""Layer 5 - Step-by-step guide controller.

A LangGraph StateGraph builds the diagnosis (gather live data -> retrieve docs -> recall history ->
LLM reasoning -> safety pre-check). A GuideSession then walks the user through the plan one step
at a time: explain -> wait for approval / questions -> execute via safety layer -> verify -> next.
If LangGraph is not installed the same nodes run as a plain sequential pipeline.
"""
import copy
import platform
from typing import Any, Dict, List, Optional, TypedDict

import psutil

from .executor import ActionExecutor
from .history import IssueHistory
from .knowledge_base import KnowledgeBase
from .llm import LLMClient
from .models import Action, Diagnosis, Issue, Step
from .safety import validate
from .verifier import FixVerifier, comparison_rows, measure

try:
    from langgraph.graph import StateGraph, END
    HAVE_LANGGRAPH = True
except Exception:
    HAVE_LANGGRAPH = False


class DiagState(TypedDict, total=False):
    issue: Issue
    live: Dict[str, Any]
    context: Dict[str, Any]
    docs: List[Dict[str, Any]]
    previous_fix: Optional[str]
    diagnosis: Diagnosis
    use_rag: bool
    rag_query: str


def live_context(db, issue):
    """Compact live snapshot for the prompt, plus lists used by playbooks."""
    m = db.recent_metrics(1)
    procs = db.latest_processes()
    top_cpu = sorted(procs, key=lambda p: p["cpu_percent"], reverse=True)[:6]
    top_mem = [p for p in sorted(procs, key=lambda p: p["rss_mb"], reverse=True)
               if p["pid"] > 2][:6]
    live = {"system": {k: m[0].get(k) for k in ("cpu_percent", "iowait", "load1", "mem_percent", "mem_available_mb",
                                                "swap_percent", "swap_in_sec", "swap_out_sec", "major_faults_sec",
                                                "page_faults_sec", "disk_percent", "disk_read_mb_sec",
                                                "disk_write_mb_sec", "disk_latency_ms", "proc_zombie")
                       if m[0].get(k) is not None} if m else {},
            "os": platform.system(),
            "top_cpu": [{k: p[k] for k in ("pid", "name", "cpu_percent", "rss_mb", "status", "nice", "username")}
                        for p in top_cpu],
            "top_memory": [{k: p[k] for k in ("pid", "name", "rss_mb", "cpu_percent", "status", "username")}
                           for p in top_mem]}
    if issue.pid:
        live["target_process_history"] = [{"t": round(r["ts"]), "cpu": r["cpu_percent"], "rss_mb": r["rss_mb"]}
                                          for r in db.process_history(issue.pid, 20)[::2]]
    # heavy I/O candidates: measured per-process disk rate (Windows + Linux), then D-state processes
    io = sorted([p for p in procs if (p.get("io_mb_sec") or 0) > 0.5 and p["pid"] > 4],
                key=lambda p: p["io_mb_sec"], reverse=True)
    if issue.issue_type == "disk_bottleneck" and issue.pid:
        io = [p for p in procs if p["pid"] == issue.pid] + [p for p in io if p["pid"] != issue.pid]
    io = io or [p for p in procs if p["status"] == "disk-sleep"]
    live["top_disk_io"] = [{k: p.get(k) for k in ("pid", "name", "io_mb_sec", "status")} for p in io[:5]]
    ctx = {"top_mem": [p for p in top_mem if p["pid"] != issue.pid or issue.issue_type == "thrashing"],
           "top_io": io}
    return live, ctx


class DiagnosisPipeline:
    def __init__(self, db, kb: KnowledgeBase, llm: LLMClient, history: IssueHistory):
        self.db, self.kb, self.llm, self.history = db, kb, llm, history
        self.graph = self._build_graph() if HAVE_LANGGRAPH else None

    # nodes
    def n_collect(self, s: DiagState):
        live, ctx = live_context(self.db, s["issue"])
        return {"live": live, "context": ctx}

    def n_retrieve(self, s: DiagState):
        """RAG: build a query from the detected issue and search the FAISS / vector index."""
        if not s.get("use_rag", True):
            return {"docs": [], "rag_query": ""}
        q = self.kb.query_for_issue(s["issue"])
        return {"docs": self.kb.search(q), "rag_query": q}

    def n_history(self, s: DiagState):
        return {"previous_fix": self.history.previous_fix_hint(s["issue"])}

    def n_reason(self, s: DiagState):
        d = self.llm.diagnose(s["issue"], s["live"], s.get("docs", []), s.get("previous_fix"),
                              s.get("context"), use_rag=s.get("use_rag", True), rag_query=s.get("rag_query", ""))
        return {"diagnosis": d}

    def n_safety(self, s: DiagState):
        """Drop steps whose actions are blocked outright; keep them as info-only with the reason."""
        d = s["diagnosis"]
        clean = []
        for step in d.steps:
            if step.action.type not in ("none", "inspect"):
                dec = validate(copy.copy(step.action))
                if not dec.allowed:
                    step = Step(step.title, f"{step.detail}\n\n(Safety layer blocked the proposed action "
                                            f"'{step.action.type}': {dec.reason} This step is shown for "
                                            "information only.)", Action("none"))
            clean.append(step)
        d.steps = clean
        return {"diagnosis": d}

    def _build_graph(self):
        g = StateGraph(DiagState)
        g.add_node("collect", self.n_collect)
        g.add_node("retrieve", self.n_retrieve)
        g.add_node("recall", self.n_history)
        g.add_node("reason", self.n_reason)
        g.add_node("safety", self.n_safety)
        g.set_entry_point("collect")
        g.add_edge("collect", "retrieve")
        g.add_edge("retrieve", "recall")
        g.add_edge("recall", "reason")
        g.add_edge("reason", "safety")
        g.add_edge("safety", END)
        return g.compile()

    def run(self, issue, use_rag=True):
        state: DiagState = {"issue": issue, "use_rag": use_rag}
        if self.graph is not None:
            return self.graph.invoke(state)
        for node in (self.n_collect, self.n_retrieve, self.n_history, self.n_reason, self.n_safety):
            state.update(node(state))
        return state


class GuideSession:
    """Interactive, one-step-at-a-time troubleshooting for a single issue."""

    def __init__(self, issue: Issue, pipeline: DiagnosisPipeline, executor: ActionExecutor,
                 verifier: FixVerifier, history: IssueHistory):
        self.issue = issue
        self.pipeline, self.executor, self.verifier, self.history = pipeline, executor, verifier, history
        self.state = pipeline.run(issue)
        self.diagnosis: Diagnosis = self.state["diagnosis"]
        self.step_index = 0
        self.status = "guiding"               # guiding | resolved | exhausted | closed
        self.messages = []                    # (role, text)
        self.actions_done = []
        self.last_verification = None
        self.comparisons = []                 # before/after tables of executed actions
        self.last_answer_docs = []
        self.replans = 0
        if issue.alert_id:
            pipeline.db.set_alert_status(issue.alert_id, "in_progress")
        self.messages.append(("assistant", self._intro()))

    # --------------------------------------------------------------- view
    def _intro(self):
        d = self.diagnosis
        txt = f"**What is happening:** {d.root_cause}\n\n**OS concept:** {d.os_concept}"
        if d.previous_fix:
            txt += f"\n\n**Seen before:** {d.previous_fix}"
        if d.llm_error:
            txt += f"\n\n_Language model not used: {d.llm_error}_"
        txt += f"\n\nI have a plan with {len(d.steps)} steps. We will go one step at a time."
        return txt

    @property
    def current_step(self) -> Optional[Step]:
        if self.status != "guiding" or self.step_index >= len(self.diagnosis.steps):
            return None
        return self.diagnosis.steps[self.step_index]

    def current_safety(self):
        st = self.current_step
        return validate(copy.copy(st.action)) if st else None

    # -------------------------------------------------------------- moves
    def approve(self):
        """User approved the current step: safety check -> execute -> verify -> record -> next step."""
        st = self.current_step
        if st is None:
            return
        act = st.action
        n = self.step_index + 1
        if act.type == "none":
            self.messages.append(("user", f"Done: step {n}"))
            return self._advance()
        if act.type in ("inspect", "refresh_metrics", "recheck"):         # read-only, no approval needed
            ok, msg = self.executor.execute(act, self.issue.alert_id, issue=self.issue)
            self.messages.append(("system", f"Step {n} result: {msg}"))
            return self._advance()
        before = measure(self.issue)
        ok, msg = self.executor.execute(act, self.issue.alert_id, confirmed=True, issue=self.issue)
        action_id = self.executor.last_action_id
        self.messages.append(("system", ("✅ " if ok else "⛔ ") + msg))
        if not ok:
            return self._advance()
        self.actions_done.append(act.to_dict())
        v = self.verifier.verify(self.issue, before)
        self.last_verification = v
        self.comparisons.append({"step": st.title, "rows": comparison_rows(v.before, v.after),
                                 "resolved": v.resolved})
        if action_id:
            self.pipeline.db.record_verification(action_id, v.before, v.after, v.resolved, v.message)
        table = "\n".join(f"| {m} | {b} | {a} |" for m, b, a in comparison_rows(v.before, v.after))
        table = "| Measurement | Before | After |\n|---|---|---|\n" + table
        if v.resolved:
            self.messages.append(("assistant", f"**Verification: problem appears resolved.** {v.message}\n\n{table}"))
            self._finish(resolved=True)
        else:
            self.messages.append(("assistant", f"**Verification: not solved yet.** {v.message}\n\n{table}\n\n"
                                               "Let's continue with the next step."))
            self._advance(replan=True)

    def skip(self):
        if self.current_step:
            self.messages.append(("user", f"Skip: {self.current_step.title}"))
        self._advance()

    def ask(self, question):
        self.messages.append(("user", question))
        docs = self.pipeline.kb.search(f"{question} {self.issue.issue_type.replace('_', ' ')}")
        live, _ = live_context(self.pipeline.db, self.issue)
        ans, used_llm = self.pipeline.llm.answer(question, self.issue, live, docs,
                                                 [(r, m) for r, m in self.messages if r in ("user", "assistant")])
        src = ", ".join(sorted({d["source"] for d in docs[:3]}))
        tag = f"\n\n<small>{'LLM' if used_llm else 'offline'} · sources: {src}</small>" if src else ""
        self.messages.append(("assistant", ans + tag))
        self.last_answer_docs = docs

    def simplify(self):
        st = self.current_step
        if st:
            self.messages.append(("user", "Can you explain this step more simply?"))
            self.messages.append(("assistant", self.pipeline.llm.simplify(st.title, st.detail, self.issue, st.action.type)))

    def close(self, resolved_by_user=False):
        self._finish(resolved=resolved_by_user, closed=True)

    # ----------------------------------------------------------- internals
    def _advance(self, replan=False):
        self.step_index += 1
        if replan and self.pipeline.llm.available() and self.replans < 2 and self.step_index < 6:
            # give the LLM the updated situation and let it propose the remaining steps
            self.replans += 1
            upd = copy.copy(self.issue)
            upd.details = (f"{self.issue.details} UPDATE: actions already tried: {self.actions_done}. "
                           f"Latest check: {self.last_verification.message if self.last_verification else ''}")
            new = self.pipeline.run(upd)["diagnosis"]
            remaining = [s for s in new.steps if s.action.to_dict() not in self.actions_done]
            if remaining:
                self.diagnosis.steps = self.diagnosis.steps[:self.step_index] + remaining
                self.messages.append(("assistant", "I updated the remaining steps using the latest measurements."))
        if self.step_index >= len(self.diagnosis.steps):
            self.messages.append(("assistant", "That was the last step I can suggest safely. "
                                               "If the problem remains, close the program manually or ask me a question."))
            self._finish(resolved=False)

    def _finish(self, resolved, closed=False):
        if self.status in ("resolved", "exhausted", "closed"):
            return
        self.status = "resolved" if resolved else ("closed" if closed else "exhausted")
        self.history.record(self.issue, self.diagnosis, self.actions_done, self.last_verification)
        if self.issue.alert_id:
            self.pipeline.db.set_alert_status(self.issue.alert_id,
                                              "resolved" if resolved else ("dismissed" if closed else "open"))
