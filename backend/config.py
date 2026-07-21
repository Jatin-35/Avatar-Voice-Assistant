"""
config.py — All environment variables in one place.
Loads from .env and raises ValueError if required keys are missing.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from the backend directory
_ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH, override=False)
# Also try loading from current working directory
load_dotenv(override=False)

# Secrets (API keys) are sourced from .env ONLY — never hardcode them here.
# Missing keys are caught by validate_config() at startup.

# ── Azure OpenAI (LLM) ────────────────────────────────────────────────────────
AZURE_OPENAI_API_KEY: str = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_ENDPOINT: str = os.getenv("AZURE_OPENAI_ENDPOINT", "")
OPENAI_API_VERSION: str = os.getenv("OPENAI_API_VERSION", "2024-12-01-preview")
AZURE_OPENAI_DEPLOYMENT: str = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")

# ── Azure Speech (TTS) ────────────────────────────────────────────────────────
AZURE_SPEECH_KEY: str = os.getenv("AZURE_SPEECH_KEY", "")
AZURE_SPEECH_REGION: str = os.getenv("AZURE_SPEECH_REGION", "centralindia")

# ── Deepgram STT ──────────────────────────────────────────────────────────────
DEEPGRAM_API_KEY: str = os.getenv("DEEPGRAM_API_KEY", "")

# ── Server ────────────────────────────────────────────────────────────────────
BACKEND_PORT: int = int(os.getenv("BACKEND_PORT", "8000"))
CORS_ORIGINS: list[str] = os.getenv(
    "CORS_ORIGINS", "http://localhost:5173,http://localhost:3000,http://127.0.0.1:5173"
).split(",")

# ── TTS voice ─────────────────────────────────────────────────────────────────
# Meera Dragon HD — natural pacing, verified on centralindia. Env can override.
TTS_VOICE_NAME: str = os.getenv("TTS_VOICE_NAME", "en-IN-Meera:DragonHDLatestNeural")
TTS_VOICE_LANGUAGE: str = os.getenv("TTS_VOICE_LANGUAGE", "hi-IN")

# ── ElevenLabs TTS ────────────────────────────────────────────────────────────
ELEVENLABS_API_KEY:   str = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID:  str = os.getenv("ELEVENLABS_VOICE_ID", "TRnaQb7q41oL7sV0w6Bu")
ELEVENLABS_MODEL_ID:  str = os.getenv("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5")


# ── RAG / Knowledge Base ──────────────────────────────────────────────────────
# Index + source docs live in backend/knowledge_base/.
# Override with KB_DIR env var if you want a custom path.
_BACKEND_DIR = Path(__file__).resolve().parent
KB_DIR: Path = Path(os.getenv("KB_DIR", str(_BACKEND_DIR / "knowledge_base")))

RAG_TOP_K:             int   = int(os.getenv("RAG_TOP_K", "6"))
RAG_MAX_CONTEXT_CHARS: int   = int(os.getenv("RAG_MAX_CONTEXT_CHARS", "2500"))
RAG_FETCH_PER_LIST:    int   = int(os.getenv("RAG_FETCH_PER_LIST", "36"))
RAG_RRF_K:             int   = int(os.getenv("RAG_RRF_K", "60"))
USE_RERANKER:          bool  = os.getenv("USE_RERANKER", "false").lower() in {"1", "true", "yes"}


def validate_config() -> None:
    """Raise ValueError if any required configuration key is missing or empty."""
    required = {
        "AZURE_OPENAI_API_KEY": AZURE_OPENAI_API_KEY,
        "AZURE_OPENAI_ENDPOINT": AZURE_OPENAI_ENDPOINT,
        "AZURE_SPEECH_KEY": AZURE_SPEECH_KEY,
        "DEEPGRAM_API_KEY": DEEPGRAM_API_KEY,
    }
    missing = [k for k, v in required.items() if not v or not v.strip()]
    if missing:
        raise ValueError(
            f"Missing required configuration keys: {', '.join(missing)}. "
            f"Check backend/.env or environment variables."
        )
    if not AZURE_SPEECH_REGION or not AZURE_SPEECH_REGION.strip():
        raise ValueError(
            "AZURE_SPEECH_REGION is required for TTS. "
            "Set it in backend/.env (e.g., AZURE_SPEECH_REGION=centralindia)"
        )
