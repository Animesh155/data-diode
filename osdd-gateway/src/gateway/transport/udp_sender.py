"""Batched, sequenced UDP sender for data-diode transport.

Queues incoming :class:`TagValue` updates and flushes them in batches at a
configurable interval, serialised into MTU-bounded packets via
:mod:`src.gateway.transport.serializer`.  An optional
:class:`TokenBucketRateLimiter` gates the outbound packet rate.
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass, field
from typing import Optional

import structlog

from src.gateway.core.tag_manager import TagValue
from src.gateway.transport.rate_limiter import TokenBucketRateLimiter
from src.gateway.transport.serializer import serialize_tags

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class _SenderStats:
    """Internal mutable counters."""

    packets_sent: int = 0
    bytes_sent: int = 0
    tags_sent: int = 0
    errors: int = 0


class UdpSender:
    """Async batched UDP sender.

    Tags are placed in an internal queue via :meth:`send` and flushed every
    *batch_interval_ms* milliseconds by a background task started with
    :meth:`start`.

    Args:
        target_host: Destination IP address or hostname.
        target_port: Destination UDP port.
        source_gateway: Identifier embedded in every outbound packet.
        max_packet_size: Maximum datagram payload size in bytes.
        batch_interval_ms: How often (ms) the background task flushes the queue.
        rate_limit: Optional :class:`TokenBucketRateLimiter` for throttling.

    Example::

        sender = UdpSender("10.0.0.2", 5000, source_gateway="gw-east-1")
        await sender.start()
        await sender.send(tag)
        ...
        await sender.stop()
    """

    def __init__(
        self,
        target_host: str,
        target_port: int,
        source_gateway: str,
        max_packet_size: int = 1400,
        batch_interval_ms: int = 100,
        rate_limit: Optional[TokenBucketRateLimiter] = None,
    ) -> None:
        self._host = target_host
        self._port = target_port
        self._source = source_gateway
        self._max_packet_size = max_packet_size
        self._batch_interval_s = batch_interval_ms / 1000.0
        self._rate_limiter = rate_limit

        self._queue: asyncio.Queue[TagValue] = asyncio.Queue()
        self._sequence: int = 0
        self._sock: Optional[socket.socket] = None
        self._task: Optional[asyncio.Task[None]] = None
        self._running = False
        self._stats = _SenderStats()

        logger.info(
            "udp_sender.init",
            target=f"{target_host}:{target_port}",
            source=source_gateway,
            batch_ms=batch_interval_ms,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Open the UDP socket and launch the background batch-sender loop."""
        if self._running:
            logger.warning("udp_sender.already_running")
            return

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setblocking(False)
        self._running = True
        self._task = asyncio.create_task(self._batch_loop(), name="udp_sender_batch_loop")
        logger.info("udp_sender.started")

    async def stop(self) -> None:
        """Flush remaining tags, cancel the background task, and close the socket."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        # Drain anything left in the queue.
        await self._flush()

        if self._sock is not None:
            self._sock.close()
            self._sock = None
        logger.info("udp_sender.stopped", stats=self.stats)

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    async def send(self, tag: TagValue) -> None:
        """Enqueue a single tag for the next batch flush.

        Args:
            tag: Tag snapshot to send.
        """
        await self._queue.put(tag)

    async def send_batch(self, tags: list[TagValue]) -> None:
        """Serialise and send *tags* immediately (bypasses the queue).

        Args:
            tags: Tag snapshots to send now.
        """
        if not tags:
            return
        await self._transmit(tags)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def stats(self) -> dict[str, int]:
        """Return transmission statistics."""
        return {
            "packets_sent": self._stats.packets_sent,
            "bytes_sent": self._stats.bytes_sent,
            "tags_sent": self._stats.tags_sent,
            "errors": self._stats.errors,
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _batch_loop(self) -> None:
        """Background loop: drain the queue and transmit periodically."""
        logger.debug("udp_sender.batch_loop.start")
        try:
            while self._running:
                await asyncio.sleep(self._batch_interval_s)
                await self._flush()
        except asyncio.CancelledError:
            pass

    async def _flush(self) -> None:
        """Drain all queued tags and transmit them."""
        batch: list[TagValue] = []
        while not self._queue.empty():
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if batch:
            await self._transmit(batch)

    async def _transmit(self, tags: list[TagValue]) -> None:
        """Serialise and send over the UDP socket."""
        if self._sock is None:
            logger.error("udp_sender.no_socket")
            self._stats.errors += 1
            return

        seq = self._sequence
        packets = serialize_tags(tags, seq, self._source)
        self._sequence += len(packets)

        loop = asyncio.get_running_loop()

        for pkt in packets:
            # Rate-limit if configured.
            if self._rate_limiter is not None:
                await self._rate_limiter.wait()

            try:
                await loop.sock_sendto(self._sock, pkt, (self._host, self._port))
                self._stats.packets_sent += 1
                self._stats.bytes_sent += len(pkt)
            except OSError as exc:
                self._stats.errors += 1
                logger.error(
                    "udp_sender.send_error",
                    error=str(exc),
                    target=f"{self._host}:{self._port}",
                )

        self._stats.tags_sent += len(tags)
        logger.debug(
            "udp_sender.transmitted",
            tags=len(tags),
            packets=len(packets),
            seq_start=seq,
        )
