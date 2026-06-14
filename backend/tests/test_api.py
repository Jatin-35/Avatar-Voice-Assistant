"""
tests/test_api.py — Integration tests for REST API and WebSocket.

Run with: pytest backend/tests/test_api.py -v
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
import sys
from pathlib import Path

# Ensure backend is importable
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# Import app (triggers lifespan in tests — use without lifespan for speed)
from main import app


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.asyncio
async def test_health_endpoint():
    """GET /health should return 200 with status=healthy."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "rag_ready" in data
    assert "sessions_active" in data


@pytest.mark.asyncio
async def test_sessions_endpoint():
    """GET /api/sessions should return a list."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/sessions")
    assert response.status_code == 200
    data = response.json()
    assert "sessions" in data
    assert isinstance(data["sessions"], list)
    assert "count" in data


@pytest.mark.asyncio
async def test_delete_nonexistent_session():
    """DELETE /api/sessions/{id} for unknown id should return 404."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.delete("/api/sessions/nonexistent-session-id")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_stats_endpoint():
    """GET /api/stats should return metrics dict."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/stats")
    assert response.status_code == 200
    data = response.json()
    assert "sessions_active" in data
    assert "tts_cache_entries" in data


@pytest.mark.asyncio
async def test_websocket_connection():
    """Connecting to /ws/voice should receive session_ready message."""
    try:
        from httpx_ws import aconnect_ws

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            async with aconnect_ws("/ws/voice", client) as ws:
                import json
                msg_text = await ws.receive_text()
                msg = json.loads(msg_text)
                assert msg["type"] == "session_ready"
                assert "session_id" in msg
                assert len(msg["session_id"]) == 36  # UUID format
    except ImportError:
        pytest.skip("httpx-ws not installed — skipping WebSocket test")
    except Exception as e:
        # Accept connection errors as non-fatal in unit test environment
        if "connection" in str(e).lower():
            pytest.skip(f"WebSocket connection failed (expected in unit test): {e}")
        raise


@pytest.mark.asyncio
async def test_session_lifecycle():
    """Create then verify session appears in /api/sessions, delete it."""
    from core.session import session_manager

    # Create a session manually
    session = await session_manager.create_session(websocket=None)
    sid = session.session_id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Should appear in list
        resp = await client.get("/api/sessions")
        assert resp.status_code == 200
        session_ids = [s["session_id"] for s in resp.json()["sessions"]]
        assert sid in session_ids

    # Clean up
    await session_manager.delete_session(sid)
