"""
voice/pipeline.py — Full voice processing pipeline for web voice agent.

Ported from streaming_cube.py with web-specific adaptations:
  - AudioQueueManager sends JSON audio_chunk messages (not raw PCM to Cube)
  - TTS output is MP3 (not Raw8Khz PCM)
  - Duration estimation from word count (not PCM byte math)
  - LLM chunks streamed to frontend via llm_chunk messages
  - State transitions broadcast via state messages
"""

import re
import json
import asyncio
import base64
import time
from collections import deque
from typing import Optional, Callable, Dict, Any

from voice.tts import synthesize_speech_azure
from llm.client import http_manager
from config import (
    AZURE_OPENAI_API_KEY,
    AZURE_OPENAI_ENDPOINT,
    OPENAI_API_VERSION,
    AZURE_OPENAI_DEPLOYMENT,
)

# ── Interruption / VAD config (ported from streaming_cube.py) ────────────────

class VADConfig:
    ENABLED = False
    ENERGY_THRESHOLD = 2000
    MIN_CHUNKS_ABOVE_THRESHOLD = 5
    SMOOTHING_WINDOW = 5
    NOISE_GATE = 600
    VAD_COOLDOWN = 0.7
    SINGLE_TRIGGER_PER_TTS = True


class InterruptionConfig:
    ENABLED = False
    MIN_SPEECH_LENGTH = 3
    INTERRUPTION_COOLDOWN = 0.3
    CANCEL_DELAY = 0.0
    ACKNOWLEDGE_INTERRUPTION = False


class InterruptionState:
    """Tracks interruption state for a conversation session.
    Exact copy from streaming_cube.py (minus Cube-specific fields).
    """

    def __init__(self):
        self.last_interruption_time: float = 0
        self.current_tts_task: Optional[asyncio.Task] = None
        self.audio_stream_active: bool = False
        self.interrupted_response: Optional[str] = None
        self.interruption_count: int = 0
        self.current_audio_manager = None  # reference to AudioQueueManager

        # VAD state
        self.energy_buffer = deque(maxlen=VADConfig.SMOOTHING_WINDOW)
        self.chunks_above_threshold: int = 0
        self.last_vad_trigger_time: float = 0
        self.pending_recognition: bool = False
        self.vad_triggered_this_tts: bool = False

    def can_interrupt(self) -> bool:
        return (time.time() - self.last_interruption_time) >= InterruptionConfig.INTERRUPTION_COOLDOWN

    def can_vad_trigger(self) -> bool:
        if (time.time() - self.last_vad_trigger_time) < VADConfig.VAD_COOLDOWN:
            return False
        if VADConfig.SINGLE_TRIGGER_PER_TTS and self.vad_triggered_this_tts:
            return False
        return True

    def record_interruption(self, interrupted_text: str):
        self.last_interruption_time = time.time()
        self.interrupted_response = interrupted_text
        self.interruption_count += 1

    def record_vad_trigger(self):
        self.last_vad_trigger_time = time.time()
        self.pending_recognition = True
        self.chunks_above_threshold = 0
        self.vad_triggered_this_tts = True

    def reset_for_new_tts(self):
        self.vad_triggered_this_tts = False
        self.chunks_above_threshold = 0
        self.energy_buffer.clear()

    def reset(self):
        self.current_tts_task = None
        self.audio_stream_active = False
        self.interrupted_response = None
        self.energy_buffer.clear()
        self.chunks_above_threshold = 0
        self.pending_recognition = False
        self.vad_triggered_this_tts = False


# ── Sentence / text helpers ───────────────────────────────────────────────────

_SENTENCE_ENDINGS = {'.', '!', '?', '।'}
_CLAUSE_BREAKS = {',', ';', ':', '—'}
_MIN_CLAUSE_LEN = 25


def _find_safe_break(buf: str) -> int:
    """Find the rightmost SAFE split position in buf.

    Returns the index AFTER which to split (buf[:idx] is the chunk to yield),
    or -1 if there is no safe break yet (caller should wait for more tokens).

    Safety rules:
      '.'  unsafe if next char is alphanumeric (inside URL/decimal, e.g. ".com" or "5.0").
           If next char hasn't arrived yet (period is last char of buf), defer.
      ','  unsafe if both neighbours are digits (inside ₹6,300 etc).
           If next char hasn't arrived yet, defer.
    Other break chars (!?।;:—) are always safe.
    """
    n = len(buf)
    # Hard breaks first
    for i in range(n - 1, -1, -1):
        ch = buf[i]
        if ch in _SENTENCE_ENDINGS:
            if ch == '.':
                nxt = buf[i + 1] if i + 1 < n else ''
                if nxt == '':
                    return -1   # period at end of buffer — defer to next token
                if nxt.isalnum():
                    continue    # mid-URL or mid-decimal
            return i + 1
    # Soft breaks (only if buffer is long enough)
    if n < _MIN_CLAUSE_LEN:
        return -1
    for i in range(n - 1, -1, -1):
        ch = buf[i]
        if ch in _CLAUSE_BREAKS:
            if ch == ',':
                prev = buf[i - 1] if i > 0 else ' '
                nxt = buf[i + 1] if i + 1 < n else ''
                if nxt == '':
                    return -1   # comma at end — defer
                if prev.isdigit() and nxt.isdigit():
                    continue    # mid-number
            return i + 1
    return -1


def _clean_for_tts(text: str) -> str:
    """Strip markdown + leaked control tags so TTS doesn't read them aloud."""
    text = re.sub(r'\[DETECTED_LANGUAGE:\s*\w+\s*\]\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\*{1,3}', '', text)
    text = re.sub(r'#{1,6}\s*', '', text)
    text = re.sub(r'^[\-•]\s*', '', text, flags=re.MULTILINE)
    return text.strip()


def _is_trivial(text: str) -> bool:
    """Return True if text is too trivial to send to TTS on its own."""
    alpha_only = re.sub(r'[^\wऀ-ॿ]', '', text, flags=re.UNICODE)
    return len(alpha_only) < 6


def _estimate_duration(text: str) -> float:
    """Estimate TTS playback duration from word count.

    Uses 150 words/min = 2.5 words/sec as average speech rate.
    Returns seconds (minimum 0.5s).
    """
    word_count = len(text.split())
    return max(0.5, word_count / 2.5)


# ── TTS future helper ─────────────────────────────────────────────────────────

async def generate_tts_for_future(
    text: str,
    future: asyncio.Future,
    language: str = "hi-IN",
) -> None:
    """Background task: synthesize text → resolve future with base64 MP3."""
    try:
        audio_b64 = await synthesize_speech_azure(text, language)
        if not future.done():
            if audio_b64:
                future.set_result(audio_b64)
                print(f"[Pipeline] TTS future resolved: {text[:40]!r}")
            else:
                print(f"[Pipeline] TTS failed: {text[:40]!r}")
                future.set_result(None)
    except Exception as e:
        print(f"[Pipeline] TTS future error: {e}")
        if not future.done():
            future.set_result(None)


# ── Audio Queue Manager ───────────────────────────────────────────────────────

class AudioQueueManager:
    """Ordered sentence-by-sentence TTS playback for web.

    Sends audio as JSON:
      {"type": "audio_chunk", "data": "<base64 mp3>", "sentence_idx": N, "is_last": bool}

    Rolling pre-roll: fires on_first_audio callback 1s before the LAST sentence ends.
    Re-closes STT gate at the start of each sentence via on_stt_close.
    """

    TTS_FUTURE_TIMEOUT = 60  # seconds

    def __init__(
        self,
        websocket,
        bot_speaking_flag: Dict[str, bool],
        interruption_state: Optional[InterruptionState] = None,
        on_first_audio: Optional[Callable] = None,
        on_stt_close: Optional[Callable] = None,
    ):
        self.websocket = websocket
        self.bot_speaking_flag = bot_speaking_flag
        self.interruption_state = interruption_state
        self.on_first_audio = on_first_audio    # open_stt_gate: 1s before last sentence ends
        self.on_stt_close = on_stt_close        # close_stt_gate: start of each sentence

        self.audio_queue: asyncio.Queue = asyncio.Queue()
        self.is_playing = False
        self.is_closed = False
        self.playback_task: Optional[asyncio.Task] = None
        self.sentences_queued = 0
        self.sentences_played = 0
        self.audio_started = False          # True once real audio reached the browser
        self.audio_started_at = 0.0         # timestamp of first audio chunk sent
        self._pre_roll_task: Optional[asyncio.Task] = None

    async def add_future(self, future_audio: asyncio.Future, text_for_timing: str = "") -> bool:
        """Reserve a slot in the queue.

        Returns False if manager is closed (interrupted).
        """
        if self.is_closed:
            print("[AudioQueue] Manager closed — cannot add future")
            try:
                future_audio.cancel()
            except Exception:
                pass
            return False

        self.sentences_queued += 1
        # Store both the future and the text (for duration estimation)
        await self.audio_queue.put((future_audio, text_for_timing))
        print(f"[AudioQueue] Queued sentence #{self.sentences_queued}")

        if not self.is_playing:
            self.is_playing = True
            print("[AudioQueue] Starting playback task")
            self.playback_task = asyncio.create_task(self._play_queue())
        return True

    async def mark_completed(self) -> None:
        """Signal that no more sentences are coming."""
        if not self.is_closed:
            await self.audio_queue.put(None)
            print(f"[AudioQueue] Stream completed — {self.sentences_queued} sentences queued")

    async def _play_queue(self) -> None:
        self.is_playing = True
        try:
            while True:
                if self.is_closed:
                    print(f"[AudioQueue] Playback stopped — manager closed ({self.sentences_played}/{self.sentences_queued})")
                    break

                item = await self.audio_queue.get()
                if item is None:
                    self.audio_queue.task_done()
                    print(f"[AudioQueue] End sentinel — played {self.sentences_played}/{self.sentences_queued}")
                    break

                future, text_for_timing = item
                self.sentences_played += 1
                idx = self.sentences_played

                try:
                    audio_b64 = await asyncio.wait_for(
                        future, timeout=self.TTS_FUTURE_TIMEOUT
                    )
                except (asyncio.TimeoutError, asyncio.CancelledError) as e:
                    print(f"[AudioQueue] TTS timeout/cancelled for #{idx}: {e}")
                    audio_b64 = None
                except Exception as e:
                    print(f"[AudioQueue] TTS error for #{idx}: {e}")
                    audio_b64 = None

                self.audio_queue.task_done()

                if self.is_closed:
                    print(f"[AudioQueue] Interrupted before playing #{idx}")
                    break

                if audio_b64:
                    # Re-close STT gate at start of each sentence (keep bot speech out of STT)
                    if self.on_stt_close:
                        asyncio.create_task(self.on_stt_close())

                    # Send audio chunk to browser
                    try:
                        await self.websocket.send_json({
                            "type": "audio_chunk",
                            "data": audio_b64,
                            "sentence_idx": idx,
                        })
                        print(f"[AudioQueue] Sent sentence #{idx} ({len(audio_b64)} b64 chars)")
                        if not self.audio_started:
                            self.audio_started = True
                            self.audio_started_at = time.time()
                    except Exception as e:
                        print(f"[AudioQueue] Send error for #{idx}: {e}")
                        break

                    # Estimate duration from text for wait timing
                    duration_sec = _estimate_duration(text_for_timing) if text_for_timing else 2.0
                    total_wait = duration_sec + 0.2

                    print(f"[AudioQueue] Waiting {total_wait:.2f}s for sentence #{idx} playback")
                    await asyncio.sleep(total_wait)
                    print(f"[AudioQueue] Sentence #{idx} playback complete")
                else:
                    print(f"[AudioQueue] Skipping null audio for #{idx}")

        except asyncio.CancelledError:
            print(f"[AudioQueue] Playback task cancelled ({self.sentences_played}/{self.sentences_queued})")
        except Exception as e:
            print(f"[AudioQueue] Playback error: {e}")
        finally:
            self.is_playing = False
            if not self.is_closed:
                # Natural end — clear speaking flag, notify frontend, open STT gate
                self.bot_speaking_flag["speaking"] = False
                if self.interruption_state:
                    self.interruption_state.audio_stream_active = False
                print("[AudioQueue] Natural end — bot_speaking cleared")
                # Tell the frontend all audio is done (avatar → still, mic reopens)
                try:
                    await self.websocket.send_json({"type": "audio_complete"})
                    print("[AudioQueue] audio_complete sent to frontend")
                except Exception as e:
                    print(f"[AudioQueue] Could not send audio_complete: {e}")
                # Open STT gate only after the last sentence finishes
                if self.on_first_audio:
                    try:
                        asyncio.create_task(self.on_first_audio())
                        print("[AudioQueue] STT gate opened (natural end)")
                    except Exception as e:
                        print(f"[AudioQueue] Gate open error: {e}")

    async def clear(self) -> None:
        """Stop playback immediately (interruption)."""
        print(f"[AudioQueue] CLEAR called ({self.sentences_played}/{self.sentences_queued})")
        self.is_playing = False
        self.is_closed = True

        # Cancel pre-roll
        if self._pre_roll_task and not self._pre_roll_task.done():
            self._pre_roll_task.cancel()

        # Drain and cancel futures
        cancelled = 0
        while not self.audio_queue.empty():
            try:
                item = self.audio_queue.get_nowait()
                if item is not None:
                    future, _ = item
                    if asyncio.isfuture(future) and not future.done():
                        future.cancel()
                        cancelled += 1
                self.audio_queue.task_done()
            except Exception:
                break
        print(f"[AudioQueue] Cancelled {cancelled} pending futures")

        if self.playback_task and not self.playback_task.done():
            self.playback_task.cancel()
            try:
                await self.playback_task
            except asyncio.CancelledError:
                pass

        # Notify browser to clear its audio queue
        try:
            await self.websocket.send_json({"type": "audio_clear"})
        except Exception as e:
            print(f"[AudioQueue] Could not send audio_clear: {e}")


# ── LLM streaming ─────────────────────────────────────────────────────────────

async def get_azure_openai_stream(
    messages: list,
    websocket,
    session_id: str,
):
    """Async generator: stream LLM response, yield complete sentences.

    Also streams {"type": "llm_chunk", "text": chunk} to frontend as tokens arrive.
    Detects [HANGUP] tag (sends state disconnect message).
    """
    headers = {
        "Content-Type": "application/json",
        "api-key": AZURE_OPENAI_API_KEY,
    }
    data = {
        "messages": messages,
        "max_tokens": 200,
        "temperature": 0.6,
        "stream": True,
    }
    url = (
        f"{AZURE_OPENAI_ENDPOINT}openai/deployments/{AZURE_OPENAI_DEPLOYMENT}"
        f"/chat/completions?api-version={OPENAI_API_VERSION}"
    )

    import aiohttp

    buffer = ""
    hangup_detected = False
    timeout = aiohttp.ClientTimeout(total=90, connect=30, sock_read=60)
    session = await http_manager.get_session()

    try:
        async with session.post(url, headers=headers, json=data, timeout=timeout) as response:
            if response.status != 200:
                body = await response.text()
                print(f"[LLM] Stream error: {response.status} {body[:200]}")
                if buffer.strip():
                    yield buffer
                return

            while True:
                line = await response.content.readline()
                if not line:
                    break
                line_text = line.decode("utf-8").strip()

                if line_text.startswith("data: ") and line_text != "data: [DONE]":
                    try:
                        chunk_json = json.loads(line_text[6:])
                        if chunk_json.get("choices"):
                            delta = chunk_json["choices"][0].get("delta", {})
                            content = delta.get("content", "") or ""

                            if content:
                                buffer += content

                                # Stream raw token to frontend
                                try:
                                    await websocket.send_json({
                                        "type": "llm_chunk",
                                        "text": content,
                                    })
                                except Exception:
                                    pass  # Don't abort LLM stream on WS send error

                                # Check for [HANGUP] tag
                                if "[HANGUP]" in buffer:
                                    hangup_detected = True
                                    buffer = buffer.replace("[HANGUP]", "").strip()

                                split_idx = len(buffer) if hangup_detected else _find_safe_break(buffer)

                                if split_idx > 0:
                                    chunk = buffer[:split_idx]
                                    if chunk.strip():
                                        yield chunk
                                    buffer = buffer[split_idx:]

                                    if hangup_detected:
                                        print(f"[LLM] [HANGUP] detected — sending disconnect state")
                                        try:
                                            await websocket.send_json({
                                                "type": "state",
                                                "value": "disconnecting",
                                            })
                                        except Exception:
                                            pass
                                        return

                    except (json.JSONDecodeError, KeyError):
                        pass

        # Yield any remaining buffer
        if buffer.strip():
            if "[HANGUP]" in buffer:
                hangup_detected = True
                buffer = buffer.replace("[HANGUP]", "").strip()
            if buffer:
                yield buffer
            if hangup_detected:
                try:
                    await websocket.send_json({"type": "state", "value": "disconnecting"})
                except Exception:
                    pass

    except Exception as e:
        print(f"[LLM] Stream exception: {e}")
        if buffer.strip():
            yield buffer


# ── Main pipeline function ────────────────────────────────────────────────────

async def process_user_speech_parallel(
    user_text: str,
    conversation_history: list,
    websocket,
    session_id: str,
    bot_speaking_flag: Dict[str, bool],
    interruption_state: Optional[InterruptionState] = None,
    on_first_audio: Optional[Callable] = None,
    on_stt_close: Optional[Callable] = None,
) -> None:
    """Main pipeline: RAG → LLM stream → sentence-by-sentence TTS → audio queue.

    Mirrors process_user_speech_parallel from streaming_cube.py but adapted for web:
      - No Cube binary protocol
      - MP3 audio chunks sent as JSON
      - on_first_audio/on_stt_close same gate logic
    """
    # Import here to avoid circular imports
    from rag.service import rag_service, _is_trivial_for_rag as _trivial_rag
    from core.conversation import trim_history, build_messages_with_rag
    from llm.prompts import get_system_prompt

    start_time = time.time()
    print(f"[Pipeline] Processing: {user_text[:80]!r}")

    # ── RAG in parallel with setup ────────────────────────────────────────────
    if _trivial_rag(user_text):
        print("[Pipeline] RAG skipped (trivial utterance)")
        rag_task = None
    else:
        # Tell the UI we're consulting the knowledge base
        try:
            await websocket.send_json({"type": "state", "value": "searching_kb"})
        except Exception:
            pass
        rag_task = asyncio.create_task(rag_service.retrieve_context_with_sources(user_text))

    # ── Append user turn, THEN trim ───────────────────────────────────────────
    # Append BEFORE trimming so the current utterance is part of the trimmed
    # history sent to the LLM. (Trimming first dropped it whenever there was no
    # RAG context — which is always, while RAG is off.)
    conversation_history.append({"role": "user", "content": user_text})
    trimmed_history = trim_history(conversation_history, max_turns=10)

    # ── Setup audio manager ───────────────────────────────────────────────────
    audio_manager = AudioQueueManager(
        websocket=websocket,
        bot_speaking_flag=bot_speaking_flag,
        interruption_state=interruption_state,
        on_first_audio=on_first_audio,
        on_stt_close=on_stt_close,
    )

    # Set speaking flag (with small grace period for VAD to settle)
    async def _set_speaking_after_delay():
        await asyncio.sleep(0.3)
        if interruption_state and interruption_state.audio_stream_active:
            bot_speaking_flag["speaking"] = True
            print("[Pipeline] Bot speaking flag SET (0.3s grace)")

    asyncio.create_task(_set_speaking_after_delay())

    if interruption_state:
        if interruption_state.audio_stream_active:
            print("[Pipeline] Previous stream active — cancelling old audio manager")
            old_am = getattr(interruption_state, "current_audio_manager", None)
            if old_am:
                await old_am.clear()
        interruption_state.current_audio_manager = audio_manager
        interruption_state.reset_for_new_tts()
        interruption_state.audio_stream_active = True
        interruption_state.interrupted_response = ""

    # ── Await RAG ─────────────────────────────────────────────────────────────
    if rag_task is not None:
        rag_context, rag_sources = await rag_task
    else:
        rag_context, rag_sources = "", []
    if rag_context:
        print(f"[Pipeline] RAG context injected ({len(rag_context)} chars)")
    if rag_sources:
        try:
            await websocket.send_json({"type": "agent_sources", "sources": rag_sources})
        except Exception:
            pass

    # ── Build messages list ───────────────────────────────────────────────────
    messages_for_llm = build_messages_with_rag(
        history=trimmed_history,
        user_text=user_text,
        rag_context=rag_context,
        system_prompt=None,  # System prompt is already first message in history
    )

    # ── LLM stream → TTS pipeline ─────────────────────────────────────────────
    full_response = ""
    loop = asyncio.get_running_loop()
    sentence_count = 0
    pending_prefix = ""

    # Broadcast processing state
    try:
        await websocket.send_json({"type": "state", "value": "processing"})
    except Exception:
        pass

    try:
        async for sentence in get_azure_openai_stream(messages_for_llm, websocket, session_id):
            if interruption_state and not interruption_state.audio_stream_active:
                print(f"[Pipeline] LLM stream interrupted after {sentence_count} sentences")
                break

            full_response += sentence
            sentence = (sentence or "").strip()
            if not sentence:
                continue

            if _is_trivial(sentence):
                pending_prefix += sentence + " "
                print(f"[Pipeline] Trivial buffered: {sentence!r}")
                continue

            if pending_prefix:
                sentence = (pending_prefix + sentence).strip()
                pending_prefix = ""

            tts_text = _clean_for_tts(sentence)
            if not tts_text:
                continue

            sentence_count += 1
            print(f"[Pipeline] Sentence #{sentence_count}: {tts_text[:60]!r}")

            future = loop.create_future()
            added = await audio_manager.add_future(future, text_for_timing=tts_text)
            if added:
                asyncio.create_task(generate_tts_for_future(tts_text, future, "hi-IN"))
            else:
                print(f"[Pipeline] Audio manager closed — stopping at sentence #{sentence_count}")
                break

        print(f"[Pipeline] LLM stream complete — {sentence_count} sentences")
        await audio_manager.mark_completed()

        # Append assistant response to history
        conversation_history.append({"role": "assistant", "content": full_response})

        if interruption_state and interruption_state.audio_stream_active:
            interruption_state.interrupted_response = full_response

    except asyncio.CancelledError:
        print("[Pipeline] Processing cancelled (interruption)")
        raise
    except Exception as e:
        print(f"[Pipeline] Error: {e}")
        if bot_speaking_flag:
            bot_speaking_flag["speaking"] = False
        if interruption_state:
            interruption_state.audio_stream_active = False
    finally:
        elapsed = time.time() - start_time
        print(f"[Pipeline] Done: {elapsed:.2f}s ({sentence_count} sentences)")
