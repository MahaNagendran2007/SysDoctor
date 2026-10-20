"""Layer 3 - Knowledge Base (RAG).

Markdown documents in ./knowledge (OS concepts, man-page summaries, troubleshooting guides) are
split into overlapping chunks, embedded with sentence-transformers (all-MiniLM-L6-v2) and stored in
FAISS (default) or ChromaDB. If those libraries are not installed the module falls back to a
TF-IDF index so the project still runs on any machine.
"""
import hashlib
import json
import pickle
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import config


@dataclass
class Chunk:
    text: str
    source: str
    section: str


def _split_sections(text):
    """Split a markdown document on headings, keeping heading text with each section."""
    parts, current, heading = [], [], "Introduction"
    for line in text.splitlines():
        m = re.match(r"^#{1,3}\s+(.*)", line)
        if m:
            if current:
                parts.append((heading, "\n".join(current).strip()))
            heading, current = m.group(1).strip(), []
        else:
            current.append(line)
    if current:
        parts.append((heading, "\n".join(current).strip()))
    return [(h, b) for h, b in parts if b]


def chunk_text(text, size=None, overlap=None):
    size = size or config.CHUNK_SIZE
    overlap = overlap or config.CHUNK_OVERLAP
    if len(text) <= size:
        return [text]
    chunks, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):                        # try to end on a sentence / paragraph
            cut = max(text.rfind("\n", start, end), text.rfind(". ", start, end))
            if cut > start + size // 2:
                end = cut + 1
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]


def load_documents(folder=None):
    folder = Path(folder or config.KNOWLEDGE_DIR)
    chunks = []
    for path in sorted(folder.glob("**/*")):
        if path.suffix.lower() not in (".md", ".txt"):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for heading, body in _split_sections(text):
            for piece in chunk_text(body):
                chunks.append(Chunk(text=f"{heading}\n{piece}", source=path.name, section=heading))
    return chunks


class KnowledgeBase:
    def __init__(self, folder=None, store=None):
        self.folder = Path(folder or config.KNOWLEDGE_DIR)
        self.store = (store or config.VECTOR_STORE).lower()
        self.chunks = []
        self.backend = None
        self._embedder = None
        self._faiss = None
        self._matrix = None
        self._tfidf = None
        self._chroma = None
        self.build()

    # ----------------------------------------------------------------- build
    def _fingerprint(self):
        h = hashlib.sha1()
        for p in sorted(self.folder.glob("**/*")):
            if p.is_file():
                h.update(p.name.encode())
                h.update(str(p.stat().st_mtime_ns).encode())
        h.update(config.EMBED_MODEL.encode())
        return h.hexdigest()[:16]

    def build(self):
        self.chunks = load_documents(self.folder)
        if not self.chunks:
            self.backend = "empty"
            return
        try:
            from sentence_transformers import SentenceTransformer
            self._embedder = SentenceTransformer(config.EMBED_MODEL)
            self._build_dense()
        except Exception:
            self._embedder = None
            self._build_tfidf()

    def _build_dense(self):
        fp = self._fingerprint()
        cache = config.INDEX_DIR / f"emb_{fp}.npy"
        if cache.exists():
            emb = np.load(cache)
        else:
            emb = self._embedder.encode([c.text for c in self.chunks], normalize_embeddings=True,
                                        show_progress_bar=False).astype("float32")
            np.save(cache, emb)
        if self.store == "chroma":
            try:
                import chromadb
                client = chromadb.PersistentClient(path=str(config.INDEX_DIR / "chroma"))
                name = f"sysdoctor_{fp}"
                col = client.get_or_create_collection(name, metadata={"hnsw:space": "cosine"})
                if col.count() != len(self.chunks):
                    col.add(ids=[str(i) for i in range(len(self.chunks))], embeddings=emb.tolist(),
                            documents=[c.text for c in self.chunks],
                            metadatas=[{"source": c.source, "section": c.section} for c in self.chunks])
                self._chroma = col
                self.backend = "sentence-transformers + ChromaDB"
                return
            except Exception:
                pass
        try:
            import faiss
            index = faiss.IndexFlatIP(emb.shape[1])
            index.add(emb)
            self._faiss = index
            self.backend = "sentence-transformers + FAISS"
        except Exception:
            self._matrix = emb
            self.backend = "sentence-transformers + NumPy cosine"

    def _build_tfidf(self):
        from sklearn.feature_extraction.text import TfidfVectorizer
        vec = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), sublinear_tf=True)
        self._matrix = vec.fit_transform([c.text for c in self.chunks])
        self._tfidf = vec
        self.backend = "TF-IDF (fallback - install sentence-transformers for semantic search)"

    # ---------------------------------------------------------------- search
    def search(self, query, k=None):
        k = k or config.RAG_TOP_K
        self.queries = getattr(self, "queries", 0) + 1       # proof in the UI that RAG is really queried
        self.last_query = query
        if not self.chunks:
            return []
        k = min(k, len(self.chunks))
        if self._embedder is not None:
            q = self._embedder.encode([query], normalize_embeddings=True).astype("float32")
            if self._chroma is not None:
                res = self._chroma.query(query_embeddings=q.tolist(), n_results=k)
                ids = [int(i) for i in res["ids"][0]]
                scores = [1 - d for d in res["distances"][0]]
            elif self._faiss is not None:
                scores, ids = self._faiss.search(q, k)
                scores, ids = scores[0].tolist(), ids[0].tolist()
            else:
                sims = self._matrix @ q[0]
                ids = np.argsort(-sims)[:k].tolist()
                scores = [float(sims[i]) for i in ids]
        else:
            q = self._tfidf.transform([query])
            sims = (self._matrix @ q.T).toarray().ravel()
            ids = np.argsort(-sims)[:k].tolist()
            scores = [float(sims[i]) for i in ids]
        return [{"text": self.chunks[i].text, "source": self.chunks[i].source,
                 "section": self.chunks[i].section, "score": round(float(s), 3)}
                for i, s in zip(ids, scores) if i >= 0]

    def query_for_issue(self, issue):
        """Build a retrieval query from an Issue."""
        base = {
            "memory_leak": "memory leak process resident memory keeps growing RSS heap free",
            "high_memory": "high memory usage RAM almost full available memory swap page file close programs",
            "cpu_hog": "process using 100% CPU scheduler time slice starvation nice renice",
            "thrashing": "thrashing swapping page faults working set virtual memory",
            "zombie": "zombie process defunct parent wait SIGCHLD reaping",
            "disk_bottleneck": "disk I/O bottleneck iowait iostat uninterruptible sleep disk full",
            "anomaly": "unusual system behaviour troubleshooting first steps",
            "resource_exhaustion": "running out of memory disk space OOM killer prediction",
        }.get(issue.issue_type, issue.issue_type)
        return f"{base}. {issue.title}. {issue.details}"
