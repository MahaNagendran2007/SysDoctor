"""Layer 4 - LLM reasoning and explanation.

Backends:
  * ollama  - local Llama 3 8B / Mistral 7B (offline & private). Uses LangChain's ChatOllama when
              installed, otherwise the Ollama REST API directly.
  * openai  - any OpenAI-compatible chat API (set SYSDOCTOR_OPENAI_KEY).
  * offline - no model; explanations are assembled from the playbooks and RAG passages.
If the chosen backend is unreachable the client falls back to offline mode automatically.
The LLM returns JSON with explanation + steps; each step may include a structured action that is
handed to the safety layer. The LLM itself never executes anything.
"""
import json
import re
import time
from dataclasses import dataclass, field

import requests

from . import config
from .models import Action, Diagnosis, Step
from .playbooks import CONCEPTS, SIMPLE_CAUSE, default_steps

SYSTEM_PROMPT = """You are SysDoctor, an Operating Systems troubleshooting assistant for ordinary users.
You receive a detected problem, live system measurements, excerpts from OS documentation and
a reference plan. Rules:
- Explain in simple language a student can follow, then connect it to the correct OS concept.
- Base your explanation ONLY on the measurements and documentation given. Do not invent numbers.
- Propose an ordered plan of 2-5 small steps, least disruptive first (inspect -> renice -> suspend -> terminate -> kill).
- You can NOT run commands. For a step that needs an action, output a structured action object.
  Allowed action types: none, inspect, renice, suspend, resume, terminate, kill, signal_parent.
  Use only PIDs that appear in the measurements. Never target PID 1, system or desktop processes.
- Zombies cannot be killed; act on the parent with signal_parent, then terminate the parent.
Reply with ONLY a JSON object, no markdown fences, with this shape:
{"summary": "one sentence", "root_cause": "2-4 simple sentences",
 "os_concept": "name of the OS concept and one sentence linking it",
 "steps": [{"title": "short", "detail": "1-2 sentences", "action": {"type": "renice", "pid": 1234, "process": "name", "value": 15, "reason": "why"}}]}"""

CHAT_PROMPT = """You are SysDoctor, a friendly Operating Systems troubleshooting assistant. Answer the user's
question about the current problem using the live data and documentation provided. Be short (under
150 words), simple and accurate. Never claim to have run a command. If an action is needed, tell the
user to use the step buttons, which are checked by a safety layer."""


@dataclass
class LLMStatus:
    backend: str
    online: bool
    model: str                    # model requested in settings
    resolved_model: str = ""      # exact name that will be sent to the server
    running: bool = False         # Ollama server answered
    installed: list = field(default_factory=list)
    message: str = ""
    hint: str = ""                # what the user should do to enable the LLM


class LLMClient:
    def __init__(self, backend=None, model=None):
        self.backend = (backend or config.LLM_BACKEND).lower()
        self.model = model or {"ollama": config.OLLAMA_MODEL, "openai": config.OPENAI_MODEL}.get(self.backend, "")
        self.last_error = None
        self._status = None
        self._status_ts = 0.0
        self._lc = None

    # ------------------------------------------------------------ backends
    def status(self, force=False) -> "LLMStatus":
        """Real connectivity check, cached for a few seconds so the dashboard stays fast."""
        if not force and self._status and time.time() - self._status_ts < config.LLM_STATUS_CACHE_SEC:
            return self._status
        st = self._check()
        self._status, self._status_ts = st, time.time()
        if not st.online and self.backend != "offline":
            self.last_error = st.message
        return st

    def _check(self) -> "LLMStatus":
        if self.backend == "offline":
            return LLMStatus("offline", False, "", message="Offline mode: rule-based explanations built from the "
                                                           "retrieved OS passages. No language model is used.")
        if self.backend == "openai":
            if not config.OPENAI_API_KEY:
                return LLMStatus("openai", False, self.model, message="No API key configured.",
                                 hint="Set the environment variable SYSDOCTOR_OPENAI_KEY and restart.")
            return LLMStatus("openai", True, self.model, self.model, True, message="API key configured.")
        try:
            r = requests.get(f"{config.OLLAMA_URL}/api/tags", timeout=2)
            r.raise_for_status()
            installed = [m.get("name", "") for m in r.json().get("models", [])]
        except Exception as e:
            return LLMStatus("ollama", False, self.model, running=False,
                             message=f"Ollama is not running at {config.OLLAMA_URL} ({type(e).__name__}).",
                             hint="Install Ollama from https://ollama.com/download, start it (tray icon, or run "
                                  f"`ollama serve`), then run `ollama pull {self.model}`.")
        resolved = self.resolve_model(self.model, installed)
        if not resolved:
            return LLMStatus("ollama", False, self.model, running=True, installed=installed,
                             message=f"Ollama is running but model '{self.model}' is not installed.",
                             hint=f"Run in a terminal:  ollama pull {self.model}"
                                  + (f"   (installed: {', '.join(installed)})" if installed else ""))
        return LLMStatus("ollama", True, self.model, resolved, True, installed,
                         message=f"Connected to Ollama - using '{resolved}'.")

    @staticmethod
    def resolve_model(wanted, installed):
        """Exact name > '<name>:latest' > any tag of the family when no tag was asked for."""
        if wanted in installed:
            return wanted
        base, _, tag = wanted.partition(":")
        if not tag and f"{base}:latest" in installed:
            return f"{base}:latest"
        if tag in ("", "latest"):
            fam = [n for n in installed if n.split(":")[0] == base]
            return fam[0] if fam else None
        if tag == "8b" and f"{base}:latest" in installed and base in ("llama3", "llama3.1"):
            return f"{base}:latest"            # Ollama's llama3:latest *is* the 8B model
        return None

    def available(self):
        return self.status().online

    @property
    def label(self):
        if self.backend == "offline":
            return "offline (rule-based)"
        st = self.status()
        return f"{self.backend}:{st.resolved_model or self.model}"

    def chat(self, system, user, json_mode=False):
        """One real request to the configured model. Raises on failure - never fabricates a reply."""
        st = self.status()
        if not st.online:
            raise RuntimeError(st.message)
        if self.backend == "ollama":
            model = st.resolved_model
            if not json_mode:
                try:                                      # LangChain (Layer 4 framework) when installed
                    from langchain_ollama import ChatOllama
                    from langchain_core.messages import SystemMessage, HumanMessage
                    if self._lc is None or self._lc.model != model:
                        self._lc = ChatOllama(model=model, base_url=config.OLLAMA_URL,
                                              temperature=config.LLM_TEMPERATURE)
                    return self._lc.invoke([SystemMessage(content=system), HumanMessage(content=user)]).content
                except ImportError:
                    pass                                  # REST API below does the same thing
            body = {"model": model, "stream": False, "options": {"temperature": config.LLM_TEMPERATURE},
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
            if json_mode:
                body["format"] = "json"
            r = requests.post(f"{config.OLLAMA_URL}/api/chat", json=body, timeout=config.LLM_TIMEOUT_SEC)
            if r.status_code != 200:
                raise RuntimeError(f"Ollama returned HTTP {r.status_code}: {r.text[:200]}")
            return r.json()["message"]["content"]
        if self.backend == "openai":
            body = {"model": self.model, "temperature": config.LLM_TEMPERATURE,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            r = requests.post(f"{config.OPENAI_BASE_URL}/chat/completions", json=body, timeout=config.LLM_TIMEOUT_SEC,
                              headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"})
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        raise RuntimeError("offline")

    # ----------------------------------------------------------- prompting
    @staticmethod
    def build_context(issue, live, docs, previous_fix=None, plan=None):
        doc_text = "\n\n".join(f"[{i+1}] ({d['source']} - {d['section']})\n{d['text']}" for i, d in enumerate(docs))
        plan_text = "\n".join(f"{i+1}. {s.title} -> action={s.action.to_dict()}" for i, s in enumerate(plan or []))
        if issue is None:
            return (f"LIVE SYSTEM DATA:\n{json.dumps(live, default=str, indent=1)}\n\n"
                    f"DOCUMENTATION EXCERPTS:\n{doc_text}")
        return (f"DETECTED PROBLEM: {issue.title}\nSeverity: {issue.severity}\nDetails: {issue.details}\n"
                f"Evidence: {json.dumps(issue.evidence, default=str)}\n\n"
                f"LIVE SYSTEM DATA:\n{json.dumps(live, default=str, indent=1)}\n\n"
                f"PREVIOUS SIMILAR INCIDENT: {previous_fix or 'none'}\n\n"
                f"REFERENCE PLAN (safe default):\n{plan_text}\n\n"
                f"DOCUMENTATION EXCERPTS:\n{doc_text}")

    @staticmethod
    def _parse_json(text):
        text = text.strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
        m = re.search(r"\{.*\}", text, flags=re.S)
        return json.loads(m.group(0) if m else text)

    # ------------------------------------------------------------ diagnose
    def diagnose(self, issue, live, docs, previous_fix=None, context=None, use_rag=True, rag_query=""):
        """docs = passages retrieved from the vector index for this issue (RAG). They go into the prompt."""
        plan = default_steps(issue, context)
        docs = docs if use_rag else []
        sources = sorted({d["source"] for d in docs})
        err = None
        if self.available():
            try:
                ctx = self.build_context(issue, live, docs, previous_fix, plan)
                data = self._parse_json(self.chat(SYSTEM_PROMPT, ctx, json_mode=True))
                steps = []
                for s in data.get("steps", [])[:6]:
                    if not isinstance(s, dict):
                        continue
                    steps.append(Step(title=str(s.get("title", "Step"))[:120], detail=str(s.get("detail", "")),
                                      action=Action.from_dict(s.get("action"))))
                if not steps:
                    steps = plan
                return Diagnosis(summary=str(data.get("summary", issue.title)),
                                 root_cause=str(data.get("root_cause", "")),
                                 os_concept=str(data.get("os_concept", CONCEPTS.get(issue.issue_type, ""))),
                                 steps=steps, sources=sources, backend=self.label, previous_fix=previous_fix,
                                 used_llm=True, rag_query=rag_query, passages=docs)
            except Exception as e:
                err = f"The language model call failed ({type(e).__name__}: {str(e)[:160]}). Showing the offline plan."
                self.last_error = err
        elif self.backend != "offline":
            err = self.status().message
        d = self.offline_diagnosis(issue, docs, plan, previous_fix)
        d.llm_error, d.rag_query = err, rag_query
        return d

    @staticmethod
    def offline_diagnosis(issue, docs, plan, previous_fix=None):
        """No LLM: template explanation + the most relevant retrieved passages, quoted with their source."""
        cause = SIMPLE_CAUSE.get(issue.issue_type, "").format(proc=issue.process or "A process")
        cause = f"{cause} {issue.details}"
        concept = CONCEPTS.get(issue.issue_type, "")
        notes = []
        for d in docs[:2]:
            body = d["text"].split("\n", 1)[-1].strip()
            sentence = re.split(r"(?<=[.!?])\s", body)[0]
            notes.append(f"{sentence} *({d['source']} - {d['section']})*")
        if notes:
            concept = concept + "\n\nFrom the OS knowledge base:\n" + "\n".join(f"- {n}" for n in notes)
        return Diagnosis(summary=issue.title, root_cause=cause, os_concept=concept, steps=plan,
                         sources=sorted({d["source"] for d in docs}),
                         backend="offline (rule-based + RAG)" if docs else "offline (rule-based)",
                         previous_fix=previous_fix, used_llm=False, passages=docs)

    # ---------------------------------------------------------- follow-ups
    def answer(self, question, issue, live, docs, history=None):
        """Returns (text, used_llm). Without an LLM the most relevant retrieved passage is shown, labelled."""
        if self.available():
            try:
                convo = "\n".join(f"{r}: {m}" for r, m in (history or [])[-6:])
                user = (f"{self.build_context(issue, live, docs)}\n\nCONVERSATION SO FAR:\n{convo}\n\n"
                        f"USER QUESTION: {question}")
                return self.chat(CHAT_PROMPT, user).strip(), True
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
        if not docs:
            return "I could not find anything about that in the OS knowledge base. Try other words.", False
        best = docs[0]
        body = best["text"].split("\n", 1)[-1].strip()
        return (f"*(Offline mode - no language model. Closest passage from the knowledge base.)*\n\n"
                f"**{best['section']}** ({best['source']})\n\n{body[:700]}"), False

    def simplify(self, step_title, step_detail, issue, action_type="none"):
        if self.available():
            try:
                return self.chat("Re-explain this troubleshooting step for a complete beginner in 2-3 very short "
                                 "sentences. Use an everyday analogy. No jargon.",
                                 f"Problem: {issue.title}\nStep: {step_title}\n{step_detail}").strip()
            except Exception as e:
                self.last_error = str(e)
        analogies = {
            "renice": "Think of it as moving this program to the back of the queue: it still gets served, "
                      "just after everyone else. Nothing is lost.",
            "suspend": "It's like pressing pause on a video. Nothing is lost; you can press play later.",
            "resume": "It's like pressing play again on a paused video.",
            "terminate": "It's like asking the program politely to close, so it can tidy up and save first.",
            "kill": "It's like pulling the plug. It stops instantly, but anything unsaved is gone.",
            "signal_parent": "It's like reminding a parent to pick up the kids who have already finished school.",
            "inspect": "We just take a look at the program. Nothing on your computer changes.",
        }
        if action_type in analogies:
            return analogies[action_type]
        return f"In short: {step_detail} This step is only information - nothing on your computer changes."
