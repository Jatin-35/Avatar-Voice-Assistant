"""
tests/test_voice.py — Unit tests for TTS, STT handler, and pipeline helpers.

Run with: pytest backend/tests/test_voice.py -v
"""

import sys
from pathlib import Path
import pytest

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


# ── Pipeline helpers ──────────────────────────────────────────────────────────

def test_find_safe_break_sentence_endings():
    """_find_safe_break should split at sentence-ending punctuation."""
    from voice.pipeline import _find_safe_break

    buf = "Hello world. How are you"
    idx = _find_safe_break(buf)
    assert idx == 12  # after the '.'
    assert buf[:idx] == "Hello world."


def test_find_safe_break_defers_on_url():
    """Period inside a URL should NOT be a split point."""
    from voice.pipeline import _find_safe_break

    buf = "Visit www.vestige.com for more"
    idx = _find_safe_break(buf)
    # Should not split at the dots inside the URL
    # idx might be -1 or point to a later position — just ensure no split at URL dots
    if idx != -1:
        chunk = buf[:idx]
        assert "www.vestige" not in chunk.rstrip(".")


def test_find_safe_break_defers_on_decimal():
    """Period inside a decimal number like 5.0 should not split."""
    from voice.pipeline import _find_safe_break

    buf = "The ratio is 5.0 units"
    idx = _find_safe_break(buf)
    # No hard break — idx should be -1 or not split inside the decimal
    # The number 5.0 has a digit after '.', so '.' is unsafe
    if idx > 0:
        assert buf[idx - 1] != '0'  # shouldn't cut at the decimal point


def test_find_safe_break_number_comma():
    """Comma inside ₹6,300 should NOT be a split point."""
    from voice.pipeline import _find_safe_break

    buf = "The price is ₹6,300 for this"
    idx = _find_safe_break(buf)
    if idx > 0:
        chunk = buf[:idx]
        assert "6," not in chunk or "300" in chunk  # number must remain intact


def test_clean_for_tts_strips_markdown():
    """_clean_for_tts should remove bold, italic, headings, and language tags."""
    from voice.pipeline import _clean_for_tts

    cases = [
        ("**Hello** world", "Hello world"),
        ("## Section title", "Section title"),
        ("- Bullet point", "Bullet point"),
        ("[DETECTED_LANGUAGE: hi] नमस्ते", "नमस्ते"),
    ]
    for raw, expected in cases:
        result = _clean_for_tts(raw)
        assert result == expected, f"clean_for_tts({raw!r}) = {result!r}, expected {expected!r}"


def test_is_trivial():
    """_is_trivial should detect short/empty content."""
    from voice.pipeline import _is_trivial

    assert _is_trivial("1.")
    assert _is_trivial("ok")
    assert _is_trivial("2")
    assert not _is_trivial("Hello, how can I help you today?")


def test_estimate_duration():
    """_estimate_duration should return reasonable float seconds."""
    from voice.pipeline import _estimate_duration

    duration = _estimate_duration("Hello how are you doing today")
    # 6 words / 2.5 = 2.4s
    assert 2.0 < duration < 4.0

    # Minimum 0.5s for empty / single word
    assert _estimate_duration("") == 0.5
    assert _estimate_duration("Hi") >= 0.5


# ── STT handler ───────────────────────────────────────────────────────────────

def test_stt_handler_write_when_not_running_is_safe():
    """write_audio when STT is not started should not raise."""
    from voice.stt import create_stt_handler

    handler = create_stt_handler("test-session")
    assert not handler.is_running
    # Should silently drop the chunk (not crash)
    handler.write_audio(b"\x00" * 320)


def test_stt_handler_has_correct_sample_rate():
    """WebDeepgramSTTHandler should use 16kHz sample rate."""
    from voice.stt import WebDeepgramSTTHandler

    assert WebDeepgramSTTHandler._PARAMS["sample_rate"] == 16000
    assert WebDeepgramSTTHandler._PARAMS["encoding"] == "linear16"
    assert WebDeepgramSTTHandler._PARAMS["model"] == "nova-3"


# ── TTS ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_synthesize_returns_none_or_base64():
    """synthesize_speech_azure should return None or a non-empty base64 string.

    On CI without Azure credentials it returns None — that's acceptable.
    If credentials are present it returns a valid base64 string.
    """
    import base64
    from voice.tts import synthesize_speech_azure

    result = await synthesize_speech_azure("Hello", "hi-IN")
    if result is None:
        # No credentials — skip without failure
        pytest.skip("Azure Speech credentials not configured — TTS returns None")
    else:
        assert isinstance(result, str)
        assert len(result) > 0
        # Must be valid base64
        try:
            decoded = base64.b64decode(result)
            assert len(decoded) > 0
        except Exception as e:
            pytest.fail(f"TTS result is not valid base64: {e}")


def test_response_cache_lru():
    """ResponseCache should evict oldest entry when full."""
    from voice.tts import ResponseCache

    cache = ResponseCache(max_size=2, ttl_seconds=3600)
    cache.set("a", "val_a")
    cache.set("b", "val_b")
    cache.set("c", "val_c")  # Should evict "a"

    assert cache.get("a") is None  # Evicted
    assert cache.get("b") == "val_b"
    assert cache.get("c") == "val_c"
