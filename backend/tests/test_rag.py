"""
tests/test_rag.py — Unit tests for the RAG service.

Run with: pytest backend/tests/test_rag.py -v
"""

import sys
from pathlib import Path
import pytest

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def test_import_rag_service():
    """rag_service singleton should be importable without crashing."""
    from rag.service import rag_service, _is_trivial_for_rag
    assert rag_service is not None
    assert callable(_is_trivial_for_rag)


def test_trivial_utterances_skipped():
    """Short filler utterances should be detected as trivial."""
    from rag.service import _is_trivial_for_rag

    trivial_cases = [
        "हाँ",
        "okay",
        "ji",
        "hello",
        "bye",
        "नमस्ते",
        "thanks",
        "haan",
    ]
    for text in trivial_cases:
        assert _is_trivial_for_rag(text), f"Expected trivial: {text!r}"


def test_non_trivial_utterances_not_skipped():
    """Substantive queries should NOT be detected as trivial."""
    from rag.service import _is_trivial_for_rag

    non_trivial = [
        "Vestige distributor kaise bante hain?",
        "Performance Bonus kya hota hai?",
        "मुझे DLCP के बारे में जानना है",
        "What is the minimum purchase for PV?",
    ]
    for text in non_trivial:
        assert not _is_trivial_for_rag(text), f"Expected non-trivial: {text!r}"


def test_tagged_trivial_text():
    """Trivial check should work even with [DETECTED_LANGUAGE: hi] tag prepended."""
    from rag.service import _is_trivial_for_rag

    tagged = "[DETECTED_LANGUAGE: hi] जी"
    assert _is_trivial_for_rag(tagged)


@pytest.mark.asyncio
async def test_retrieve_context_returns_string():
    """retrieve_context should return a string (empty if RAG not loaded)."""
    from rag.service import rag_service

    result = await rag_service.retrieve_context("Tell me about Vestige")
    assert isinstance(result, str)


@pytest.mark.asyncio
async def test_retrieve_context_cache_hit():
    """Calling retrieve_context twice with same query should hit cache."""
    from rag.service import rag_service

    query = "What is Performance Bonus?"
    result1 = await rag_service.retrieve_context(query)
    result2 = await rag_service.retrieve_context(query)
    # Both should be identical strings (from cache on second call)
    assert result1 == result2


@pytest.mark.asyncio
async def test_trivial_query_skips_rag():
    """For trivial utterances, the pipeline should skip RAG (tested via _is_trivial_for_rag)."""
    from rag.service import _is_trivial_for_rag

    assert _is_trivial_for_rag("okay") is True
    assert _is_trivial_for_rag("Vestige ka Performance Bonus kya hai?") is False
