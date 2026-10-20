"""Layer 9 - Streamlit dashboard.

    python run_sysdoctor.py        (recommended - applies the Windows .js MIME fix before the server starts)
    streamlit run app.py

Left: live vitals, graphs, process table, predictions, history.
Right: alerts and the AI troubleshooting assistant (one step at a time, safety-checked).
"""
import datetime as dt
import html
import json
import mimetypes

# Some Windows installs map ".js" to "text/plain" in the registry. Browsers then refuse Streamlit's lazily
# loaded ES-module chunks ("Failed to fetch dynamically imported module"). Tornado looks the type up on
# every request, so correcting it here fixes the running server as well.
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")

import pandas as pd                                     # noqa: E402
import plotly.graph_objects as go                       # noqa: E402
import streamlit as st                                  # noqa: E402

from sysdoctor import config                            # noqa: E402
from sysdoctor.collector import CAPABILITIES, LINUX_ONLY_LABEL   # noqa: E402
from sysdoctor.core import SysDoctor                    # noqa: E402
from sysdoctor.demo import DEMOS                        # noqa: E402
from sysdoctor.guide import live_context                # noqa: E402
from sysdoctor.models import ISSUE_LABELS               # noqa: E402
from sysdoctor.verifier import comparison_rows          # noqa: E402

st.set_page_config(page_title="SysDoctor", page_icon="🩺", layout="wide")

PAPER, INK, MUTED, LINE = "#F3F6F8", "#16324F", "#5F7186", "#D5DEE6"
TEAL, AMBER, ALARM, SKY = "#0E8A8A", "#C98300", "#C22F3A", "#4A78B5"
SEV = {"critical": ALARM, "warning": AMBER, "info": SKY}
SEV_WORD = {"critical": "High", "warning": "Medium", "info": "Low"}

st.markdown(f"""
<link href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:wght@400;700&display=swap" rel="stylesheet">
<style>
html, body, [class*="css"], .stMarkdown, .stButton button, .stTextInput input {{
  font-family: 'Atkinson Hyperlegible', 'Segoe UI', system-ui, sans-serif; }}
.stApp {{ background: {PAPER}; color: {INK}; }}
h1, h2, h3 {{ color: {INK}; letter-spacing: -0.01em; }}
.verdict {{ font-size: 2.1rem; font-weight: 700; line-height: 1.15; margin: 0 0 .25rem 0; }}
.verdict-sub {{ color: {MUTED}; font-size: 1rem; margin-bottom: 1.2rem; }}
.vital {{ border-left: 4px solid var(--c); padding: .2rem 0 .2rem .8rem; }}
.vital .v {{ font-size: 2rem; font-weight: 700; font-variant-numeric: tabular-nums; color: var(--c); }}
.vital .l {{ color: {MUTED}; font-size: .9rem; }}
.alert {{ border-left: 5px solid var(--c); background: white; padding: .7rem .9rem; margin-bottom: .6rem;
          border-radius: 0 6px 6px 0; }}
.alert b {{ color: var(--c); }}
.alert table {{ border-collapse: collapse; margin: .35rem 0; font-size: .9rem; }}
.alert td {{ padding: 0 .6rem 0 0; border: none; vertical-align: top; }}
.alert td:first-child {{ color: {MUTED}; }}
.step {{ background: white; border: 1px solid {LINE}; border-radius: 8px; padding: .9rem 1rem; }}
.cmd {{ font-family: ui-monospace, Menlo, Consolas, monospace; background: {PAPER}; padding: .15rem .4rem;
        border-radius: 4px; }}
.risk {{ font-weight: 700; color: var(--c); }}
.msg-system {{ color: {MUTED}; font-size: .92rem; border-left: 2px solid {LINE}; padding-left: .6rem; margin: .4rem 0; }}
.msg-user {{ text-align: right; margin: .5rem 0; }}
.msg-user span {{ background: {INK}; color: white; padding: .35rem .7rem; border-radius: 12px 12px 2px 12px;
                  display: inline-block; max-width: 90%; text-align: left; }}
.mode {{ display: inline-block; padding: .1rem .5rem; border-radius: 4px; font-size: .85rem;
         border: 1px solid var(--c); color: var(--c); }}
.na {{ color: {MUTED}; font-style: italic; }}
</style>""", unsafe_allow_html=True)


@st.cache_resource
def get_doctor():
    return SysDoctor(start_monitor=True)


doc = get_doctor()
ss = st.session_state
ss.setdefault("auto", True)
ss.setdefault("general_chat", [])


def color_for(v, warn, crit):
    return ALARM if v >= crit else AMBER if v >= warn else TEAL


def fmt_ts(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else ""


def esc(x):
    return html.escape(str(x)) if x is not None else ""


def mode_badge(used_llm, backend):
    if used_llm:
        return f"<span class='mode' style='--c:{TEAL}'>LLM · {esc(backend)}</span>"
    return f"<span class='mode' style='--c:{MUTED}'>Offline rule-based · no LLM</span>"


# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.header("Settings")
    modes = {"ollama": "Ollama (local LLM)", "offline": "Offline (rule-based)", "openai": "OpenAI-compatible API"}
    backend = st.selectbox("Language model", list(modes), format_func=modes.get,
                           index=list(modes).index(doc.llm.backend))
    if backend == "ollama":
        cur = doc.llm.status()
        options = sorted(set(cur.installed + [doc.llm.model if doc.llm.backend == "ollama" else config.OLLAMA_MODEL]))
        model = st.selectbox("Model", options, index=options.index(
            doc.llm.model if doc.llm.backend == "ollama" and doc.llm.model in options else options[0]),
            help="Installed Ollama models are listed. Pull others with `ollama pull <name>`.")
    elif backend == "openai":
        model = st.text_input("Model name", value=doc.llm.model if doc.llm.backend == "openai" else config.OPENAI_MODEL)
    else:
        model = None
    if backend != doc.llm.backend or (model and model != doc.llm.model):
        doc.set_llm(backend, model)
    if st.button("Re-check connection"):
        doc.llm.status(force=True)
    ss.auto = st.toggle("Live refresh", value=ss.auto, help=f"Refresh every {config.SAMPLE_INTERVAL_SEC:.0f} s")
    s = doc.status()
    lst = s["llm_status"]
    st.markdown("---")
    if lst.online:
        st.markdown(f"**Model:** {s['llm']} — connected")
    elif backend == "offline":
        st.markdown("**Model:** offline (rule-based) — explanations are built from templates and the "
                    "retrieved knowledge passages; no language model is used.")
    else:
        st.markdown(f"**Model:** {backend}:{doc.llm.model} — not available, using offline mode")
        st.caption(lst.message)
        if lst.hint:
            st.code(lst.hint, language=None)
    kb_q = getattr(doc.kb, "queries", 0)
    st.markdown(f"**Knowledge base:** {s['rag_chunks']} passages · {s['rag_backend']} · {kb_q} searches so far")
    st.markdown(f"**Guide engine:** {'LangGraph' if s['langgraph'] else 'built-in controller'}")
    st.markdown(f"**Samples collected:** {s['samples']} every {s['interval']:.0f} s")
    missing = [k for k, v in s["capabilities"].items() if not v]
    st.markdown(f"**Platform:** {s['platform']}")
    if missing:
        nice = {"iowait": "I/O wait", "swap_activity": "swap in/out", "major_faults": "major page faults",
                "disk_busy": "disk busy %", "zombies": "zombie detection", "page_faults_total": "page faults",
                "process_io": "per-process disk I/O", "disk_latency": "disk latency"}
        st.caption(f"{LINUX_ONLY_LABEL}: " + ", ".join(nice.get(m, m) for m in missing) + ".")
    if s["collector_error"]:
        st.error(f"Collector: {s['collector_error']}")

    st.markdown("---")
    st.subheader("Demonstration")
    st.caption("Controlled test programs. They stop by themselves (2-3 min) or with Stop.")
    for key, d in DEMOS.items():
        c1, c2 = st.columns([1.3, 1])
        running = doc.demos.running(key)
        c1.markdown(f"**{d['label']}**" + (f"<br><small>running · PID {doc.demos.pid(key)}</small>" if running else ""),
                    unsafe_allow_html=True)
        if running:
            if c2.button("Stop", key=f"stop_{key}"):
                doc.demos.stop(key)
                st.rerun()
        elif c2.button("Start", key=f"start_{key}"):
            doc.demos.start(key)
            st.rerun()
        st.caption(d["expect"])


# ------------------------------------------------------------- live section
def chart(df, cols, names, colors, title, ymax=None, unit="%"):
    fig = go.Figure()
    for c, n, col in zip(cols, names, colors):
        fig.add_trace(go.Scatter(x=df["time"], y=df[c], name=n, mode="lines", line=dict(color=col, width=2)))
    fig.update_layout(title=dict(text=title, font=dict(size=15, color=INK)), height=250,
                      margin=dict(l=10, r=10, t=40, b=10), paper_bgcolor="white", plot_bgcolor="white",
                      legend=dict(orientation="h", y=-0.2), font=dict(color=INK),
                      yaxis=dict(range=[0, ymax] if ymax else None, gridcolor=LINE, ticksuffix=unit),
                      xaxis=dict(gridcolor=LINE))
    return fig


def has(df, col):
    return col in df and df[col].notna().any()


def live_panel():
    metrics = doc.db.recent_metrics(240)
    if not metrics:
        st.info("Collecting the first samples. The dashboard fills in within a few seconds.")
        return
    m = metrics[-1]
    open_alerts = doc.db.open_alerts()
    crit = [a for a in open_alerts if a["severity"] == "critical"]
    if not open_alerts:
        verdict, sub, vc = "Your computer is healthy", "No problems detected in the latest samples.", TEAL
    else:
        n = len(open_alerts)
        verdict = f"{n} problem{'s' if n > 1 else ''} need{'s' if n == 1 else ''} attention"
        sub = "Pick one on the right and the assistant will walk you through a safe fix."
        vc = ALARM if crit else AMBER
    st.markdown(f"<div class='verdict' style='color:{vc}'>{verdict}</div><div class='verdict-sub'>{sub} "
                f"Last sample {fmt_ts(m['ts'])}.</div>", unsafe_allow_html=True)

    cpu_d = []
    if m.get("load1") is not None:
        cpu_d.append(f"load {m['load1']:.1f}")
    cpu_d.append(f"I/O wait {m['iowait']:.0f}%" if m.get("iowait") is not None else "I/O wait: Linux only")
    if m.get("swap_in_sec") is not None:
        swap_d = f"{(m['swap_in_sec'] or 0) + (m['swap_out_sec'] or 0):.0f} pages/s moved"
    else:
        swap_d = f"{m['swap_used_mb']/1024:.1f} GB page file used"
    disk_d = f"{m['disk_free_gb']:.0f} GB free · " + (
        f"busy {m['disk_busy_percent']:.0f}%" if m.get("disk_busy_percent") is not None else
        f"{m['disk_latency_ms']:.1f} ms/request" if m.get("disk_latency_ms") is not None else "")
    vitals = [("CPU", m["cpu_percent"], " · ".join(cpu_d), 70, 90),
              ("Memory", m["mem_percent"], f"{m['mem_available_mb']/1024:.1f} GB available", 80, 92),
              ("Swap" if CAPABILITIES["swap_activity"] else "Swap / page file", m["swap_percent"], swap_d, 40, 75),
              ("Disk", m["disk_percent"], disk_d, 85, 95)]
    cols = st.columns(4)
    for col, (label, val, detail, w, c) in zip(cols, vitals):
        col.markdown(f"<div class='vital' style='--c:{color_for(val, w, c)}'><div class='v'>{val:.0f}%</div>"
                     f"<div class='l'><b>{label}</b><br>{detail}</div></div>", unsafe_allow_html=True)

    df = pd.DataFrame(metrics)
    df["time"] = pd.to_datetime(df["ts"], unit="s")
    t1, t2, t3, t4 = st.tabs(["Graphs", "Processes", "Predictions", "History"])
    with t1:
        a, b = st.columns(2)
        if has(df, "iowait"):
            a.plotly_chart(chart(df, ["cpu_percent", "iowait"], ["CPU", "I/O wait"], [INK, AMBER], "CPU", 100),
                           use_container_width=True)
        else:
            a.plotly_chart(chart(df, ["cpu_percent"], ["CPU"], [INK], "CPU", 100), use_container_width=True)
            a.caption(f"I/O wait: {LINUX_ONLY_LABEL}.")
        b.plotly_chart(chart(df, ["mem_percent", "swap_percent"], ["Memory", "Swap / page file"], [TEAL, ALARM],
                             "Memory and swap", 100), use_container_width=True)
        a.plotly_chart(chart(df, ["disk_read_mb_sec", "disk_write_mb_sec"], ["Read", "Write"], [SKY, INK],
                             "Disk throughput", unit=" MB/s"), use_container_width=True)
        if has(df, "swap_in_sec"):
            b.plotly_chart(chart(df, ["swap_in_sec", "swap_out_sec", "major_faults_sec"],
                                 ["Swap in", "Swap out", "Major faults"], [TEAL, ALARM, AMBER],
                                 "Paging activity", unit="/s"), use_container_width=True)
        elif has(df, "page_faults_sec"):
            b.plotly_chart(chart(df, ["page_faults_sec"], ["Page faults (all)"], [AMBER],
                                 "Paging activity", unit="/s"), use_container_width=True)
            b.caption(f"Swap in/out and major page faults: {LINUX_ONLY_LABEL}. Windows shows total page faults "
                      "(soft + hard) summed over all processes.")
        zomb = f"{m['proc_zombie']} zombie" if m.get("proc_zombie") is not None else "zombies: Linux only"
        st.caption(f"Processes: {m['proc_total']} total · {m['proc_running']} running · "
                   f"{m['proc_sleeping']} sleeping · {zomb} · {m['thread_total']} threads · "
                   f"{m['ctx_switches_sec']:.0f} context switches/s"
                   + (f" · disk latency {m['disk_latency_ms']:.1f} ms" if m.get("disk_latency_ms") is not None else ""))
    with t2:
        procs = pd.DataFrame(doc.db.latest_processes())
        if not procs.empty:
            flagged = {a["pid"] for a in open_alerts if a["pid"]}
            procs.insert(0, "flag", procs["pid"].map(lambda p: "⚠" if p in flagged else ""))
            if "io_mb_sec" not in procs:
                procs["io_mb_sec"] = None
            show = procs[["flag", "pid", "name", "status", "cpu_percent", "rss_mb", "mem_percent", "io_mb_sec",
                          "num_threads", "nice", "open_files", "ppid", "username"]].rename(columns={
                "cpu_percent": "CPU %", "rss_mb": "Memory MB", "mem_percent": "Mem %", "io_mb_sec": "Disk MB/s",
                "num_threads": "Threads", "nice": "Priority", "open_files": "Open files / handles",
                "ppid": "Parent", "username": "User", "flag": ""})
            sort = st.radio("Sort by", ["CPU %", "Memory MB", "Disk MB/s"], horizontal=True, key="sortproc")
            st.dataframe(show.sort_values(sort, ascending=False, na_position="last"), hide_index=True,
                         use_container_width=True, height=420)
            st.caption("CPU % is per core (100% = one full core). Priority is the nice value on Linux and the "
                       "priority class on Windows.")
    with t3:
        preds = doc.db.latest_predictions()
        if not preds:
            st.write(f"Predictions start once monitoring is running (they need at least "
                     f"{config.PREDICT_MIN_SAMPLES} samples, about "
                     f"{config.PREDICT_MIN_SAMPLES * config.SAMPLE_INTERVAL_SEC:.0f} s).")
        for p in preds:
            status = p.get("status") or "stable"
            c = {"insufficient": MUTED, "rising": ALARM if (p["minutes_to_limit"] or 99) < 10 else AMBER,
                 "at_limit": ALARM, "noisy": AMBER}.get(status, TEAL)
            head = {"insufficient": "collecting data", "rising": "rising", "at_limit": "at limit",
                    "noisy": "rising unevenly", "falling": "falling", "stable": "stable"}.get(status, status)
            msg = p.get("message") or ""
            extra = "" if status == "insufficient" else (
                f"<br><small>{p['n_samples']} samples over {p['span_min']:.1f} min · trend "
                f"{p['slope_per_min']:+.2f} pts/min · fit R² {p['r2']:.2f}</small>")
            st.markdown(f"<div class='alert' style='--c:{c}'><b>{p['resource'].capitalize()} · {head}</b> "
                        f"({p['current']:.0f}% now)<br>{esc(msg)}{extra}</div>", unsafe_allow_html=True)
        st.caption("How to read this: the trend is a robust straight-line fit (Theil-Sen) over the last few "
                   f"minutes of real samples. A time estimate is only given when usage rises steadily "
                   f"(R² ≥ {config.PREDICT_MIN_R2}); it assumes the current rate continues, so it is an early "
                   f"warning, not a guarantee. An alert is raised if the limit ({config.PREDICT_LIMIT_PERCENT:.0f}%) "
                   f"would be reached within {config.PREDICT_WARN_MINUTES:.0f} min.")
    with t4:
        stats = doc.history.stats()
        if stats:
            st.dataframe(pd.DataFrame([{"Problem": ISSUE_LABELS.get(k, k), "Times seen": v["count"],
                                        "Fixed": v["fixed"]} for k, v in stats.items()]), hide_index=True)
        inc = doc.db.incidents(50)
        if inc:
            st.subheader("Past incidents")
            st.dataframe(pd.DataFrame([{"Time": fmt_ts(i["ts"]), "Problem": ISSUE_LABELS.get(i["issue_type"]),
                                        "Program": i["process"], "Fixed": "yes" if i["fixed"] else "no",
                                        "Actions": ", ".join(a.get("type", "") for a in json.loads(i["actions"] or "[]")),
                                        "Outcome": i["outcome"]} for i in inc]), hide_index=True,
                         use_container_width=True)
        acts = [a for a in doc.db.recent_actions(100)]
        st.subheader("Action log")
        if acts:
            def summ(js):
                try:
                    d = json.loads(js) if js else None
                except ValueError:
                    return ""
                if not d:
                    return ""
                keys = [("process_cpu", "CPU", "%"), ("process_rss_mb", "RSS", "MB"), ("mem_percent", "mem", "%"),
                        ("disk_mb_sec", "disk", "MB/s")]
                out = [f"{lbl} {d[k]:.0f}{u}" for k, lbl, u in keys if isinstance(d.get(k), (int, float))]
                if d.get("process_alive") is False:
                    out.insert(0, "exited")
                return ", ".join(out)
            st.dataframe(pd.DataFrame([{
                "Time": fmt_ts(a["ts"]), "Problem": ISSUE_LABELS.get(a.get("issue_type"), a.get("issue_type") or ""),
                "Action": a["action"], "PID": a["pid"], "Program": a["process"], "Risk": a["risk"],
                "Approved by user": {1: "yes", 0: "no"}.get(a.get("approved"), "not needed"),
                "Decision": a["decision"], "Result": a["result"],
                "Before": summ(a.get("before")), "After": summ(a.get("after")),
                "Resolved": {1: "yes", 0: "no"}.get(a.get("resolved"), ""),
                "Message": a["message"]} for a in acts]), hide_index=True, use_container_width=True)
        else:
            st.write("No actions have been taken yet.")


def alerts_panel():
    alerts = doc.db.open_alerts()
    st.subheader("Alerts")
    if not alerts:
        st.markdown(f"<div class='alert' style='--c:{TEAL}'>Nothing to fix right now.</div>", unsafe_allow_html=True)
    g = doc.active_guide
    busy = g is not None and g.status == "guiding"
    for a in alerts[:8]:
        c = SEV.get(a["severity"], SKY)
        try:
            ev = json.loads(a["evidence"] or "{}")
        except ValueError:
            ev = {}
        rows = []
        target = a["process"] and f"{a['process']}" + (f" (PID {a['pid']})" if a["pid"] else "")
        rows.append(("Affected", target or ev.get("resource") or "whole system"))
        if ev.get("value") is not None:
            rows.append((ev.get("metric", "Value"), f"{ev['value']} {ev.get('unit', '')}".strip()))
        if ev.get("threshold"):
            rows.append(("Detected because", ev["threshold"]))
        if ev.get("duration_s"):
            rows.append(("Duration", f"{ev['duration_s']} s"))
        rows.append(("Detected at", fmt_ts(a["ts"])))
        rows.append(("Severity", SEV_WORD.get(a["severity"], a["severity"])))
        table = "".join(f"<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>" for k, v in rows)
        st.markdown(f"<div class='alert' style='--c:{c}'><b>{esc(ISSUE_LABELS.get(a['issue_type'], a['issue_type']))}"
                    f" detected</b><br>{esc(a['title'])}<table>{table}</table><small>{esc(a['details'])}</small></div>",
                    unsafe_allow_html=True)
        if st.button("Explain this problem", key=f"fix{a['id']}", disabled=busy):
            with st.spinner("Reading the system, searching the OS knowledge base and preparing a plan..."):
                doc.active_guide = doc.start_guide(a)
            st.rerun()
    if busy:
        st.caption("Finish or end the current assistant session to start another one.")


def render_messages(messages):
    for role, msg in messages:
        if role == "system":
            st.markdown(f"<div class='msg-system'>{esc(msg)}</div>", unsafe_allow_html=True)
        elif role == "user":
            st.markdown(f"<div class='msg-user'><span>{esc(msg)}</span></div>", unsafe_allow_html=True)
        else:
            st.markdown(msg, unsafe_allow_html=True)


def question_form(key, placeholder):
    """Text box + button in a form (replaces st.chat_input - see module docstring)."""
    with st.form(key, clear_on_submit=True):
        q = st.text_input("Ask a question", placeholder=placeholder, label_visibility="collapsed")
        sent = st.form_submit_button("Ask")
    return q.strip() if sent and q and q.strip() else None


run_every = config.SAMPLE_INTERVAL_SEC if ss.auto else None
if hasattr(st, "fragment"):
    live_panel = st.fragment(run_every=run_every)(live_panel)
    alerts_panel = st.fragment(run_every=run_every)(alerts_panel)

left, right = st.columns([2.1, 1])
with left:
    st.title("🩺 SysDoctor")
    live_panel()
with right:
    alerts_panel()
    st.markdown("---")
    st.subheader("Assistant")
    g = doc.active_guide
    if g is None:
        st.write("Choose an alert and press **Explain this problem**: I will explain what is going on and fix it "
                 "with you, one step at a time. You can also ask a general question below.")
        render_messages(ss.general_chat[-8:])
        q = question_form("general_q", "e.g. What is thrashing?")
        if q:
            with st.spinner("Searching the knowledge base..."):
                docs = doc.kb.search(q, k=3)
                live = {"system": {k: v for k, v in (doc.db.recent_metrics(1) or [{}])[0].items()
                                   if v is not None and k in ("cpu_percent", "mem_percent", "swap_percent",
                                                              "disk_percent", "iowait")}}
                ans, used = doc.llm.answer(q, None, live, docs)
            src = ", ".join(sorted({d["source"] for d in docs}))
            ss.general_chat += [("user", q), ("assistant", ans + (f"\n\n<small>{'LLM' if used else 'offline'}"
                                                                  f" · sources: {src}</small>" if src else ""))]
            st.rerun()
    else:
        d = g.diagnosis
        st.markdown(f"**{esc(g.issue.title)}**<br>{mode_badge(d.used_llm, d.backend)}", unsafe_allow_html=True)
        if d.passages:
            with st.expander(f"Knowledge retrieved (RAG) · {len(d.passages)} passages"):
                st.caption(f"Search query: {d.rag_query}")
                for p in d.passages:
                    body = p["text"].split("\n", 1)[-1].strip()
                    st.markdown(f"**{p['section']}** — {p['source']} · score {p['score']}\n\n> "
                                + body[:400].replace("\n", " ") + ("…" if len(body) > 400 else ""))
                st.caption("These passages were sent to the language model with the live measurements."
                           if d.used_llm else "Offline mode: the explanation quotes these passages directly.")
        render_messages(g.messages)
        step = g.current_step
        if step is not None:
            dec = g.current_safety()
            n, total = g.step_index + 1, len(d.steps)
            risk_c = {"none": TEAL, "low": TEAL, "medium": AMBER, "high": ALARM, "critical": ALARM}.get(dec.risk, MUTED)
            body = f"<div class='step'><b>Step {n} of {total}: {esc(step.title)}</b><br>{esc(step.detail)}"
            if step.action.type not in ("none",):
                body += (f"<br><br>Action: <span class='cmd'>{esc(step.action.type)}</span> · "
                         f"<span class='cmd'>{esc(dec.command_preview)}</span><br>"
                         f"Risk: <span class='risk' style='--c:{risk_c}'>{esc(dec.risk)}</span> — {esc(dec.reason)}")
            st.markdown(body + "</div>", unsafe_allow_html=True)
            b1, b2, b3 = st.columns(3)
            label = {"none": "Continue", "inspect": "Run check", "refresh_metrics": "Run check",
                     "recheck": "Run check"}.get(step.action.type, "Approve and run")
            if dec.allowed and dec.risk in ("high", "critical"):
                confirm = st.checkbox("I understand unsaved work in this program may be lost.", key=f"c{id(g)}{g.step_index}")
            else:
                confirm = True
            if b1.button(label, type="primary", disabled=not (dec.allowed and confirm), key=f"ok{id(g)}{g.step_index}"):
                with st.spinner("Running the step and re-checking the system..."):
                    g.approve()
                st.rerun()
            if b2.button("Skip", key=f"sk{id(g)}{g.step_index}"):
                g.skip()
                st.rerun()
            if b3.button("Explain simpler", key=f"ex{id(g)}{g.step_index}"):
                g.simplify()
                st.rerun()
        else:
            st.success("Problem fixed and verified." if g.status == "resolved" else
                       "Session finished. It has been saved to the history.")
        q = question_form("guide_q", "Ask about this problem or step...")
        if q:
            with st.spinner("Thinking..."):
                g.ask(q)
            st.rerun()
        if st.button("End session" if g.status == "guiding" else "Close"):
            if g.status == "guiding":
                g.close()
            doc.active_guide = None
            st.rerun()
