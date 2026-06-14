"""
src/utterance_buffer.py
Debounce + deduplicate STT utterances before sending to the LLM pipeline.

An utterance is held for `debounce_ms` after the last add_utterance() call.
If a new utterance arrives while the callback is already running it cancels
the in-flight task and starts fresh.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional


@dataclass
class UtteranceBufferConfig:
    debounce_ms:   int  = 400   # wait this long after last utterance before firing
    debug_logging: bool = False


class UtteranceBuffer:
    """
    Thin async debounce buffer for speech recognition results.

    Usage:
        buf = UtteranceBuffer(UtteranceBufferConfig())
        await buf.add_utterance("hello world", my_async_callback)
    """

    def __init__(self, config: Optional[UtteranceBufferConfig] = None):
        self._cfg          = config or UtteranceBufferConfig()
        self._pending_text = ""
        self._debounce:    Optional[asyncio.TimerHandle]  = None
        self._task:        Optional[asyncio.Task]         = None
        self.is_processing = False

    # ── Public API ────────────────────────────────────────────────────────────

    async def add_utterance(
        self,
        text: str,
        callback: Callable[[str], Awaitable[None]],
    ) -> None:
        """Buffer `text` and fire `callback` after the debounce window."""
        self._pending_text = text
        self._log(f"add_utterance: {text!r}")

        # Reset debounce timer
        if self._debounce:
            self._debounce.cancel()

        loop = asyncio.get_running_loop()
        delay = self._cfg.debounce_ms / 1000.0

        self._debounce = loop.call_later(
            delay,
            lambda: asyncio.ensure_future(self._fire(callback)),
        )

    def cancel_on_new_speech_detected(self) -> bool:
        """
        Cancel the currently running callback task (user spoke mid-response).
        Returns True if a task was actually cancelled.
        """
        if self._task and not self._task.done():
            self._task.cancel()
            self._task = None
            self.is_processing = False
            self._log("Task cancelled (new speech)")
            return True
        return False

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _fire(self, callback: Callable[[str], Awaitable[None]]) -> None:
        text = self._pending_text.strip()
        if not text:
            return

        self.is_processing = True
        self._log(f"Firing callback: {text!r}")
        try:
            self._task = asyncio.create_task(callback(text))
            await self._task
        except asyncio.CancelledError:
            self._log("Callback cancelled")
        except Exception as e:
            print(f"[UtteranceBuffer] Callback error: {e}")
        finally:
            self.is_processing = False
            self._task = None

    def _log(self, msg: str) -> None:
        if self._cfg.debug_logging:
            print(f"[UtteranceBuffer] {msg}")
