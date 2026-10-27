"""Base driver abstraction for OSDD Protocol Gateway.

Provides the abstract base class that all protocol drivers (Modbus, MQTT, OPC-UA)
must implement. Includes connection management with exponential backoff, health
monitoring, and safe tag-reading wrappers.
"""

from __future__ import annotations

import asyncio
import enum
import time
from abc import ABC, abstractmethod
from typing import Any

import structlog

from gateway.core.tag_manager import TagValue


class DriverStatus(enum.Enum):
    """Lifecycle states for a protocol driver."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"
    STOPPING = "stopping"


class BaseDriver(ABC):
    """Abstract base class for all protocol drivers.

    Subclasses must implement :meth:`_connect`, :meth:`_disconnect`,
    :meth:`_read_tags`, and the :attr:`protocol_name` property.

    Features
    --------
    * Exponential-backoff reconnection via :meth:`connect_with_retry`.
    * Safe tag reading with automatic reconnection on failure.
    * Built-in health monitoring based on last successful read time.
    * Structured logging with *structlog*.

    Parameters
    ----------
    config:
        Driver-specific configuration dictionary.  Must contain a ``name`` key.
    """

    # Default health-check multiplier: a driver is considered unhealthy if no
    # successful read has occurred within ``_HEALTH_MULTIPLIER * poll_interval``
    # seconds.
    _HEALTH_MULTIPLIER: int = 3
    _RECONNECT_DELAY_INITIAL: float = 1.0
    _RECONNECT_DELAY_MAX: float = 60.0

    def __init__(self, config: dict[str, Any]) -> None:
        self.name: str = config.get("name", self.__class__.__name__)
        self.config: dict[str, Any] = config
        self.status: DriverStatus = DriverStatus.DISCONNECTED
        self.reconnect_delay: float = self._RECONNECT_DELAY_INITIAL
        self.connection_attempts: int = 0
        self.last_successful_read: float | None = None
        self._poll_interval: float = config.get("poll_interval", 1.0)
        self._total_reads: int = 0
        self._total_errors: int = 0
        self._connected_since: float | None = None

        self.log: structlog.stdlib.BoundLogger = structlog.get_logger(
            driver=self.name,
            protocol=self.protocol_name,
        )

    # ------------------------------------------------------------------
    # Abstract interface — subclasses MUST implement these
    # ------------------------------------------------------------------

    @abstractmethod
    async def _connect(self) -> None:
        """Establish a connection to the underlying data source.

        Raises
        ------
        Exception
            On any connection failure.
        """

    @abstractmethod
    async def _disconnect(self) -> None:
        """Tear down the connection to the underlying data source."""

    @abstractmethod
    async def _read_tags(self) -> list[TagValue]:
        """Read a batch of tags from the data source.

        Returns
        -------
        list[TagValue]
            Zero or more tag values read during this poll cycle.
        """

    @property
    @abstractmethod
    def protocol_name(self) -> str:
        """Return the human-readable protocol name (e.g. ``'modbus'``)."""

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    async def connect_with_retry(self, max_attempts: int = 0) -> None:
        """Attempt to connect with exponential backoff.

        Parameters
        ----------
        max_attempts:
            Maximum number of connection attempts.  ``0`` means infinite
            retries until a connection is established.
        """
        self.reconnect_delay = self._RECONNECT_DELAY_INITIAL
        attempt = 0

        while True:
            attempt += 1
            self.connection_attempts += 1
            self.status = DriverStatus.CONNECTING

            self.log.info(
                "driver.connecting",
                attempt=attempt,
                max_attempts=max_attempts or "infinite",
            )

            try:
                await self._connect()
                self.status = DriverStatus.CONNECTED
                self._connected_since = time.monotonic()
                self.reconnect_delay = self._RECONNECT_DELAY_INITIAL
                self.log.info("driver.connected", attempt=attempt)
                return
            except Exception as exc:
                self.status = DriverStatus.ERROR
                self.log.warning(
                    "driver.connect_failed",
                    attempt=attempt,
                    error=str(exc),
                    retry_in=self.reconnect_delay,
                )

                if max_attempts and attempt >= max_attempts:
                    self.log.error(
                        "driver.connect_exhausted",
                        max_attempts=max_attempts,
                    )
                    raise

                await asyncio.sleep(self.reconnect_delay)
                self.reconnect_delay = min(
                    self.reconnect_delay * 2,
                    self._RECONNECT_DELAY_MAX,
                )

    # ------------------------------------------------------------------
    # Tag reading
    # ------------------------------------------------------------------

    async def safe_read_tags(self) -> list[TagValue]:
        """Read tags with error handling and automatic reconnection.

        If the underlying :meth:`_read_tags` raises, the driver status is set
        to ``ERROR`` and a reconnection attempt is made before re-raising.

        Returns
        -------
        list[TagValue]
            Tag values from the current poll cycle.
        """
        try:
            tags = await self._read_tags()
            self.last_successful_read = time.monotonic()
            self._total_reads += 1
            self.log.debug("driver.read_ok", tag_count=len(tags))
            return tags
        except Exception as exc:
            self._total_errors += 1
            self.status = DriverStatus.ERROR
            self.log.error("driver.read_error", error=str(exc))

            # Attempt to reconnect transparently.
            try:
                await self.connect_with_retry(max_attempts=3)
            except Exception as reconnect_exc:
                self.log.error(
                    "driver.reconnect_failed",
                    error=str(reconnect_exc),
                )
            raise

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def disconnect(self) -> None:
        """Gracefully disconnect the driver."""
        self.status = DriverStatus.STOPPING
        self.log.info("driver.disconnecting")

        try:
            await self._disconnect()
        except Exception as exc:
            self.log.error("driver.disconnect_error", error=str(exc))
        finally:
            self.status = DriverStatus.DISCONNECTED
            self._connected_since = None
            self.log.info("driver.disconnected")

    # ------------------------------------------------------------------
    # Health & statistics
    # ------------------------------------------------------------------

    @property
    def is_healthy(self) -> bool:
        """Return ``True`` if the driver is connected and reading on time.

        A driver is healthy when:
        * Its status is :attr:`DriverStatus.CONNECTED`, **and**
        * The last successful read occurred within
          ``3 × poll_interval`` seconds.
        """
        if self.status != DriverStatus.CONNECTED:
            return False
        if self.last_successful_read is None:
            return False
        elapsed = time.monotonic() - self.last_successful_read
        return elapsed <= self._HEALTH_MULTIPLIER * self._poll_interval

    def stats(self) -> dict[str, Any]:
        """Return a snapshot of driver statistics.

        Returns
        -------
        dict[str, Any]
            Dictionary containing driver name, status, health flag, read
            counts, error counts, connection attempts, and uptime.
        """
        uptime: float | None = None
        if self._connected_since is not None:
            uptime = time.monotonic() - self._connected_since

        return {
            "name": self.name,
            "protocol": self.protocol_name,
            "status": self.status.value,
            "healthy": self.is_healthy,
            "total_reads": self._total_reads,
            "total_errors": self._total_errors,
            "connection_attempts": self.connection_attempts,
            "reconnect_delay": self.reconnect_delay,
            "uptime_seconds": round(uptime, 2) if uptime is not None else None,
            "last_successful_read": self.last_successful_read,
        }
