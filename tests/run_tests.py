"""Runs every test_* function without pytest:  python tests/run_tests.py"""
import importlib
import sys
import traceback
from pathlib import Path

here = Path(__file__).resolve().parent
sys.path.insert(0, str(here))
ok = fail = 0
for mod in ("test_core", "test_upgrade"):
    m = importlib.import_module(mod)
    for name in sorted(n for n in dir(m) if n.startswith("test_")):
        try:
            getattr(m, name)()
            ok += 1
            print(f"PASS {mod}.{name}")
        except Exception:
            fail += 1
            print(f"FAIL {mod}.{name}")
            traceback.print_exc()
print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
