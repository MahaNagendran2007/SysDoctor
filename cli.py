"""Headless mode: monitor in the terminal, print alerts and optionally walk through a guided fix.

    python cli.py              # monitor and print alerts
    python cli.py --guide      # also offer the step-by-step assistant when an alert appears
    python cli.py --once       # one analysis pass and exit
"""
import argparse
import time

from sysdoctor.core import SysDoctor
from sysdoctor.models import Issue


def fmt(v, unit=""):
    return f"{v:.1f}{unit}" if isinstance(v, (int, float)) else str(v)


def guided(doc, alert):
    s = doc.start_guide(alert)
    print("\n" + "=" * 70)
    for role, msg in s.messages:
        print(f"[{role}] {msg}\n")
    shown = len(s.messages)
    while s.status == "guiding" and s.current_step:
        st = s.current_step
        dec = s.current_safety()
        print(f"\nSTEP {s.step_index + 1}/{len(s.diagnosis.steps)}: {st.title}\n  {st.detail}")
        if st.action.type not in ("none",):
            print(f"  Action: {st.action.type}  Command: {dec.command_preview}  Risk: {dec.risk}\n  {dec.reason}")
        choice = input("  [y] do it  [s] skip  [e] explain simpler  [q] quit  or type a question: ").strip()
        if choice.lower() == "y":
            s.approve()
        elif choice.lower() == "s":
            s.skip()
        elif choice.lower() == "e":
            s.simplify()
        elif choice.lower() == "q":
            s.close()
        elif choice:
            s.ask(choice)
        for role, msg in s.messages[shown:]:
            print(f"\n[{role}] {msg}")
        shown = len(s.messages)
    print(f"\nSession finished: {s.status}\n" + "=" * 70)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--guide", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--llm", default=None, help="ollama | openai | offline")
    a = ap.parse_args()
    doc = SysDoctor(start_monitor=not a.once, llm_backend=a.llm)
    st = doc.status()
    print(f"SysDoctor | RAG: {st['rag_backend']} ({st['rag_chunks']} chunks) | LLM: {st['llm']} "
          f"({'online' if st['llm_online'] else 'offline fallback'}) | LangGraph: {st['langgraph']}")
    if a.once:
        for _ in range(3):
            doc.monitor.collector.collect_once()
            time.sleep(1)
        issues, preds = doc.engine.analyse()
        m = doc.db.recent_metrics(1)[0]
        print(f"CPU {fmt(m['cpu_percent'],'%')} MEM {fmt(m['mem_percent'],'%')} SWAP {fmt(m['swap_percent'],'%')} "
              f"DISK {fmt(m['disk_percent'],'%')} IOWAIT {fmt(m['iowait'],'%')} ZOMBIES {m['proc_zombie']}")
        for i in issues:
            print(f"  [{i.severity}] {i.title} - {i.details}")
        if not issues:
            print("  No problems detected.")
        return
    seen = set()
    try:
        while True:
            time.sleep(doc.monitor.interval)
            m = doc.db.recent_metrics(1)
            if m:
                m = m[0]
                print(f"\rCPU {m['cpu_percent']:5.1f}%  MEM {m['mem_percent']:5.1f}%  SWAP {m['swap_percent']:5.1f}%  "
                      f"IOWAIT {m['iowait']:4.1f}%  ZOMBIES {m['proc_zombie']}  samples={doc.monitor.samples}   ",
                      end="", flush=True)
            for alert in doc.db.open_alerts():
                if alert["id"] in seen:
                    continue
                seen.add(alert["id"])
                print(f"\n\n⚠  [{alert['severity'].upper()}] {alert['title']}\n   {alert['details']}")
                if a.guide and input("   Start guided troubleshooting? [y/N] ").lower() == "y":
                    guided(doc, alert)
    except KeyboardInterrupt:
        doc.monitor.stop()
        print("\nStopped.")


if __name__ == "__main__":
    main()
