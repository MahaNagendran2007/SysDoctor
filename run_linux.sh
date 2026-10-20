#!/usr/bin/env bash
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q -r requirements.txt
if command -v ollama >/dev/null 2>&1; then (ollama serve >/dev/null 2>&1 &); fi
python run_sysdoctor.py
