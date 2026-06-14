"""
api/websocket_handler.py — WebSocket endpoint /ws/voice

Handles browser mic audio (16kHz PCM) → Deepgram STT → LLM → Azure TTS → MP3 to browser.

Protocol:
  Browser → Server:
    binary: raw PCM16 at 16kHz
    json: {"type": "ping"} | {"type": "interrupt"}

  Server → Browser:
    {"type": "session_ready", "session_id": "..."}
    {"type": "partial_transcript", "text": "..."}
    {"type": "final_transcript", "text": "...", "language": "HINDI|ENGLISH|HINGLISH"}
    {"type": "llm_chunk", "text": "...", "sentence_idx": N}
    {"type": "audio_chunk", "data": "<base64 mp3>", "sentence_idx": N}
    {"type": "audio_complete"}                    # all sentences finished — stop avatar, open mic
    {"type": "audio_clear"}
    {"type": "interrupted"}                       # barge-in confirmation
    {"type": "agent_sources", "sources": [...]}   # RAG chunks used
    {"type": "state", "value": "listening|searching_kb|processing|speaking|disconnecting"}
    {"type": "error", "message": "..."}
    {"type": "pong"}
"""

import asyncio
import json
import time
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect

from core.session import session_manager, Session
from voice.stt import create_stt_handler
from voice.pipeline import (
    InterruptionState,
    process_user_speech_parallel,
)
from voice.tts import synthesize_speech_azure
from llm.prompts import get_system_prompt

from shared.utterance_buffer import UtteranceBuffer, UtteranceBufferConfig

# If STT yields partials but never a final for this long (seconds), force-process
# the stalled partial. Safety net behind Deepgram's endpointing (~1s).
SILENCE_FALLBACK_SEC = 3.0

# Fixed opening greeting — pre-synthesized at startup so the first one is instant.
GREETING_TEXT = "नमस्ते! मैं साक्षी हूँ, Sonalika Tractor से। बताइए, मैं आपकी कैसे मदद कर सकती हूँ?"


async def handle_voice_websocket(websocket: WebSocket) -> None:
    """Main WebSocket handler for /ws/voice."""

    # ── Accept + create session ───────────────────────────────────────────────
    await websocket.accept()
    session: Session = await session_manager.create_session(websocket=websocket)
    session_id = session.session_id
    print(f"[WS] New session: {session_id}")

    # ── Initialise session state ──────────────────────────────────────────────
    system_prompt = get_system_prompt()
    session.conversation_history = [{"role": "system", "content": system_prompt}]
    session.bot_speaking_state = {"speaking": False}
    interruption_state = InterruptionState()
    session.interruption_state = interruption_state

    # Announce session to browser
    await websocket.send_json({"type": "session_ready", "session_id": session_id})

    # ── STT gate variables (closure) ─────────────────────────────────────────
    stt_discard_until = {"value": 0.0}       # timestamp gate
    detected_language = {"value": "HINDI"}   # last STT language
    last_partial = {"text": "", "time": 0.0}  # newest interim; drives silence watchdog
    current_loop: Optional[asyncio.AbstractEventLoop] = None
    watchdog_task: Optional[asyncio.Task] = None

    async def open_stt_gate():
        stt_discard_until["value"] = 0.0
        print("[STT] Gate OPEN — accepting transcripts")
        try:
            await websocket.send_json({"type": "state", "value": "listening"})
        except Exception:
            pass

    async def close_stt_gate():
        stt_discard_until["value"] = float("inf")
        print("[STT] Gate CLOSED — bot speaking/processing")
        try:
            await websocket.send_json({"type": "state", "value": "speaking"})
        except Exception:
            pass

    # ── UtteranceBuffer ───────────────────────────────────────────────────────
    utterance_buffer = UtteranceBuffer(UtteranceBufferConfig(debug_logging=True))
    session.utterance_buffer = utterance_buffer

    # ── Barge-in (user interrupts the bot mid-response) ───────────────────────
    async def _barge_in(reason: str = "vad") -> None:
        """Stop the bot immediately so the interrupting speech becomes the next turn."""
        am = getattr(interruption_state, "current_audio_manager", None)
        if not (am and not am.is_closed):
            return
        print(f"[WS] Barge-in ({reason}) — stopping bot, reopening mic")
        # Hard-cancel the in-flight pipeline task (if any)
        if utterance_buffer.is_processing:
            utterance_buffer.cancel_on_new_speech_detected()
        try:
            await am.clear()  # stops playback + sends audio_clear to browser
        except Exception:
            pass
        session.bot_speaking_state["speaking"] = False
        interruption_state.audio_stream_active = False
        stt_discard_until["value"] = 0.0  # reopen gate; keep accumulating user speech
        try:
            await websocket.send_json({"type": "interrupted"})
            await websocket.send_json({"type": "state", "value": "listening"})
        except Exception:
            pass

    # NOTE: Barge-in is now driven by the browser's client-side VAD (Silero),
    # which sends {"type": "interrupt"} → _barge_in("client"). We deliberately do
    # NOT use Deepgram's server-side SpeechStarted VAD for barge-in anymore.

    # ── STT callbacks ─────────────────────────────────────────────────────────

    async def process_buffered_speech(text: str) -> None:
        """Run the full pipeline for a recognised utterance."""
        # Close gate: bot is about to start processing / speaking
        stt_discard_until["value"] = float("inf")
        print(f"[WS] process_buffered_speech: {text[:80]!r}")

        # Tag language for LLM
        lang_code = "en" if detected_language["value"] == "ENGLISH" else "hi"
        tagged_text = f"[DETECTED_LANGUAGE: {lang_code}] {text}"

        try:
            await process_user_speech_parallel(
                user_text=tagged_text,
                conversation_history=session.conversation_history,
                websocket=websocket,
                session_id=session_id,
                bot_speaking_flag=session.bot_speaking_state,
                interruption_state=interruption_state,
                on_first_audio=open_stt_gate,
                on_stt_close=close_stt_gate,
            )
        except asyncio.CancelledError:
            print("[WS] Pipeline cancelled — user spoke again")
        except Exception as e:
            print(f"[WS] Pipeline error: {e}")
            try:
                await websocket.send_json({"type": "error", "message": str(e)})
            except Exception:
                pass
        finally:
            # Failsafe: if pipeline exits without opening gate, open it now
            _am = getattr(interruption_state, "current_audio_manager", None)
            _pipeline_owns_gate = (
                _am is not None
                and not getattr(_am, "is_closed", True)
                and getattr(_am, "sentences_queued", 0) > 0
            )
            if stt_discard_until["value"] == float("inf") and not _pipeline_owns_gate:
                stt_discard_until["value"] = 0.0
                print("[WS] Gate FORCED OPEN (failsafe)")
                try:
                    await websocket.send_json({"type": "state", "value": "listening"})
                except Exception:
                    pass

    def on_recognized_handler(event):
        """Handle final STT transcript (sync callback, posts to async loop)."""
        text = event.result.text.strip()
        lang = getattr(event.result, "language", "HINDI") or "HINDI"
        detected_language["value"] = lang

        if not text:
            if interruption_state and interruption_state.pending_recognition:
                interruption_state.pending_recognition = False
            return

        # Discard gate check
        if time.time() < stt_discard_until["value"]:
            print(f"[STT] Pre-roll discard: {text[:60]!r}")
            return

        # A final arrived — clear the watchdog's pending partial
        last_partial["text"] = ""

        print(f"[STT] Recognized [{lang}]: {text!r}")

        if current_loop is None:
            return

        # Send final_transcript to browser
        asyncio.run_coroutine_threadsafe(
            websocket.send_json({
                "type": "final_transcript",
                "text": text,
                "language": lang,
            }),
            current_loop,
        )

        # VAD interruption path
        if interruption_state and interruption_state.pending_recognition:
            interruption_state.pending_recognition = False
            asyncio.run_coroutine_threadsafe(
                utterance_buffer.add_utterance(text, process_buffered_speech),
                current_loop,
            )
            return

        # Normal path
        asyncio.run_coroutine_threadsafe(
            utterance_buffer.add_utterance(text, process_buffered_speech),
            current_loop,
        )

    def on_recognizing_handler(event):
        """Handle partial STT transcript (sync callback)."""
        if not (hasattr(event, "result") and hasattr(event.result, "text")):
            return
        partial = event.result.text.strip()
        if partial and len(partial) > 3:
            print(f"\r[STT] Partial: {partial[:80]}", end="", flush=True)
            # Track newest partial for the silence watchdog
            last_partial["text"] = partial
            last_partial["time"] = time.time()
            # Stream partial to browser
            if current_loop:
                asyncio.run_coroutine_threadsafe(
                    websocket.send_json({"type": "partial_transcript", "text": partial}),
                    current_loop,
                )
            # Cancel LLM if still processing and user is still speaking
            if utterance_buffer and utterance_buffer.is_processing:
                cancelled = utterance_buffer.cancel_on_new_speech_detected()
                if cancelled:
                    print(f"\n[STT] LLM cancelled — user still speaking")

    # ── Silence watchdog ──────────────────────────────────────────────────────
    async def silence_watchdog():
        """Safety net: force-process a partial if STT never emits its final."""
        last_fired = {"text": "", "time": 0.0}
        try:
            while True:
                await asyncio.sleep(0.5)
                txt = last_partial["text"]
                if not txt:
                    continue
                # Only when the mic is open, the bot is idle, and we aren't already busy
                if stt_discard_until["value"] != 0.0:
                    continue
                if utterance_buffer.is_processing:
                    continue
                if session.bot_speaking_state.get("speaking", False):
                    continue
                if time.time() - last_partial["time"] < SILENCE_FALLBACK_SEC:
                    continue
                # Don't re-fire the same stalled text repeatedly
                if txt == last_fired["text"] and time.time() - last_fired["time"] < 8.0:
                    continue
                print(f"[WS] Silence watchdog — forcing stalled partial: {txt[:60]!r}")
                last_fired["text"] = txt
                last_fired["time"] = time.time()
                last_partial["text"] = ""
                await utterance_buffer.add_utterance(txt, process_buffered_speech)
        except asyncio.CancelledError:
            pass

    # ── STT handler ───────────────────────────────────────────────────────────
    speech_recognizer = create_stt_handler(
        session_id=session_id,
        on_recognized=on_recognized_handler,
        on_recognizing=on_recognizing_handler,
    )
    session.stt_handler = speech_recognizer

    # ── Greeting ──────────────────────────────────────────────────────────────
    async def send_greeting():
        """Synthesize and send a greeting MP3 to the browser."""
        greeting_text = GREETING_TEXT
        print(f"[WS] Sending greeting: {greeting_text[:60]!r}")

        # Close STT gate during greeting
        stt_discard_until["value"] = float("inf")

        audio_b64 = await synthesize_speech_azure(greeting_text, "hi-IN")
        if audio_b64:
            session.conversation_history.append(
                {"role": "assistant", "content": greeting_text}
            )
            try:
                await websocket.send_json({
                    "type": "audio_chunk",
                    "data": audio_b64,
                    "sentence_idx": 0,
                    "is_last": True,
                })
                await websocket.send_json({"type": "state", "value": "speaking"})
            except Exception as e:
                print(f"[WS] Greeting send error: {e}")

            # Wait for greeting playback then signal completion and open gate
            # Estimate: ~10 words / 2.5 wps = 4s + 0.5s buffer
            await asyncio.sleep(4.5)
            try:
                await websocket.send_json({"type": "audio_complete"})
            except Exception:
                pass
            stt_discard_until["value"] = 0.0
            try:
                await websocket.send_json({"type": "state", "value": "listening"})
            except Exception:
                pass
        else:
            stt_discard_until["value"] = 0.0

    # ── Main receive loop ─────────────────────────────────────────────────────
    try:
        current_loop = asyncio.get_running_loop()
        audio_chunk_count = 0

        while True:
            message = await websocket.receive()

            if "bytes" in message and message["bytes"] is not None:
                # Binary: raw PCM16 at 16kHz from browser AudioWorklet
                pcm_audio: bytes = message["bytes"]

                if len(pcm_audio) < 100:
                    continue

                # First audio chunk → start STT, greeting, and watchdog
                if not session.stt_started:
                    session.stt_started = True
                    print(f"[WS] First audio received — starting STT session")
                    # Fire the greeting first so its TTS overlaps the STT connect
                    asyncio.create_task(send_greeting())
                    await speech_recognizer.start_async()
                    print("[WS] Deepgram STT started (16kHz)")
                    # Start silence watchdog
                    watchdog_task = asyncio.create_task(silence_watchdog())

                # Feed audio to Deepgram
                if speech_recognizer.is_running:
                    speech_recognizer.write_audio(pcm_audio)

                audio_chunk_count += 1
                if audio_chunk_count % 200 == 0:
                    print(f"[WS] Audio chunks received: {audio_chunk_count}")

            elif "text" in message and message["text"] is not None:
                # Text: JSON control messages
                try:
                    msg = json.loads(message["text"])
                    msg_type = msg.get("type")

                    if msg_type == "ping":
                        await websocket.send_json({"type": "pong"})

                    elif msg_type == "interrupt":
                        # Browser-side interruption signal
                        print("[WS] Client-side interrupt signal received")
                        await _barge_in("client")

                    else:
                        print(f"[WS] Unknown control message: {msg_type}")

                except json.JSONDecodeError:
                    print(f"[WS] Invalid JSON: {message['text'][:100]!r}")

    except WebSocketDisconnect:
        print(f"[WS] Client disconnected: {session_id}")
    except Exception as e:
        print(f"[WS] Error: {e}")
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        print(f"[WS] Cleaning up session {session_id}")
        # Stop watchdog
        if watchdog_task and not watchdog_task.done():
            watchdog_task.cancel()
        # Stop STT
        if speech_recognizer and speech_recognizer.is_running:
            try:
                await speech_recognizer.stop_async()
            except Exception as e:
                print(f"[WS] STT stop error: {e}")
        # Delete session
        await session_manager.delete_session(session_id)
        print(f"[WS] Session {session_id} cleaned up")
