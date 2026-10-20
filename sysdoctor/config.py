"""Central configuration. Every value can be overridden with an environment variable."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env(name, default, cast=str):
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


# ---------------- Paths ----------------
DATA_DIR = Path(_env("SYSDOCTOR_DATA_DIR", str(BASE_DIR / "data")))
DB_PATH = DATA_DIR / "sysdoctor.db"
KNOWLEDGE_DIR = BASE_DIR / "knowledge"
INDEX_DIR = DATA_DIR / "rag_index"
ACTION_LOG = DATA_DIR / "actions.log"

# ---------------- Layer 1: collection ----------------
SAMPLE_INTERVAL_SEC = _env("SYSDOCTOR_INTERVAL", 3.0, float)       # 2-5 s as per design
TOP_PROCESSES = _env("SYSDOCTOR_TOP_PROCESSES", 40, int)           # processes stored per sample
RETENTION_HOURS = _env("SYSDOCTOR_RETENTION_HOURS", 24, float)

# ---------------- Layer 2: detection thresholds ----------------
CPU_HOG_PERCENT = _env("SYSDOCTOR_CPU_HOG", 80.0, float)           # % of one core
CPU_HOG_SAMPLES = _env("SYSDOCTOR_CPU_HOG_SAMPLES", 5, int)        # sustained samples
LEAK_MIN_SAMPLES = _env("SYSDOCTOR_LEAK_SAMPLES", 12, int)
LEAK_MIN_SLOPE_MB_MIN = _env("SYSDOCTOR_LEAK_SLOPE", 20.0, float)  # MB per minute
LEAK_MIN_R2 = _env("SYSDOCTOR_LEAK_R2", 0.85, float)
LEAK_MIN_GROWTH_MB = _env("SYSDOCTOR_LEAK_GROWTH", 50.0, float)
THRASH_MEM_PERCENT = _env("SYSDOCTOR_THRASH_MEM", 85.0, float)
THRASH_SWAP_PAGES_SEC = _env("SYSDOCTOR_THRASH_SWAP", 200.0, float)
THRASH_MAJFLT_SEC = _env("SYSDOCTOR_THRASH_MAJFLT", 100.0, float)
IOWAIT_PERCENT = _env("SYSDOCTOR_IOWAIT", 20.0, float)
DISK_BUSY_PERCENT = _env("SYSDOCTOR_DISK_BUSY", 90.0, float)
DISK_FULL_PERCENT = _env("SYSDOCTOR_DISK_FULL", 90.0, float)
DISK_WRITE_MB_SEC = _env("SYSDOCTOR_DISK_WRITE", 80.0, float)
DISK_LATENCY_MS = _env("SYSDOCTOR_DISK_LATENCY", 50.0, float)      # avg ms per I/O request (Windows + Linux)
DISK_HEAVY_MB_SEC = _env("SYSDOCTOR_DISK_HEAVY", 100.0, float)     # sustained read+write MB/s
DISK_HEAVY_SAMPLES = _env("SYSDOCTOR_DISK_HEAVY_SAMPLES", 5, int)
HIGH_MEM_PERCENT = _env("SYSDOCTOR_HIGH_MEM", 90.0, float)         # sustained high system memory
HIGH_MEM_SAMPLES = _env("SYSDOCTOR_HIGH_MEM_SAMPLES", 10, int)
WIN_THRASH_FAULT_FACTOR = _env("SYSDOCTOR_WIN_THRASH_FAULTS", 3.0, float)  # x baseline page-fault rate
ZOMBIE_MIN_COUNT = _env("SYSDOCTOR_ZOMBIE_MIN", 1, int)
ISOLATION_MIN_SAMPLES = _env("SYSDOCTOR_IF_MIN", 60, int)
ISOLATION_CONTAMINATION = _env("SYSDOCTOR_IF_CONTAM", 0.03, float)
ALERT_COOLDOWN_SEC = _env("SYSDOCTOR_ALERT_COOLDOWN", 60, int)

# ---------------- Prediction ----------------
PREDICT_WINDOW_SAMPLES = _env("SYSDOCTOR_PREDICT_WINDOW", 100, int)  # ~5 min at 3 s
PREDICT_MIN_SAMPLES = _env("SYSDOCTOR_PREDICT_MIN_SAMPLES", 20, int)  # below this: "not enough data"
PREDICT_MIN_SPAN_MIN = _env("SYSDOCTOR_PREDICT_MIN_SPAN", 1.0, float)
PREDICT_MIN_R2 = _env("SYSDOCTOR_PREDICT_MIN_R2", 0.6, float)
PREDICT_MIN_SLOPE = _env("SYSDOCTOR_PREDICT_MIN_SLOPE", 0.05, float)  # % points / min considered "rising"
PREDICT_LIMIT_PERCENT = _env("SYSDOCTOR_PREDICT_LIMIT", 95.0, float)
PREDICT_WARN_MINUTES = _env("SYSDOCTOR_PREDICT_WARN", 30.0, float)

# ---------------- Layer 3: RAG ----------------
EMBED_MODEL = _env("SYSDOCTOR_EMBED_MODEL", "all-MiniLM-L6-v2")
VECTOR_STORE = _env("SYSDOCTOR_VECTOR_STORE", "faiss")             # faiss | chroma | numpy
CHUNK_SIZE = _env("SYSDOCTOR_CHUNK_SIZE", 700, int)
CHUNK_OVERLAP = _env("SYSDOCTOR_CHUNK_OVERLAP", 120, int)
RAG_TOP_K = _env("SYSDOCTOR_RAG_K", 4, int)

# ---------------- Layer 4: LLM ----------------
LLM_BACKEND = _env("SYSDOCTOR_LLM_BACKEND", "ollama")              # ollama | openai | offline
OLLAMA_URL = _env("SYSDOCTOR_OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = _env("SYSDOCTOR_OLLAMA_MODEL", "llama3:8b")         # or mistral:7b
OPENAI_BASE_URL = _env("SYSDOCTOR_OPENAI_URL", "https://api.openai.com/v1")
OPENAI_MODEL = _env("SYSDOCTOR_OPENAI_MODEL", "gpt-4o-mini")
OPENAI_API_KEY = os.environ.get("SYSDOCTOR_OPENAI_KEY") or os.environ.get("OPENAI_API_KEY", "")
LLM_TIMEOUT_SEC = _env("SYSDOCTOR_LLM_TIMEOUT", 180, int)          # first call loads the model into RAM
LLM_STATUS_CACHE_SEC = _env("SYSDOCTOR_LLM_STATUS_CACHE", 10, int)
LLM_TEMPERATURE = _env("SYSDOCTOR_LLM_TEMPERATURE", 0.2, float)

# ---------------- Layer 7: verification ----------------
VERIFY_WAIT_SEC = _env("SYSDOCTOR_VERIFY_WAIT", 6.0, float)
VERIFY_SAMPLES = _env("SYSDOCTOR_VERIFY_SAMPLES", 3, int)

DATA_DIR.mkdir(parents=True, exist_ok=True)
INDEX_DIR.mkdir(parents=True, exist_ok=True)
