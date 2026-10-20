"""Wires all nine layers together into one object used by the dashboard and the CLI."""
import platform

from . import config
from .collector import CAPABILITIES, CollectorThread
from .database import Database
from .demo import DemoManager
from .engine import MonitorEngine
from .executor import ActionExecutor
from .guide import DiagnosisPipeline, GuideSession, HAVE_LANGGRAPH
from .history import IssueHistory
from .knowledge_base import KnowledgeBase
from .llm import LLMClient
from .models import Issue
from .verifier import FixVerifier


class SysDoctor:
    def __init__(self, start_monitor=True, llm_backend=None, db_path=None):
        self.db = Database(db_path)
        self.engine = MonitorEngine(self.db)
        self.kb = KnowledgeBase()
        self.llm = LLMClient(backend=llm_backend)
        self.history = IssueHistory(self.db)
        self.executor = ActionExecutor(self.db)
        self.verifier = FixVerifier(self.engine)
        self.pipeline = DiagnosisPipeline(self.db, self.kb, self.llm, self.history)
        self.monitor = CollectorThread(self.db, self.engine)
        self.executor.refresh_fn = self.monitor.collector.collect_once
        self.active_guide = None            # survives browser reloads (one local user)
        self.demos = DemoManager()
        if start_monitor:
            self.monitor.start()

    def set_llm(self, backend, model=None):
        self.llm = LLMClient(backend=backend, model=model)
        self.pipeline.llm = self.llm

    def start_guide(self, alert_row) -> GuideSession:
        issue = Issue.from_alert(alert_row)
        return GuideSession(issue, self.pipeline, self.executor, self.verifier, self.history)

    def status(self):
        st = self.llm.status()
        return {"samples": self.monitor.samples, "collector_error": self.monitor.last_error,
                "interval": config.SAMPLE_INTERVAL_SEC, "rag_backend": self.kb.backend,
                "rag_chunks": len(self.kb.chunks), "llm": self.llm.label,
                "llm_online": st.online, "llm_error": None if st.online else st.message,
                "llm_status": st, "langgraph": HAVE_LANGGRAPH, "platform": platform.system(),
                "capabilities": CAPABILITIES}
