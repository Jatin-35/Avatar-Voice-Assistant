"""
build_index.py — Build the FAISS vector index for the web voice agent.

NOTE for Windows users: run with PYTHONIOENCODING=utf-8 to avoid emoji encoding errors:
  FORCE_REBUILD_INDEX=1 PYTHONIOENCODING=utf-8 python backend/scripts/build_index.py
Or set it permanently in PowerShell:
  $env:PYTHONIOENCODING="utf-8"

Indexes .md and .txt files from backend/knowledge_base/ into
backend/knowledge_base/faiss_index/.

This is SEPARATE from the phone bot's index (scripts/build_index.py at repo root).
Each interface has its own knowledge base and index.

Pipeline:
  • .md  files — heading-aware splitter (## / ### = chunk boundaries)
  • .txt files — paragraph splitter (blank-line separated)
  • .pdf / .xlsx — NOT indexed directly; convert to .md first

Usage:
    cd web/
    python backend/scripts/build_index.py

    # Force rebuild even if index already exists:
    FORCE_REBUILD_INDEX=1 python backend/scripts/build_index.py

Output:
    backend/knowledge_base/faiss_index/   ← index.faiss + index.pkl + manifest.json

Dependencies (install in your Python 3.11 venv):
    pip install langchain langchain-community langchain-huggingface faiss-cpu
                sentence-transformers rank-bm25 python-dotenv
"""

import os
import sys
import json
import time
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
# backend/scripts/build_index.py → .parent.parent = backend/
SCRIPT_DIR  = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent

KB_DIR        = BACKEND_DIR / "knowledge_base"
INDEX_DIR     = KB_DIR / "faiss_index"
INDEX_FAISS   = INDEX_DIR / "index.faiss"
INDEX_PKL     = INDEX_DIR / "index.pkl"
MANIFEST_JSON = INDEX_DIR / "manifest.json"

# Load .env from backend/
from dotenv import load_dotenv
load_dotenv(BACKEND_DIR / ".env")

# ── Config ────────────────────────────────────────────────────────────────────
EMBEDDING_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"

# Markdown: larger chunks — headings give natural boundaries
MD_CHUNK_SIZE    = 900
MD_CHUNK_OVERLAP = 150

# Text: smaller chunks — plain FAQ-style blocks
TXT_CHUNK_SIZE    = 400
TXT_CHUNK_OVERLAP = 80

# Heading-aware separator priority (try in order)
MD_SEPARATORS = [
    "\n## ",        # H2 sections
    "\n### ",       # H3 subsections
    "\n#### ",      # H4
    "\n\n---\n\n",  # horizontal-rule dividers
    "\n\n",         # paragraph breaks
    "\n",           # line breaks
    "।",            # Hindi sentence terminator
    ".",            # English sentence terminator
    " ",            # word breaks
    "",             # character fallback
]

TXT_SEPARATORS = ["\n\n", "\n", "।", ".", " ", ""]

# ── LangChain imports ─────────────────────────────────────────────────────────
try:
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    from langchain_huggingface import HuggingFaceEmbeddings
    from langchain_community.vectorstores import FAISS
    from langchain_core.documents import Document
except ImportError:
    print("❌ Missing dependencies. Run:")
    print("   pip install langchain langchain-community langchain-huggingface faiss-cpu sentence-transformers")
    sys.exit(1)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def index_exists() -> bool:
    if not INDEX_FAISS.is_file() or not INDEX_PKL.is_file():
        return False
    try:
        return INDEX_FAISS.stat().st_size > 0 and INDEX_PKL.stat().st_size > 0
    except OSError:
        return False


def load_markdown(path: Path, kb_root: Path = KB_DIR) -> list[Document]:
    if not path.exists():
        print(f"⚠️  Not found: {path}")
        return []
    source = str(path.relative_to(kb_root))   # e.g. "rag/01_aadhaar_charges_rag.md"
    print(f"📘 Loading MD:  {source}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        print(f"   └─ Empty file, skipping")
        return []
    doc = Document(page_content=text, metadata={"source": source, "type": "md"})
    print(f"   └─ {len(text):,} chars")
    return [doc]


def load_txt(path: Path, kb_root: Path = KB_DIR) -> list[Document]:
    if not path.exists():
        print(f"⚠️  Not found: {path}")
        return []
    source = str(path.relative_to(kb_root))
    print(f"📝 Loading TXT: {source}")
    text = path.read_text(encoding="utf-8")
    blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
    docs = [
        Document(page_content=block, metadata={"source": source, "type": "txt"})
        for block in blocks
    ]
    print(f"   └─ {len(docs)} blocks")
    return docs


# ── Main ──────────────────────────────────────────────────────────────────────

def build_index():
    print("\n" + "=" * 60)
    print("  Web Voice Agent — Build Knowledge Base Index")
    print("=" * 60)
    print(f"  KB folder  : {KB_DIR}")
    print(f"  Index out  : {INDEX_DIR}")
    print(f"  Embeddings : {EMBEDDING_MODEL}")
    print("=" * 60 + "\n")

    force_rebuild = _env_truthy("FORCE_REBUILD_INDEX", default=False)
    if index_exists() and not force_rebuild:
        print(f"✅ Index already exists at: {INDEX_DIR}")
        print("   Skipping rebuild. To force: FORCE_REBUILD_INDEX=1 python backend/scripts/build_index.py\n")
        return

    if not KB_DIR.exists():
        print(f"❌ knowledge_base/ directory not found: {KB_DIR}")
        print("   Create it and add your .md / .txt documents first.")
        sys.exit(1)

    # ── 1. Discover documents ─────────────────────────────────────────────────
    # Scan ALL subfolders recursively (e.g. ORG/, rag/, etc.)
    # Skip files that are meta/planning docs, not knowledge content.
    SKIP_FILES = {
        "readme.md",
        "uidai_rag_implementation_plan.md",  # planning doc, not knowledge
    }

    md_files  = sorted(f for f in KB_DIR.rglob("*.md")
                       if f.name.lower() not in SKIP_FILES
                       and "faiss_index" not in str(f))
    txt_files = sorted(f for f in KB_DIR.rglob("*.txt")
                       if "faiss_index" not in str(f))

    if not md_files and not txt_files:
        print("❌ No .md or .txt documents found in knowledge_base/ (or subfolders)")
        print(f"   Add your documents to: {KB_DIR}")
        print("   Supported: .md (recommended), .txt")
        print("   Subfolders like ORG/ and rag/ are scanned automatically.")
        sys.exit(1)

    print(f"📂 Found {len(md_files)} .md file(s) and {len(txt_files)} .txt file(s)")
    for f in md_files:
        rel = f.relative_to(KB_DIR)
        print(f"   ✓ {rel}")
    for f in txt_files:
        rel = f.relative_to(KB_DIR)
        print(f"   ✓ {rel}")
    print()

    # ── 2. Load ───────────────────────────────────────────────────────────────
    md_docs:  list[Document] = []
    txt_docs: list[Document] = []

    for f in md_files:
        md_docs.extend(load_markdown(f, KB_DIR))
    for f in txt_files:
        txt_docs.extend(load_txt(f, KB_DIR))

    raw_count = len(md_docs) + len(txt_docs)
    print(f"\n📦 Total raw documents loaded: {raw_count} ({len(md_docs)} MD, {len(txt_docs)} TXT)")

    # ── 3. Chunk ──────────────────────────────────────────────────────────────
    print("\n✂️  Chunking documents...")
    md_splitter = RecursiveCharacterTextSplitter(
        chunk_size=MD_CHUNK_SIZE,
        chunk_overlap=MD_CHUNK_OVERLAP,
        separators=MD_SEPARATORS,
    )
    txt_splitter = RecursiveCharacterTextSplitter(
        chunk_size=TXT_CHUNK_SIZE,
        chunk_overlap=TXT_CHUNK_OVERLAP,
        separators=TXT_SEPARATORS,
    )

    md_chunks  = md_splitter.split_documents(md_docs)  if md_docs  else []
    txt_chunks = txt_splitter.split_documents(txt_docs) if txt_docs else []
    chunks = md_chunks + txt_chunks

    print(f"   → {len(chunks)} total chunks ({len(md_chunks)} from MD, {len(txt_chunks)} from TXT)")

    if not chunks:
        print("❌ No chunks produced — check that your documents are non-empty.")
        sys.exit(1)

    # ── 4. Load embedding model ───────────────────────────────────────────────
    print(f"\n🔗 Loading embedding model: '{EMBEDDING_MODEL}' (first run downloads ~120 MB)...")
    t0 = time.time()
    embeddings = HuggingFaceEmbeddings(
        model_name=EMBEDDING_MODEL,
        encode_kwargs={"normalize_embeddings": True},
    )
    print(f"   → Model loaded in {time.time()-t0:.1f}s")

    # ── 5. Build FAISS index ──────────────────────────────────────────────────
    print(f"\n🧮 Building FAISS index ({len(chunks)} chunks)...")
    print("   (This may take 10–60s depending on document size and CPU)")
    t0 = time.time()
    vector_store = FAISS.from_documents(chunks, embeddings)
    elapsed = time.time() - t0
    print(f"   → Index built in {elapsed:.1f}s")

    # ── 6. Save to disk ───────────────────────────────────────────────────────
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    vector_store.save_local(str(INDEX_DIR))
    print(f"\n✅ FAISS index saved → {INDEX_DIR}")

    # ── 7. Manifest ───────────────────────────────────────────────────────────
    indexed_files = sorted({d.metadata.get("source", "unknown") for d in (md_docs + txt_docs)})
    manifest = {
        "total_chunks":      len(chunks),
        "md_chunks":         len(md_chunks),
        "txt_chunks":        len(txt_chunks),
        "embedding_model":   EMBEDDING_MODEL,
        "md_chunk_size":     MD_CHUNK_SIZE,
        "md_chunk_overlap":  MD_CHUNK_OVERLAP,
        "txt_chunk_size":    TXT_CHUNK_SIZE,
        "txt_chunk_overlap": TXT_CHUNK_OVERLAP,
        "documents":         [{"file": f} for f in indexed_files],
    }
    with open(MANIFEST_JSON, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)

    print(f"📋 Manifest saved → {MANIFEST_JSON}")

    print("\n" + "=" * 60)
    print("  INDEX BUILD COMPLETE")
    print("=" * 60)
    print(f"  Total chunks : {len(chunks)}")
    print(f"  Indexed files: {', '.join(indexed_files)}")
    print(f"  Index path   : {INDEX_DIR}")
    print("=" * 60)
    print("\n🎉 Start the backend now — the index loads automatically.\n")
    print("   cd backend && python main.py\n")


if __name__ == "__main__":
    build_index()
