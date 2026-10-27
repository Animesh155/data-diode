"""Token-bucket rate limiter for async transports.

Provides a classic `token bucket <https://en.wikipedia.org/wiki/Token_bucket>`_
implementation backed by :mod:`asyncio`, suitable for throttling outbound UDP
packet rates across a data-diode link.
"""

from __future__ import annotations

import asyncio
import time

import structlog

logger = structlog.get_logger(__name__)


class TokenBucketRateLimiter:
    """Async token-bucket rate limiter.

    Tokens are replenished continuously at *rate* tokens per second up to a
    maximum of *burst*.  Callers either check-and-consume via :meth:`acquire`
    or block until tokens are available via :meth:`wait`.

    Args:
        rate: Sustained token replenishment rate (tokens/second).
        burst: Maximum bucket capacity.

    Example::

        limiter = TokenBucketRateLimiter(rate=100.0, burst=200)
        if await limiter.acquire(tokens=5):
            send_packets(...)
    """

    def __init__(self, rate: float, burst: int) -> None:
        if rate <= 0:
            raise ValueError(f"rate must be positive, got {rate}")
        if burst <= 0:
            raise ValueError(f"burst must be positive, got {burst}")

        self._rate: float = rate
        self._burst: int = burst
        self._tokens: float = float(burst)
        self._last_refill: float = time.monotonic()
        self._lock = asyncio.Lock()

        logger.info("rate_limiter.init", rate=rate, burst=burst)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _refill(self) -> None:
        """Add tokens accrued since the last refill."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
        self._last_refill = now

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def tokens_available(self) -> float:
        """Approximate number of tokens currently available (non-locking)."""
        elapsed = time.monotonic() - self._last_refill
        return min(self._burst, self._tokens + elapsed * self._rate)

    async def acquire(self, tokens: int = 1) -> bool:
        """Try to consume *tokens* without blocking.

        Args:
            tokens: Number of tokens to consume.

        Returns:
            ``True`` if tokens were consumed, ``False`` otherwise.
        """
        async with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    async def wait(self, tokens: int = 1) -> None:
        """Block until *tokens* are available, then consume them.

        Args:
            tokens: Number of tokens to consume.
        """
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                # Calculate how long until enough tokens accrue.
                deficit = tokens - self._tokens
                wait_s = deficit / self._rate

            await asyncio.sleep(wait_s)

    def reset(self) -> None:
        """Refill the bucket to its maximum capacity immediately."""
        self._tokens = float(self._burst)
        self._last_refill = time.monotonic()
        logger.debug("rate_limiter.reset", burst=self._burst)
