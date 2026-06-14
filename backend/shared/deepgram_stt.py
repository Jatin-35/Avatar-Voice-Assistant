"""
src/deepgram_stt.py
Streams raw PCM16 audio to Deepgram's live-transcription WebSocket API
and fires on_recognized / on_recognizing callbacks.

Uses the `websockets` library (already in requirements.txt) — no Deepgram
SDK dependency needed.

Subclasses override _PARAMS to change model / audio format:

    class WebDeepgramSTTHandler(DeepgramSTTHandler):
        _PARAMS = { "sample_rate": 16000, ... }
"""

import asyncio
import json
from typing import Any, Callable, List, Optional


# ── Event objects expected by the callback callers ────────────────────────────

class _Result:
    """Mimics event.result used in websocket_handler callbacks."""
    def __init__(self, text: str, language: str, is_final: bool):
        self.text     = text
        self.language = language   # "HINDI" | "ENGLISH" | "HINGLISH"
        self.isFinal  = is_final


class _Event:
    def __init__(self, text: str, language: str, is_final: bool):
        self.result = _Result(text, language, is_final)


# ── Language code mapper ──────────────────────────────────────────────────────

def _map_lang(code: str) -> str:
    """
    Map Deepgram detected_language codes to the backend's label convention.
    Deepgram returns BCP-47 codes like "hi", "en-US", "hi-IN", etc.
    """
    if not code:
        return "HINDI"
    lc = code.lower()
    if lc.startswith("hi"):
        return "HINDI"
    if lc.startswith("en"):
        return "ENGLISH"
    return "HINGLISH"


# ── Main handler class ────────────────────────────────────────────────────────

class DeepgramSTTHandler:
    """
    Live-streaming STT via Deepgram WebSocket.

    Interface:
        handler = DeepgramSTTHandler(api_key, on_recognized, on_recognizing, keywords)
        await handler.start_async()      # opens WebSocket, starts receive loop
        handler.write_audio(pcm_bytes)   # feed raw PCM16 chunks (sync, thread-safe)
        await handler.stop_async()       # graceful close
        handler.is_running               # bool

    The callbacks receive a single _Event argument with a .result attribute:
        event.result.text      -> str
        event.result.language  -> "HINDI" | "ENGLISH" | "HINGLISH"
        event.result.isFinal   -> bool
    """

    # Base Deepgram params — subclasses override _PARAMS to change sample_rate etc.
    _PARAMS: dict = {
        "model":            "nova-3",
        "language":         "multi",
        "encoding":         "linear16",
        "sample_rate":      8000,
        "channels":         1,
        "interim_results":  "true",
        "smart_format":     "true",
        "punctuate":        "true",
        "endpointing":      "500",
        "utterance_end_ms": "1000",
        "vad_events":       "true",
    }

    _DEEPGRAM_URL = "wss://api.deepgram.com/v1/listen"

    # ── Constructor ───────────────────────────────────────────────────────────

    def __init__(
        self,
        api_key:        str,
        on_recognized:  Optional[Callable] = None,
        on_recognizing: Optional[Callable] = None,
        keywords:       Optional[List[str]] = None,
        on_speech_started: Optional[Callable] = None,
    ):
        self._api_key          = api_key
        self._on_recognized    = on_recognized
        self._on_recognizing   = on_recognizing
        self._on_speech_started = on_speech_started
        self._keywords         = keywords or []

        self._ws:        Any                               = None
        self._loop:      Optional[asyncio.AbstractEventLoop] = None
        self._recv_task: Optional[asyncio.Task]            = None
        self.is_running  = False
        # Accumulate is_final segments; flush as one turn on speech_final / UtteranceEnd
        self._utterance_parts: List[str] = []
        self._last_language: str = "HINDI"

    # ── URL builder ───────────────────────────────────────────────────────────

    def _build_url(self) -> str:
        """Build the Deepgram WebSocket URL with query params + keyterms."""
        parts = []
        for key, val in self._PARAMS.items():
            parts.append(f"{key}={val}")

        # Keyterms boost accuracy for domain-specific words (cap at 100)
        for kw in self._keywords[:100]:
            # URL-encode spaces and special chars
            encoded = kw.replace(" ", "%20")
            parts.append(f"keyterms={encoded}")

        return f"{self._DEEPGRAM_URL}?{'&'.join(parts)}"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start_async(self) -> None:
        """Open the Deepgram WebSocket and start the background receive loop."""
        if self.is_running:
            return

        import websockets  # imported here so the module loads without websockets if needed

        self._loop = asyncio.get_running_loop()
        url        = self._build_url()
        headers    = {"Authorization": f"Token {self._api_key}"}

        print(f"[STT] Connecting to Deepgram  model={self._PARAMS.get('model')}  "
              f"sr={self._PARAMS.get('sample_rate')} Hz  lang={self._PARAMS.get('language')}")
        try:
            self._ws    = await websockets.connect(url, additional_headers=headers)
            self.is_running = True
            self._recv_task = asyncio.create_task(self._receive_loop())
            print("[STT] Deepgram connected ✓")
        except Exception as e:
            print(f"[STT] Connection failed: {e}")
            self.is_running = False
            raise

    async def stop_async(self) -> None:
        """Send a graceful close signal to Deepgram and tear down."""
        if not self.is_running:
            return
        self.is_running = False
        print("[STT] Stopping Deepgram STT …")

        try:
            if self._ws is not None:
                # Deepgram expects an empty binary frame to signal end-of-stream
                try:
                    await self._ws.send(b"")
                    await asyncio.sleep(0.15)
                except Exception:
                    pass
                try:
                    await self._ws.close()
                except Exception:
                    pass
        except Exception as e:
            print(f"[STT] Stop error: {e}")

        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass

        self._recv_task = None
        self._ws        = None
        print("[STT] Deepgram disconnected")

    # ── Audio ingestion ───────────────────────────────────────────────────────

    def write_audio(self, pcm_bytes: bytes) -> None:
        """
        Feed a raw PCM16 chunk to Deepgram.
        This is a synchronous method safe to call from any thread.
        """
        if not self.is_running or self._ws is None or self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._send_audio(pcm_bytes), self._loop)

    async def _send_audio(self, pcm_bytes: bytes) -> None:
        try:
            if self._ws is not None:
                await self._ws.send(pcm_bytes)
        except Exception as e:
            if self.is_running:          # silence errors during intended shutdown
                print(f"[STT] Audio send error: {e}")

    # ── Receive loop ──────────────────────────────────────────────────────────

    async def _receive_loop(self) -> None:
        """Background task: read JSON frames from Deepgram and dispatch them."""
        try:
            async for raw_msg in self._ws:
                if not self.is_running:
                    break
                if isinstance(raw_msg, bytes):
                    continue                      # Deepgram doesn't send binary
                await self._dispatch(raw_msg)
        except Exception as e:
            # websockets raises ConnectionClosed, CancelledError, etc.
            if self.is_running:
                print(f"[STT] Receive loop ended: {type(e).__name__}: {e}")
        finally:
            self.is_running = False

    # ── Message dispatcher ────────────────────────────────────────────────────

    async def _dispatch(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = msg.get("type", "")

        if msg_type == "Results":
            self._handle_results(msg)
        elif msg_type == "Metadata":
            req_id = msg.get("request_id", "")[:12]
            print(f"[STT] Metadata  request_id={req_id}")
        elif msg_type == "SpeechStarted":
            # VAD: user started speaking — drives barge-in
            if self._on_speech_started:
                try:
                    self._on_speech_started()
                except Exception as e:
                    print(f"[STT] on_speech_started error: {e}")
        elif msg_type == "UtteranceEnd":
            # Backup turn-end: flush accumulated segments if speech_final didn't fire
            self._flush_utterance()
        elif msg_type == "Error":
            print(f"[STT] Deepgram error: {msg.get('message', raw[:200])}")
        elif msg_type == "Warning":
            print(f"[STT] Deepgram warning: {msg.get('message', '')}")

    # ── Results handler ───────────────────────────────────────────────────────

    def _handle_results(self, msg: dict) -> None:
        channel      = msg.get("channel", {})
        alternatives = channel.get("alternatives", [])
        if not alternatives:
            return

        transcript = alternatives[0].get("transcript", "").strip()

        is_final     = msg.get("is_final", False)
        speech_final = msg.get("speech_final", False)

        # Deepgram multi-language puts detected_language at the top level
        lang_code = (
            msg.get("detected_language")
            or channel.get("detected_language")
            or ""
        )
        language = _map_lang(lang_code)
        self._last_language = language

        # Deepgram emits is_final per FINALIZED SEGMENT (mid-utterance); only
        # speech_final marks the true end of a turn. Accumulate is_final segments
        # and fire on_recognized ONCE, on speech_final — otherwise a turn fires
        # mid-sentence and the remainder of the user's speech gets cut off.
        if speech_final:
            parts = self._utterance_parts + ([transcript] if transcript else [])
            self._utterance_parts = []
            full = " ".join(p for p in parts if p).strip()
            if full and self._on_recognized:
                self._on_recognized(_Event(full, language, True))
        elif is_final:
            if transcript:
                self._utterance_parts.append(transcript)
            if self._on_recognizing and self._utterance_parts:
                interim = " ".join(self._utterance_parts).strip()
                self._on_recognizing(_Event(interim, language, False))
        elif transcript and self._on_recognizing:
            interim = " ".join(self._utterance_parts + [transcript]).strip()
            self._on_recognizing(_Event(interim, language, False))

    def _flush_utterance(self) -> None:
        """Fire any accumulated is_final segments as one turn (UtteranceEnd backup)."""
        parts = self._utterance_parts
        self._utterance_parts = []
        if parts and self._on_recognized:
            full = " ".join(p for p in parts if p).strip()
            if full:
                self._on_recognized(_Event(full, self._last_language, True))
