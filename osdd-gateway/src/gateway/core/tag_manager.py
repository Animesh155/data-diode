"""Tag Manager — Thread-safe, async tag registry with change notification.

Provides a centralized store for TagValue instances sourced from protocol
drivers, with subscriber-based change notifications, staleness detection,
and observability stats.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Coroutine, Optional

import structlog

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class TagValue:
    """Immutable snapshot of a single process tag reading.

    Attributes:
        name: Fully qualified tag identifier (e.g. ``PLC1.Temperature``).
        value: Current reading value — type matches *data_type*.
        data_type: Wire-type descriptor (``float32``, ``int16``, ``bool``, …).
        quality: OPC-UA style quality string (``Good``, ``Bad``, ``Uncertain``).
        timestamp: UTC instant the value was sampled.
        unit: Engineering unit (``°C``, ``bar``, ``ppm``, …).
        source_driver: Name of the driver that produced this value.
        metadata: Arbitrary key/value pairs attached by the driver.
    """

    name: str
    value: Any
    data_type: str
    quality: str = "Good"
    timestamp: Optional[datetime] = None
    unit: Optional[str] = None
    source_driver: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None

    def __post_init__(self) -> None:
        if self.timestamp is None:
            self.timestamp = datetime.now(tz=timezone.utc)


# Type alias for change-notification callbacks.
ChangeCallback = Callable[[TagValue], Coroutine[Any, Any, None]]


class TagManager:
    """Async, thread-safe, in-memory tag store.

    All public mutating methods are serialised behind an :class:`asyncio.Lock`
    so they can safely be called from concurrent coroutines within the same
    event-loop.

    Example::

        mgr = TagManager()
        mgr.on_change(my_handler)
        await mgr.update(TagValue(name="Temp", value=72.5, data_type="float32"))
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._tags: dict[str, TagValue] = {}
        self._callbacks: list[ChangeCallback] = []

        # Observability counters
        self._update_count: int = 0
        self._update_window_start: float = time.monotonic()

        logger.info("tag_manager.init")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def update(self, tag: TagValue) -> None:
        """Insert or update a tag, notifying subscribers when the value changes.

        Args:
            tag: The new tag snapshot to store.
        """
        async with self._lock:
            prev = self._tags.get(tag.name)
            self._tags[tag.name] = tag
            self._update_count += 1

            changed = prev is None or prev.value != tag.value or prev.quality != tag.quality

        if changed:
            logger.debug(
                "tag_manager.tag_changed",
                tag=tag.name,
                value=tag.value,
                quality=tag.quality,
            )
            await self._notify(tag)

    def get(self, name: str) -> Optional[TagValue]:
        """Return the latest snapshot for *name*, or ``None``."""
        return self._tags.get(name)

    def get_all(self) -> dict[str, TagValue]:
        """Return a shallow copy of all stored tags."""
        return dict(self._tags)

    def get_changed_since(self, since: datetime) -> list[TagValue]:
        """Return tags whose *timestamp* is at or after *since*.

        Args:
            since: UTC datetime threshold (inclusive).

        Returns:
            List of :class:`TagValue` instances updated after *since*.
        """
        return [
            t
            for t in self._tags.values()
            if t.timestamp is not None and t.timestamp >= since
        ]

    def on_change(self, callback: ChangeCallback) -> None:
        """Register an async callback fired on every tag value change.

        Args:
            callback: An async callable accepting a single :class:`TagValue`.
        """
        self._callbacks.append(callback)
        logger.debug("tag_manager.callback_registered", total=len(self._callbacks))

    async def mark_stale(
        self,
        driver_name: str,
        threshold: timedelta,
    ) -> int:
        """Mark tags from *driver_name* as ``Uncertain`` when they are stale.

        A tag is *stale* when its ``timestamp`` is older than
        ``utcnow() - threshold``.

        Args:
            driver_name: Filter tags by ``source_driver``.
            threshold: Maximum acceptable age.

        Returns:
            Number of tags that were marked stale.
        """
        cutoff = datetime.now(tz=timezone.utc) - threshold
        stale_count = 0

        async with self._lock:
            for tag in self._tags.values():
                if (
                    tag.source_driver == driver_name
                    and tag.quality == "Good"
                    and tag.timestamp is not None
                    and tag.timestamp < cutoff
                ):
                    # Dataclass is mutable — update in-place.
                    tag.quality = "Uncertain"
                    stale_count += 1
                    logger.warning(
                        "tag_manager.tag_stale",
                        tag=tag.name,
                        driver=driver_name,
                        age_s=(datetime.now(tz=timezone.utc) - tag.timestamp).total_seconds(),
                    )

        if stale_count:
            logger.info(
                "tag_manager.stale_sweep",
                driver=driver_name,
                stale_count=stale_count,
            )
        return stale_count

    def stats(self) -> dict[str, Any]:
        """Return observability statistics.

        Returns:
            Dictionary with ``tag_count``, ``update_rate`` (updates/s since
            construction), and ``stale_count``.
        """
        elapsed = max(time.monotonic() - self._update_window_start, 0.001)
        stale = sum(1 for t in self._tags.values() if t.quality == "Uncertain")
        return {
            "tag_count": len(self._tags),
            "update_rate": round(self._update_count / elapsed, 2),
            "stale_count": stale,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _notify(self, tag: TagValue) -> None:
        """Fire all registered callbacks, swallowing per-callback errors."""
        for cb in self._callbacks:
            try:
                await cb(tag)
            except Exception:
                logger.exception("tag_manager.callback_error", tag=tag.name)
