"""
llm/client.py — HTTP session manager and Azure OpenAI pre-warm.

HTTPSessionManager: singleton aiohttp session pool reused across all LLM calls.
_prewarm_azure_openai(): fires a tiny streaming call at startup so TLS + routing is warm.
"""

import asyncio
import time
import aiohttp

from config import (
    AZURE_OPENAI_API_KEY,
    AZURE_OPENAI_ENDPOINT,
    OPENAI_API_VERSION,
    AZURE_OPENAI_DEPLOYMENT,
)


class HTTPSessionManager:
    """Singleton aiohttp session with connection pooling and keep-alive."""

    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    async def get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                limit=50,
                limit_per_host=10,
                keepalive_timeout=60,
                enable_cleanup_closed=True,
                ttl_dns_cache=600,
                force_close=False,
            )
            timeout = aiohttp.ClientTimeout(total=10, connect=3, sock_read=5)
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None


# Global singleton
http_manager = HTTPSessionManager()


async def _prewarm_azure_openai() -> None:
    """Send a tiny streaming call to Azure OpenAI at startup.

    This pre-establishes the TLS session, connection pool, and Azure routing
    path so the FIRST real user turn is as fast as subsequent turns.
    (Without this, the first call can take 15-20s on a cold container.)
    """
    if not (AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT and OPENAI_API_VERSION):
        print("[LLM] Azure OpenAI pre-warm SKIPPED (missing config)")
        return

    url = (
        f"{AZURE_OPENAI_ENDPOINT}openai/deployments/{AZURE_OPENAI_DEPLOYMENT}"
        f"/chat/completions?api-version={OPENAI_API_VERSION}"
    )
    headers = {
        "Content-Type": "application/json",
        "api-key": AZURE_OPENAI_API_KEY,
    }
    data = {
        "messages": [{"role": "user", "content": "ok"}],
        "max_tokens": 1,
        "temperature": 0.0,
        "stream": True,
    }
    timeout = aiohttp.ClientTimeout(total=15)
    t0 = time.time()
    try:
        session = await http_manager.get_session()
        async with session.post(url, headers=headers, json=data, timeout=timeout) as resp:
            async for _ in resp.content.iter_chunked(1024):
                pass
        elapsed = time.time() - t0
        print(f"[LLM] Azure OpenAI pre-warm complete ({elapsed:.2f}s) — first turn will be fast")
    except Exception as e:
        print(f"[LLM] Azure OpenAI pre-warm failed (non-fatal): {e}")
