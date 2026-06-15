# knowledge_base/

Source documents for BotrixAI's RAG (retrieval-augmented generation).

## How to use

1. Drop `.md` (recommended) or `.txt` files in here (subfolders are scanned recursively).
2. Build the index:

   ```powershell
   cd backend
   python scripts/build_index.py
   # force a rebuild:
   $env:FORCE_REBUILD_INDEX="1"; python scripts/build_index.py
   ```

3. Restart the backend — the index in `knowledge_base/faiss_index/` loads automatically
   and `/health` will report `"rag_ready": true`.

## Notes

- `faiss_index/` is generated output and is git-ignored.
- RAG libs are optional: `pip install langchain langchain-community langchain-huggingface faiss-cpu sentence-transformers rank-bm25`.
- With no documents here, RAG stays off and the assistant answers from the LLM's general knowledge.
