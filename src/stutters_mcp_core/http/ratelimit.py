"""Client-side throttling: a requests-per-minute token bucket plus a concurrency cap.

FMC allows ~120 requests/minute and 10 concurrent connections per source IP; Catalyst Center has
per-API limits. Running into those limits costs retries, so the client paces itself instead.
One limiter can be shared by several ApiClients that talk to the same appliance (e.g. ISE surfaces).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class RateLimiter:
    def __init__(self, per_minute: float | None = None, max_concurrency: int | None = None) -> None:
        if per_minute is not None and per_minute <= 0:
            raise ValueError("per_minute must be positive")
        self.per_minute = per_minute
        self.max_concurrency = max_concurrency
        self._capacity = max(1.0, per_minute / 6) if per_minute else 0.0  # allow ~10s bursts
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(max_concurrency) if max_concurrency else None

    async def _take(self) -> None:
        if not self.per_minute:
            return
        rate = self.per_minute / 60.0
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self._capacity, self._tokens + (now - self._updated) * rate)
                self._updated = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                await asyncio.sleep((1 - self._tokens) / rate)

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Hold a concurrency slot (if capped) after waiting for a rate token (if limited)."""
        if self._sem is None:
            await self._take()
            yield
            return
        async with self._sem:
            await self._take()
            yield
