# SysDoctor — AI-Powered System Health Monitor and Troubleshooter using LLM and RAG

**Maha Vignesh Shree N · Operating Systems / Artificial Intelligence**

SysDoctor watches your computer's CPU, memory, swap, disk and processes, detects and predicts
problems, explains them in plain language using OS concepts (grounded in a RAG knowledge base), guides
you through a safe fix one step at a time, and verifies that each fix actually worked.

## Quick start (Windows 10/11 - the currently supported environment)

```bat
cd SysDoctor
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python run_sysdoctor.py
```
The dashboard opens at http://localhost:8501. (`run_windows.bat` does the same.) After updating the
project, reload the browser tab with **Ctrl+F5** so it does not reuse files from an older Streamlit build.

### Optional: local LLM with Ollama
1. Install Ollama from https://ollama.com/download and start it (tray icon).
2. `ollama pull llama3:8b`  (or `ollama pull mistral:7b` and pick it in the sidebar).
3. In the sidebar choose **Ollama (local LLM)** and press **Re-check connection**.

The sidebar always shows the real state: connected (with the exact model), Ollama not running, or model not
pulled (with the command to fix it). Without Ollama, SysDoctor works in **offline rule-based** mode: the
explanation is a template plus the retrieved knowledge passages, and it is labelled "Offline rule-based · no LLM".

Linux: `./run_linux.sh`. Terminal mode: `python cli.py --guide`.

## What is Linux-only
Windows does not expose these, so SysDoctor shows **"Available on Linux only"** instead of fake zeros:
I/O wait, swap-in/out pages, major page faults, disk busy %, zombie processes. On Windows it uses real
substitutes: total page faults/s (summed per process), disk latency (ms per request), per-process disk MB/s
and handle counts. Thrashing detection on Windows is an approximation (memory pressure + rising page file +
page-fault storm vs. this machine's baseline + disk reads) and says so in the alert.

## Controlled demonstration
From the dashboard sidebar (**Demonstration** section): Start / Stop **CPU hog**, **Memory growth**,
**Disk pressure**. Or from a second terminal:

| Problem | Command | Expected |
|---|---|---|
| CPU hog | `python stress_tests\cpu_hog.py --seconds 180` | alert after ~15-20 s |
| Memory growth | `python stress_tests\memory_leak.py --rate 20 --seconds 180` | alert after ~40-60 s, rising prediction |
| Disk pressure | `python stress_tests\disk_io.py --mb 256 --seconds 120` | alert when writes stay > 100 MB/s or latency > 50 ms |
| Zombies (Linux) | `python3 stress_tests/zombie.py --count 5 --reap-on-signal` | zombie alert, SIGCHLD to parent |
| Thrashing (Linux VM with swap) | `python3 stress_tests/thrashing.py --fraction 1.15` | swap storm alert |

Safety of the demos: they stop by themselves, the memory demo is capped at 25% of free RAM, the disk demo
writes only its own temp file (and refuses if under 2 GB would remain free) and deletes it, and the CPU demo
always leaves one core free. The dashboard's Stop button only terminates processes it started.
On a very fast NVMe drive the disk demo may not reach 100 MB/s sustained; raise `--mb` or lower the threshold
with `set SYSDOCTOR_DISK_HEAVY=50` before starting SysDoctor.

## Evaluation

```bash
python evaluation/evaluate.py                     # cpu_hog, memory_leak, zombie (Linux), disk_bottleneck
python evaluation/evaluate.py --with-thrashing    # include thrashing (VM only)
```
Measures detection accuracy, detection time, prediction accuracy, fix success rate and OS-concept coverage
of LLM+RAG vs. LLM-only explanations. Results go to `evaluation/results.csv`.

Tests: `python -m pytest -q` or, without pytest, `python tests/run_tests.py`.

## Architecture (9 layers)

```
System Data → Detection & Prediction → RAG Retrieval → LLM Explanation → Step-by-Step Guidance
            → Safety Check → User Approval → Action → Verification → History
```

| # | Layer | File | Notes |
|---|---|---|---|
| 1 | System data collector | `sysdoctor/collector.py` | psutil + /proc/vmstat every 2–5 s → SQLite (`data/sysdoctor.db`) |
| 2 | Detection & prediction | `detector.py`, `predictor.py`, `engine.py` | 5 rule detectors, Isolation Forest, linear-regression time-to-exhaustion |
| 3 | Knowledge base (RAG) | `knowledge_base.py`, `knowledge/*.md` | chunked docs → all-MiniLM-L6-v2 → FAISS (or ChromaDB); TF-IDF fallback |
| 4 | LLM reasoning | `llm.py`, `playbooks.py` | Ollama (Llama 3 8B / Mistral 7B) via LangChain, OpenAI-compatible API, or offline |
| 5 | Step-by-step guide | `guide.py` | LangGraph pipeline collect→retrieve→recall→reason→safety; one step at a time, re-plans after failed fixes |
| 6 | Safety layer + executor | `safety.py`, `executor.py` | whitelist of actions, critical-process block list, PID-reuse check, no other users, approval required, everything logged |
| 7 | Fix verification | `verifier.py` | measures before/after and re-runs the detector |
| 8 | Issue history | `history.py` | incidents table; similar past fixes are offered to the LLM |
| 9 | Dashboard | `app.py`, `run_sysdoctor.py` | Streamlit + Plotly: vitals, graphs, process table, predictions, alerts, assistant, history, action log, demo controls |

### Safety rules
The LLM never runs commands. It outputs a structured action (`inspect, refresh_metrics, recheck, renice,
suspend, resume, terminate, kill, signal_parent`); anything else (e.g. `shell`) is rejected and the step is
shown as information only. The executor maps each allowed type to one fixed psutil call and contains no
shell/subprocess code. The safety layer:
* blocks PID 0/1/2, kernel threads, desktop/session/system services (Linux, Windows, macOS lists), SysDoctor itself and its parent;
* blocks processes of other users and negative nice values (no privilege escalation);
* blocks the action if the PID now belongs to a different program (PID reuse);
* refuses to "kill" a zombie and redirects to its parent;
* requires explicit approval for every state-changing action and an extra tick-box for terminate/kill;
* logs every decision to SQLite and `data/actions.log`.

### Adding knowledge
Drop more `.md` or `.txt` files (man-page extracts, textbook notes) into `knowledge/`. The index rebuilds automatically.

## Configuration
All thresholds live in `sysdoctor/config.py` and can be overridden with environment variables, e.g.
`SYSDOCTOR_INTERVAL=2`, `SYSDOCTOR_CPU_HOG=90`, `SYSDOCTOR_OLLAMA_MODEL=mistral:7b`, `SYSDOCTOR_VECTOR_STORE=chroma`.

## Project layout
```
app.py                 Streamlit dashboard
cli.py                 terminal mode
sysdoctor/             all layers
knowledge/             RAG documents
stress_tests/          controlled problem generators
evaluation/evaluate.py evaluation metrics
tests/                 unit tests
```

## Screenshots
<img width="2850" height="1516" alt="Screenshot 2026-10-20 231801" src="https://github.com/user-attachments/assets/25d23630-9779-4c69-a9f8-5a0061679ab7" />
<img width="2838" height="1524" alt="Screenshot 2026-10-20 231749" src="https://github.com/user-attachments/assets/90dcf33e-823a-4179-95e4-46b434dfdbe0" />
<img width="2846" height="1518" alt="Screenshot 2026-10-20 231736" src="https://github.com/user-attachments/assets/901e3aef-0cf3-4ee4-a80c-bc15477e74b8" />
<img width="2854" height="1522" alt="Screenshot 2026-10-20 231721" src="https://github.com/user-attachments/assets/5b5225e4-3192-4a9e-82df-9475a8fedccd" />
<img width="2884" height="1500" alt="Screenshot 2026-10-20 231703" src="https://github.com/user-attachments/assets/8ad6409c-af7c-40d7-8f03-676fab12fcd8" />

