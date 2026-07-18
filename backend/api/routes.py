"""
api/routes.py — REST API endpoints for the web voice agent.

Endpoints:
  GET  /health          — health check
  GET  /api/sessions    — list active sessions
  DELETE /api/sessions/{session_id} — close a session
  GET  /api/stats       — performance metrics
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from core.session import session_manager

router = APIRouter()


@router.api_route("/health", methods=["GET", "HEAD"])
async def health_check():
    """Health check endpoint — used by load balancers and monitoring.

    Accepts HEAD as well as GET: uptime pingers (e.g. UptimeRobot's default
    monitor) probe with HEAD, which returned 405 when this was GET-only.
    """
    from rag.service import rag_service
    rag_ready = getattr(rag_service, "is_ready", False)
    sessions_active = await session_manager.count()
    return {
        "status": "healthy",
        "rag_ready": rag_ready,
        "sessions_active": sessions_active,
    }


@router.get("/api/sessions")
async def list_sessions():
    """Return all active session summaries."""
    sessions = await session_manager.list_sessions()
    return {"sessions": sessions, "count": len(sessions)}


@router.delete("/api/sessions/{session_id}")
async def close_session(session_id: str):
    """Forcefully close a session (e.g. for admin cleanup)."""
    session = await session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session {session_id!r} not found")

    # Stop STT if active
    stt = getattr(session, "stt_handler", None)
    if stt and getattr(stt, "is_running", False):
        try:
            await stt.stop_async()
        except Exception as e:
            print(f"[Routes] STT stop error for {session_id}: {e}")

    # Clear audio manager
    am = getattr(session, "audio_manager", None) or (
        getattr(session.interruption_state, "current_audio_manager", None)
        if session.interruption_state else None
    )
    if am:
        try:
            await am.clear()
        except Exception:
            pass

    # Close WebSocket
    ws = getattr(session, "websocket", None)
    if ws:
        try:
            await ws.close()
        except Exception:
            pass

    deleted = await session_manager.delete_session(session_id)
    return {"success": deleted, "session_id": session_id}


@router.get("/api/stats")
async def get_stats():
    """Return performance metrics (approximate — in-memory only)."""
    sessions_active = await session_manager.count()
    sessions = await session_manager.list_sessions()

    # Aggregate conversation stats
    total_turns = sum(s.get("conversation_turns", 0) for s in sessions)

    # TTS cache stats
    from voice.tts import response_cache
    cache_total = len(response_cache.cache)
    cache_hits = 0  # Not tracked per-request in this impl; use Prometheus in prod

    return {
        "sessions_active": sessions_active,
        "total_conversation_turns": total_turns,
        "tts_cache_entries": cache_total,
        "sessions": sessions,
    }
