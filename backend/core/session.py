"""
session.py — In-memory session manager for web voice agent.

Each session tracks: session_id, created_at, conversation_history,
websocket ref, interruption_state, bot_speaking_state.
Thread-safe via asyncio.Lock.
"""

import uuid
import asyncio
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List


class Session:
    """Represents a single voice agent session."""

    def __init__(self, session_id: str, websocket=None):
        self.session_id: str = session_id
        self.created_at: datetime = datetime.now(timezone.utc)
        self.websocket = websocket
        self.conversation_history: List[Dict[str, str]] = []
        self.bot_speaking_state: Dict[str, bool] = {"speaking": False}
        self.interruption_state = None  # Set by pipeline on first use
        self.audio_manager = None       # Current AudioQueueManager
        self.stt_handler = None         # DeepgramSTTHandler for this session
        self.utterance_buffer = None    # UtteranceBuffer for this session
        # STT gate — timestamp-based; transcripts arriving before this are dropped
        self.stt_discard_until: float = 0.0
        # Last detected language from STT
        self.detected_language: str = "HINDI"
        # Whether STT session has been started
        self.stt_started: bool = False
        # Whether greeting has been sent
        self.greeting_sent: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "created_at": self.created_at.isoformat(),
            "conversation_turns": len(self.conversation_history),
            "bot_speaking": self.bot_speaking_state.get("speaking", False),
            "stt_started": self.stt_started,
            "greeting_sent": self.greeting_sent,
        }


class SessionManager:
    """Thread-safe in-memory session store."""

    def __init__(self):
        self._sessions: Dict[str, Session] = {}
        self._lock = asyncio.Lock()

    async def create_session(self, websocket=None) -> Session:
        """Create a new session with a fresh UUID and return it."""
        session_id = str(uuid.uuid4())
        session = Session(session_id=session_id, websocket=websocket)
        async with self._lock:
            self._sessions[session_id] = session
        print(f"[SessionManager] Created session {session_id}")
        return session

    async def get_session(self, session_id: str) -> Optional[Session]:
        """Return a session by ID, or None if not found."""
        async with self._lock:
            return self._sessions.get(session_id)

    async def delete_session(self, session_id: str) -> bool:
        """Remove a session. Returns True if it existed."""
        async with self._lock:
            if session_id in self._sessions:
                del self._sessions[session_id]
                print(f"[SessionManager] Deleted session {session_id}")
                return True
        return False

    async def list_sessions(self) -> List[Dict[str, Any]]:
        """Return a list of session dicts for all active sessions."""
        async with self._lock:
            return [s.to_dict() for s in self._sessions.values()]

    async def count(self) -> int:
        """Return number of active sessions."""
        async with self._lock:
            return len(self._sessions)


# Global singleton
session_manager = SessionManager()
