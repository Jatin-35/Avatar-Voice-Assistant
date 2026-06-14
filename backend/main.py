"""
main.py — FastAPI application entry point for the web voice agent.

Lifespan:
  startup: load RAG index, pre-warm Azure OpenAI
  shutdown: close HTTP session

Mounts:
  /ws/voice        — WebSocket for browser voice sessions
  REST routes      — /health, /api/*
"""

import asyncio
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

# Disable Azure SDK CRL check before any SDK imports
os.environ.setdefault("AZURE_SDK_DISABLE_CRL_CHECK", "1")

from config import BACKEND_PORT, CORS_ORIGINS, validate_config
from api.routes import router as api_router
from api.websocket_handler import handle_voice_websocket
from llm.client import http_manager, _prewarm_azure_openai


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle handler."""
    print("=" * 60)
    print("  Web Voice Agent — Starting up")
    print("=" * 60)

    # Validate required config (raises ValueError on missing keys)
    try:
        validate_config()
        print("[Config] All required environment variables present")
    except ValueError as e:
        print(f"[Config] WARNING: {e}")
        print("[Config] Continuing with defaults — some features may not work")

    # Load RAG index (sync, runs once)
    try:
        from rag.service import rag_service
        rag_service.load()
    except Exception as e:
        print(f"[RAG] Load error (non-fatal): {e}")

    # Pre-warm Azure OpenAI in background (non-blocking)
    asyncio.create_task(_prewarm_azure_openai())

    # Pre-synthesize the fixed greeting so the first session's greeting is instant
    async def _prewarm_greeting():
        try:
            from voice.tts import synthesize_speech_azure
            from api.websocket_handler import GREETING_TEXT
            await synthesize_speech_azure(GREETING_TEXT, "hi-IN")
            print("[TTS] Greeting pre-synth complete (cached)")
        except Exception as e:
            print(f"[TTS] Greeting pre-synth failed (non-fatal): {e}")
    asyncio.create_task(_prewarm_greeting())

    print("[Server] Ready to accept connections")
    print(f"[Server] WebSocket endpoint: ws://localhost:{BACKEND_PORT}/ws/voice")
    print(f"[Server] Health check: http://localhost:{BACKEND_PORT}/health")
    print("=" * 60)

    yield  # Application runs

    # Shutdown
    print("[Server] Shutting down...")
    await http_manager.close()
    from voice.tts import close_tts_session
    await close_tts_session()
    print("[Server] HTTP sessions closed")


# ── Create FastAPI app ────────────────────────────────────────────────────────

app = FastAPI(
    title="Web Voice Agent",
    description="Real-time voice agent with Deepgram STT, Azure TTS, and Azure OpenAI LLM",
    version="1.0.0",
    lifespan=lifespan,
)

# ── CORS ──────────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── REST routes ───────────────────────────────────────────────────────────────

app.include_router(api_router)

# ── WebSocket endpoint ────────────────────────────────────────────────────────

@app.websocket("/ws/voice")
async def voice_websocket_endpoint(websocket: WebSocket):
    """Main voice WebSocket — browser connects here for a voice session."""
    await handle_voice_websocket(websocket)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=BACKEND_PORT,
        reload=False,
        log_level="info",
    )
