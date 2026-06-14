"""
rag/service.py — Self-contained RAG service for the web voice agent.

Uses backend/knowledge_base/faiss_index/ — SEPARATE from the phone bot's index.
Add your own documents to backend/knowledge_base/ and run:

    python backend/scripts/build_index.py

Pipeline per query:
  1. Embed query (TTL cache)
  2. FAISS semantic top-N  ──┐
                              ├─► RRF fuse → top-N fused candidates
  3. BM25 keyword  top-N  ───┘
  4. Vestige-style domain boost on top of fused score
  5. Return top TOP_K chunks as formatted context string

Tuning via env vars (backend/.env):
  RAG_TOP_K              = 6      chunks injected into LLM
  RAG_MAX_CONTEXT_CHARS  = 2500   max total context chars
  RAG_FETCH_PER_LIST     = 36     FAISS + BM25 candidates each before RRF
  RAG_RRF_K              = 60     RRF constant
"""

import os
import re
import sys
import time
import asyncio
import concurrent.futures
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
# backend/rag/service.py → .parent.parent = backend/
_BACKEND_DIR = Path(__file__).resolve().parent.parent
INDEX_DIR = _BACKEND_DIR / "knowledge_base" / "faiss_index"

# ── Optional LangChain imports ────────────────────────────────────────────────
try:
    from langchain_huggingface import HuggingFaceEmbeddings
    from langchain_community.vectorstores import FAISS
    _HAS_LANGCHAIN = True
except ImportError:
    _HAS_LANGCHAIN = False
    print("[RAG] WARNING: langchain/faiss-cpu not installed — RAG disabled.")
    print("      Run: pip install langchain langchain-community langchain-huggingface faiss-cpu sentence-transformers")

# ── Optional BM25 import ──────────────────────────────────────────────────────
try:
    from rank_bm25 import BM25Okapi
    _HAS_BM25 = True
except ImportError:
    _HAS_BM25 = False
    print("[RAG] WARNING: rank-bm25 not installed — falling back to semantic-only.")
    print("      Run: pip install rank-bm25")

# ── Config ────────────────────────────────────────────────────────────────────
EMBEDDING_MODEL   = "paraphrase-multilingual-MiniLM-L12-v2"
RERANKER_MODEL    = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"

TOP_K             = int(os.getenv("RAG_TOP_K", "6"))
MAX_CONTEXT_CHARS = int(os.getenv("RAG_MAX_CONTEXT_CHARS", "2500"))
FETCH_PER_LIST    = int(os.getenv("RAG_FETCH_PER_LIST", "36"))
RRF_K             = int(os.getenv("RAG_RRF_K", "60"))

# Cross-encoder reranker OFF by default — adds ~3s latency on small corpora.
# Set USE_RERANKER=true in .env to enable.
USE_RERANKER = os.getenv("USE_RERANKER", "false").lower() in {"1", "true", "yes"}

_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="RAG")

# ── BM25 helpers ──────────────────────────────────────────────────────────────
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

def _tokenize(text: str) -> list:
    return _TOKEN_RE.findall(text.lower())


def _rrf_fuse(*ranked_lists, k: int = RRF_K) -> list:
    """Reciprocal Rank Fusion across multiple ranked doc-index lists."""
    scores: dict = {}
    for ranked in ranked_lists:
        for rank, idx in enumerate(ranked):
            scores[idx] = scores.get(idx, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


# ── RAGService ────────────────────────────────────────────────────────────────

class RAGService:
    """FAISS + BM25 + RRF retrieval service for the web voice agent."""

    def __init__(self):
        self._store    = None
        self._reranker = None
        self._ready    = False
        self._all_docs: list = []
        self._bm25     = None
        self._cache:    dict = {}
        self._CACHE_MAX = 200
        self._CACHE_TTL = 300.0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def load(self):
        """Load FAISS index from backend/knowledge_base/faiss_index/. Call once at startup."""
        if not _HAS_LANGCHAIN:
            print("[RAG] Skipping load — LangChain not available.")
            return

        if not INDEX_DIR.exists() or not (INDEX_DIR / "index.faiss").exists():
            print(f"[RAG] No index found at {INDEX_DIR}")
            print("[RAG] Add documents to backend/knowledge_base/ and run:")
            print("[RAG]   python backend/scripts/build_index.py")
            return

        # ── FAISS ─────────────────────────────────────────────────────────────
        try:
            print(f"[RAG] Loading FAISS index from {INDEX_DIR}...")
            embeddings = HuggingFaceEmbeddings(
                model_name=EMBEDDING_MODEL,
                encode_kwargs={"normalize_embeddings": True},
            )
            self._store = FAISS.load_local(
                str(INDEX_DIR),
                embeddings,
                allow_dangerous_deserialization=True,
            )
            self._ready = True
            print("[RAG] FAISS index loaded ✅")
        except Exception as e:
            print(f"[RAG] Failed to load FAISS index: {e}")
            return

        # ── Optional cross-encoder reranker ───────────────────────────────────
        if USE_RERANKER:
            try:
                from sentence_transformers import CrossEncoder
                print(f"[RAG] Loading reranker ({RERANKER_MODEL})...")
                self._reranker = CrossEncoder(RERANKER_MODEL)
                print("[RAG] Reranker loaded ✅ (adds ~2-3s per query on CPU)")
            except Exception as e:
                print(f"[RAG] Reranker failed to load — using RRF only. {e}")
                self._reranker = None
        else:
            self._reranker = None
            print("[RAG] Reranker OFF (set USE_RERANKER=true in .env to enable)")

        # ── BM25 corpus ───────────────────────────────────────────────────────
        if _HAS_BM25 and self._store is not None:
            try:
                t0 = time.time()
                self._all_docs = []
                for i in range(self._store.index.ntotal):
                    doc_id = self._store.index_to_docstore_id[i]
                    self._all_docs.append(self._store.docstore.search(doc_id))
                tokenized = [_tokenize(d.page_content) for d in self._all_docs]
                self._bm25 = BM25Okapi(tokenized)
                elapsed_ms = (time.time() - t0) * 1000
                print(f"[RAG] BM25 corpus built — {len(self._all_docs)} chunks in {elapsed_ms:.0f}ms ✅")
            except Exception as e:
                print(f"[RAG] BM25 init failed: {e}")
                self._bm25 = None
        elif not _HAS_BM25:
            print("[RAG] BM25 disabled (rank-bm25 not installed)")

        # ── Load manifest for summary ─────────────────────────────────────────
        import json
        manifest = {}
        manifest_path = INDEX_DIR / "manifest.json"
        if manifest_path.exists():
            with open(manifest_path) as f:
                manifest = json.load(f)

        print("=" * 60)
        print("  WEB RAG SERVICE READY")
        print("=" * 60)
        print(f"  KB path         : {INDEX_DIR.parent}")
        print(f"  Embedding model : {EMBEDDING_MODEL}")
        print(f"  Reranker        : {RERANKER_MODEL if self._reranker else 'DISABLED'}")
        print(f"  BM25 keyword    : {'ENABLED' if self._bm25 else 'DISABLED'}")
        print(f"  Total chunks    : {manifest.get('total_chunks', self._store.index.ntotal)}")
        print(f"  Top-K retrieval : {TOP_K}  (FAISS+BM25 top-{FETCH_PER_LIST} each → RRF → top-{TOP_K})")
        print(f"  Max context     : {MAX_CONTEXT_CHARS} chars")
        indexed = manifest.get("documents", [])
        if indexed:
            files = ", ".join(d.get("file", "") for d in indexed)
            print(f"  Indexed files   : {files}")
        print("=" * 60)

    @property
    def is_ready(self) -> bool:
        return self._ready and self._store is not None

    # ── Retrieval ─────────────────────────────────────────────────────────────

    def _search_sync(self, query: str) -> list:
        """Blocking FAISS + BM25 + RRF search (runs in thread pool)."""
        total_docs = self._store.index.ntotal
        fetch_k = min(FETCH_PER_LIST, total_docs)

        # Build content→position lookup (cached after first call)
        if not hasattr(self, "_content_to_pos"):
            self._content_to_pos = {
                d.page_content: i for i, d in enumerate(self._all_docs)
            }

        # ── FAISS semantic search ──────────────────────────────────────────────
        raw = self._store.similarity_search_with_score(query, k=fetch_k)
        semantic_positions: list = []
        position_to_doc:    dict = {}
        for doc, dist in raw:
            pos = self._content_to_pos.get(doc.page_content, -1 - len(semantic_positions))
            semantic_positions.append(pos)
            similarity = max(0.0, 1.0 - dist / 2.0)
            position_to_doc[pos] = (doc, similarity)

        # ── BM25 keyword search ────────────────────────────────────────────────
        bm25_positions: list = []
        if self._bm25 is not None and self._all_docs:
            try:
                tokens = _tokenize(query)
                if tokens:
                    scores = self._bm25.get_scores(tokens)
                    ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
                    bm25_positions = [i for i in ranked if scores[i] > 0][:fetch_k]
                    for pos in bm25_positions:
                        if pos not in position_to_doc:
                            position_to_doc[pos] = (self._all_docs[pos], 0.0)
            except Exception as e:
                print(f"[RAG] BM25 scoring error: {e}")

        if not semantic_positions and not bm25_positions:
            return []

        # ── RRF fusion ─────────────────────────────────────────────────────────
        fused = _rrf_fuse(semantic_positions, bm25_positions, k=RRF_K)
        top_positions = [pos for pos, _ in fused[:fetch_k]]

        results = []
        for pos in top_positions:
            entry = position_to_doc.get(pos)
            if entry is None:
                continue
            doc, similarity = entry
            results.append({
                "doc":              doc,
                "similarity_score": similarity,
                "position":         pos,
            })

        if not results:
            return []

        print(f"[RAG] Fused {len(semantic_positions)} semantic + {len(bm25_positions)} BM25 → {len(results)} candidates")

        # ── Optional cross-encoder rerank ──────────────────────────────────────
        if self._reranker and len(results) > 1:
            try:
                t0 = time.time()
                pairs = [[query, r["doc"].page_content] for r in results]
                rerank_scores = self._reranker.predict(pairs, show_progress_bar=False)
                for r, score in zip(results, rerank_scores):
                    r["rerank_score"] = float(score)
                print(f"[RAG] Reranked {len(results)} candidates in {(time.time()-t0)*1000:.0f}ms")
            except Exception as e:
                print(f"[RAG] Reranking failed, using vector scores: {e}")
                for r in results:
                    r["rerank_score"] = r["similarity_score"]
        else:
            for r in results:
                r["rerank_score"] = r["similarity_score"]

        # ── Sort and return top-K ──────────────────────────────────────────────
        results.sort(key=lambda x: x["rerank_score"], reverse=True)
        return results[:TOP_K]

    async def retrieve_context_with_sources(self, query: str):
        """
        Async retrieval. Returns (context_string, sources).
        sources is a list of {"source": str, "score": float} for UI display.
        Returns ("", []) if RAG unavailable or no relevant results found.
        """
        if not self.is_ready:
            return "", []

        # TTL cache check
        cache_key = " ".join(query.strip().lower().split())
        now = time.time()
        if cache_key in self._cache:
            ctx, sources, ts = self._cache[cache_key]
            if now - ts < self._CACHE_TTL:
                print(f"[RAG] Cache hit ({len(ctx)} chars)")
                return ctx, sources
            del self._cache[cache_key]

        try:
            loop = asyncio.get_running_loop()
            results = await loop.run_in_executor(_executor, self._search_sync, query)

            if not results:
                print("[RAG] No relevant chunks found")
                return "", []

            parts = []
            sources = []
            total_chars = 0
            for r in results:
                snippet = r["doc"].page_content.strip()
                source  = r["doc"].metadata.get("source", "knowledge base")
                entry   = f"[{source}] {snippet}"
                if total_chars + len(entry) > MAX_CONTEXT_CHARS:
                    continue
                parts.append(entry)
                sources.append({
                    "source": source,
                    "score": round(float(r.get("rerank_score", r["similarity_score"])), 3),
                })
                total_chars += len(entry)
                print(
                    f"[RAG] sim={r['similarity_score']:.2f} "
                    f"rerank={r.get('rerank_score', 0):.2f} "
                    f"src={source}"
                )

            if not parts:
                return "", []

            context = "\n\n---\n\n".join(parts)
            print(f"[RAG] Injecting {len(parts)} chunk(s), {total_chars} chars")

            # Store in TTL cache (evict oldest on overflow)
            if len(self._cache) >= self._CACHE_MAX:
                oldest = min(self._cache.items(), key=lambda x: x[1][2])
                del self._cache[oldest[0]]
            self._cache[cache_key] = (context, sources, now)

            return context, sources

        except Exception as e:
            print(f"[RAG] retrieve_context error: {e}")
            return "", []

    async def retrieve_context(self, query: str) -> str:
        """Back-compat wrapper — returns just the context string."""
        ctx, _ = await self.retrieve_context_with_sources(query)
        return ctx


# ── Global singleton ──────────────────────────────────────────────────────────
rag_service = RAGService()


# ── Trivial utterance filter ──────────────────────────────────────────────────

def _is_trivial_for_rag(text: str) -> bool:
    """Return True for filler/greeting utterances where RAG won't help.

    Saves ~2s of embedding + search latency on turns like "haan", "okay",
    "hello", "नमस्ते". The LLM answers these directly from conversation history.
    """
    raw = re.sub(
        r'\[DETECTED_LANGUAGE:\s*\w+\s*\]\s*', '', text or '', flags=re.IGNORECASE
    ).strip()
    if not raw:
        return True
    words = raw.split()
    if len(words) <= 2:
        return True
    trivial_starts = (
        "hello", "hi ", "namaste", "नमस्ते", "thank", "thanks",
        "okay", "ok ", "ok.", "ok,", "achha", "अच्छा",
        "ji", "जी", "haan", "हाँ", "han", "yes ", "yes.", "yes,",
        "no ", "no.", "no,", "nahi", "नहीं", "bye", "alvida",
    )
    low = raw.lower()
    if any(low == s.strip() or low.startswith(s) for s in trivial_starts) and len(words) <= 4:
        return True
    return False
