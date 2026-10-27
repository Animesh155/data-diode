"""Asynchronous polling engine for OSDD Protocol Gateway.

Manages concurrent poll loops for each registered driver, applies the filter
chain, updates the tag manager, and forwards values via the UDP sender.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import structlog

from gateway.drivers.base import BaseDriver, DriverStatus

logger: structlog.stdlib.BoundLogger = structlog.get_logger(component="polling_engine")


class PollingEngine:
    """Coordinate periodic tag polling across multiple protocol drivers.

    For each registered driver an independent :class:`asyncio.Task` is spawned
    that reads tags at the requested interval, pipes them through the filter
    chain, records them in the tag manager, and emits them over the UDP
    transport.

    Parameters
    ----------
    tag_manager:
        Central tag-value store.
    udp_sender:
        UDP transport used to push tag batches to the diode.
    filter_chain:
        Ordered chain of tag-value filters.
    """

    _JITTER_FACTOR: float = 0.10  # 0-10 % random start jitter

    def __init__(
        self,
        tag_manager: Any,
        udp_sender: Any,
        filter_chain: Any,
    ) -> None:
        self._tag_manager = tag_manager
        self._udp_sender = udp_sender
        self._filter_chain = filter_chain

        self._drivers: list[tuple[BaseDriver, float]] = []
        self._tasks: list[asyncio.Task[None]] = []
        self._running: bool = False

        self._total_polls: int = 0
        self._total_tags_read: int = 0
        self._total_tags_sent: int = 0
        self._total_poll_errors: int = 0
        self._started_at: float | None = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def add_driver(
        self,
        driver: BaseDriver,
        poll_interval_seconds: float,
    ) -> None:
        """Register a driver for periodic polling.

        Parameters
        ----------
        driver:
            An initialised (but not necessarily connected) protocol driver.
        poll_interval_seconds:
            Time in seconds between successive polls for this driver.
        """
        self._drivers.append((driver, poll_interval_seconds))
        logger.info(
            "polling_engine.driver_added",
            driver=driver.name,
            interval=poll_interval_seconds,
        )

    async def start(self) -> None:
        """Start all registered poll loops with random jitter.

        Each loop is started as an independent :class:`asyncio.Task`.
        A small random jitter (0-10 % of the poll interval) is applied to
        avoid thundering-herd effects when multiple drivers share a similar
        interval.
        """
        if self._running:
            logger.warning("polling_engine.already_running")
            return

        self._running = True
        self._started_at = time.monotonic()

        for driver, interval in self._drivers:
            jitter = random.uniform(0, self._JITTER_FACTOR * interval)
            task = asyncio.create_task(
                self._poll_loop(driver, interval, jitter),
                name=f"poll-{driver.name}",
            )
            self._tasks.append(task)
            logger.info(
                "polling_engine.task_started",
                driver=driver.name,
                interval=interval,
                jitter=round(jitter, 4),
            )

        logger.info(
            "polling_engine.started",
            driver_count=len(self._drivers),
        )

    async def stop(self) -> None:
        """Cancel all polling tasks and wait for them to finish."""
        if not self._running:
            return

        self._running = False
        logger.info("polling_engine.stopping", task_count=len(self._tasks))

        for task in self._tasks:
            task.cancel()

        results = await asyncio.gather(*self._tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception) and not isinstance(
                result, asyncio.CancelledError
            ):
                logger.error("polling_engine.task_error", error=str(result))

        self._tasks.clear()
        logger.info("polling_engine.stopped")

    def stats(self) -> dict[str, Any]:
        """Return engine-wide statistics.

        Returns
        -------
        dict[str, Any]
            Counts of polls, tags read/sent, errors, uptime, and per-driver
            information.
        """
        uptime: float | None = None
        if self._started_at is not None:
            uptime = time.monotonic() - self._started_at

        return {
            "running": self._running,
            "driver_count": len(self._drivers),
            "total_polls": self._total_polls,
            "total_tags_read": self._total_tags_read,
            "total_tags_sent": self._total_tags_sent,
            "total_poll_errors": self._total_poll_errors,
            "uptime_seconds": round(uptime, 2) if uptime is not None else None,
            "drivers": [
                {
                    "name": drv.name,
                    "interval": interval,
                    "stats": drv.stats(),
                }
                for drv, interval in self._drivers
            ],
        }

    # ------------------------------------------------------------------
    # Internal poll loop
    # ------------------------------------------------------------------

    async def _poll_loop(
        self,
        driver: BaseDriver,
        interval: float,
        initial_jitter: float = 0.0,
    ) -> None:
        """Continuously poll a single driver at *interval* seconds.

        Parameters
        ----------
        driver:
            The protocol driver to read from.
        interval:
            Seconds between successive polls.
        initial_jitter:
            One-time sleep applied before the first poll to stagger drivers.
        """
        if initial_jitter > 0:
            await asyncio.sleep(initial_jitter)

        log = structlog.get_logger(
            component="poll_loop",
            driver=driver.name,
        )

        # Ensure the driver is connected before entering the loop.
        if driver.status != DriverStatus.CONNECTED:
            try:
                await driver.connect_with_retry()
            except Exception as exc:
                log.error("poll_loop.initial_connect_failed", error=str(exc))
                return

        while self._running:
            loop_start = time.monotonic()

            try:
                # 1. Read raw tags
                raw_tags = await driver.safe_read_tags()
                self._total_polls += 1
                self._total_tags_read += len(raw_tags)

                # 2. Apply filter chain
                filtered_tags = self._filter_chain.apply(raw_tags)

                # 3. Update tag manager
                for tag in filtered_tags:
                    self._tag_manager.update(tag)

                # 4. Send over UDP transport
                if filtered_tags:
                    await self._udp_sender.send(filtered_tags)
                    self._total_tags_sent += len(filtered_tags)

                log.debug(
                    "poll_loop.cycle_complete",
                    raw=len(raw_tags),
                    filtered=len(filtered_tags),
                    elapsed=round(time.monotonic() - loop_start, 4),
                )

            except asyncio.CancelledError:
                log.info("poll_loop.cancelled")
                raise
            except Exception as exc:
                self._total_poll_errors += 1
                log.error("poll_loop.error", error=str(exc))

            # Sleep for the remainder of the interval.
            elapsed = time.monotonic() - loop_start
            sleep_time = max(0.0, interval - elapsed)
            await asyncio.sleep(sleep_time)
