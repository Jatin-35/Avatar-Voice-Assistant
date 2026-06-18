"""
voice/tts.py — Azure Neural TTS via REST API (no SDK).

Uses Azure TTS REST endpoint directly via aiohttp — no azure-cognitiveservices-speech
SDK required. Avoids SPXERR_INVALID_ARG crashes on Python 3.12/3.13.

Region: centralindia (set AZURE_SPEECH_REGION=centralindia in .env)
Format: audio-24khz-160kbitrate-mono-mp3 — 24 kHz is the NATIVE render rate of
        Azure neural voices, so this is full-quality (16 kHz was downsampled).
        Higher (48 kHz) only upsamples the same source, so it adds no real detail.
"""

import base64
import time
import xml.sax.saxutils as saxutils
from collections import deque
from typing import Optional

import aiohttp

from config import AZURE_SPEECH_KEY, AZURE_SPEECH_REGION, TTS_VOICE_NAME, TTS_VOICE_LANGUAGE


def _tts_url() -> str:
    return f"https://{AZURE_SPEECH_REGION}.tts.speech.microsoft.com/cognitiveservices/v1"

def _headers() -> dict:
    return {
        "Ocp-Apim-Subscription-Key": AZURE_SPEECH_KEY,
        "Content-Type":              "application/ssml+xml",
        "X-Microsoft-OutputFormat":  "audio-24khz-160kbitrate-mono-mp3",
        "User-Agent":                "WebVoiceAgent/1.0",
    }

def _build_ssml(text: str, language: str) -> str:
    safe = saxutils.escape(text)
    return (
        f'<speak version="1.0" xml:lang="{language}">'
        f'<voice name="{TTS_VOICE_NAME}">'
        f'<prosody rate="1.05" pitch="+2%">{safe}</prosody>'
        f'</voice></speak>'
    )


# ── LRU + TTL cache ───────────────────────────────────────────────────────────

class ResponseCache:
    def __init__(self, max_size: int = 200, ttl_seconds: float = 3600.0):
        self.cache:        dict  = {}
        self.access_order: deque = deque()
        self.timestamps:   dict  = {}
        self.max_size  = max_size
        self.ttl       = ttl_seconds

    def get(self, key: str) -> Optional[str]:
        if key not in self.cache:
            return None
        if time.time() - self.timestamps.get(key, 0) > self.ttl:
            try:    self.access_order.remove(key)
            except ValueError: pass
            del self.cache[key], self.timestamps[key]
            return None
        try:    self.access_order.remove(key)
        except ValueError: pass
        self.access_order.append(key)
        return self.cache[key]

    def set(self, key: str, value: str) -> None:
        if key in self.cache:
            try:    self.access_order.remove(key)
            except ValueError: pass
        elif len(self.cache) >= self.max_size:
            lru = self.access_order.popleft()
            self.cache.pop(lru, None)
            self.timestamps.pop(lru, None)
        self.cache[key] = value
        self.timestamps[key] = time.time()
        self.access_order.append(key)

response_cache = ResponseCache(max_size=200, ttl_seconds=3600)


# ── Shared aiohttp session ─────────────────────────────────────────────────────

_tts_session: Optional[aiohttp.ClientSession] = None

async def _get_session() -> aiohttp.ClientSession:
    global _tts_session
    if _tts_session is None or _tts_session.closed:
        timeout = aiohttp.ClientTimeout(total=15, connect=5, sock_read=10)
        _tts_session = aiohttp.ClientSession(
            timeout=timeout,
            connector=aiohttp.TCPConnector(limit=10, keepalive_timeout=30),
        )
    return _tts_session

async def close_tts_session() -> None:
    global _tts_session
    if _tts_session and not _tts_session.closed:
        await _tts_session.close()
        _tts_session = None


# ── Main synthesis ─────────────────────────────────────────────────────────────

async def synthesize_speech_azure(text: str, language: str = "hi-IN") -> Optional[str]:
    """Synthesize via Azure TTS REST API. Returns base64 MP3 or None."""
    # Normalise language tag
    if language in ("HINDI", "HINGLISH", "hi"):
        language = TTS_VOICE_LANGUAGE
    elif language in ("ENGLISH", "en"):
        language = "en-IN"

    cache_key = f"{language}:{text}"
    cached = response_cache.get(cache_key)
    if cached:
        print(f"[TTS] Cache HIT: {text[:40]!r}")
        return cached

    print(f"[TTS] Synthesizing: {text[:60]!r}")
    ssml = _build_ssml(text, language)

    try:
        session = await _get_session()
        async with session.post(_tts_url(), headers=_headers(), data=ssml.encode("utf-8")) as resp:
            if resp.status == 200:
                audio_bytes = await resp.read()
                audio_b64   = base64.b64encode(audio_bytes).decode("utf-8")
                response_cache.set(cache_key, audio_b64)
                print(f"[TTS] OK — {len(audio_bytes):,} bytes MP3")
                return audio_b64

            body = await resp.text()
            if resp.status == 401:
                print(f"[TTS] 401 — key wrong/expired. Azure Portal → Speech → Keys and Endpoint")
            elif resp.status == 404:
                print(f"[TTS] 404 — wrong region '{AZURE_SPEECH_REGION}'. Azure Portal → Speech → Keys and Endpoint")
            else:
                print(f"[TTS] HTTP {resp.status}: {body[:200]}")
            return None

    except Exception as e:
        print(f"[TTS] Error: {e}")
        return None


# ── Startup log ───────────────────────────────────────────────────────────────

print(f"[TTS] Azure REST API — region={AZURE_SPEECH_REGION}, voice={TTS_VOICE_NAME}")
print(f"[TTS]   Endpoint: {_tts_url()}")
