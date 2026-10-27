"""Lightweight HTTP health / metrics server for OSDD Protocol Gateway.

Exposes ``/health``, ``/metrics``, and ``/tags`` endpoints via *aiohttp* so
that external monitoring systems (Prometheus, load balancers, etc.) can observe
gateway liveness and performance.
"""

from __future__ import annotations

import json
import time
from typing import Any

import structlog
from aiohttp import web

logger: structlog.stdlib.BoundLogger = structlog.get_logger(component="health_server")


class HealthServer:
    """Asynchronous HTTP server for health checks and metrics.

    Parameters
    ----------
    host:
        Bind address for the HTTP listener.
    port:
        TCP port for the HTTP listener.
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 9090) -> None:
        self._host = host
        self._port = port
        self._app: web.Application = web.Application()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._components: dict[str, Any] = {}
        self._started_at: float = time.monotonic()

        # Register routes
        self._app.router.add_get("/health", self._handle_health)
        self._app.router.add_get("/metrics", self._handle_metrics)
        self._app.router.add_get("/tags", self._handle_tags)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, gateway_components: dict[str, Any]) -> None:
        """Start the HTTP server.

        Parameters
        ----------
        gateway_components:
            Dictionary of gateway subsystems whose stats will be surfaced on
            the health/metrics endpoints.  Expected keys (all optional):

            * ``polling_engine`` — :class:`~gateway.core.polling_engine.PollingEngine`
            * ``tag_manager`` — :class:`~gateway.core.tag_manager.TagManager`
            * ``udp_sender`` — UDP transport sender
            * ``drivers`` — list of :class:`~gateway.drivers.base.BaseDriver`
        """
        self._components = gateway_components
        self._started_at = time.monotonic()

        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()

        logger.info(
            "health_server.started",
            host=self._host,
            port=self._port,
        )

    async def stop(self) -> None:
        """Shut down the HTTP server gracefully."""
        if self._runner is not None:
            await self._runner.cleanup()
            logger.info("health_server.stopped")

    # ------------------------------------------------------------------
    # Route handlers
    # ------------------------------------------------------------------

    async def _handle_health(self, _request: web.Request) -> web.Response:
        """``GET /health`` — overall gateway health summary.

        Returns JSON with gateway status, uptime, driver states, tag
        statistics, and transport statistics.
        """
        uptime = time.monotonic() - self._started_at

        # Collect driver states
        drivers_info: list[dict[str, Any]] = []
        drivers = self._components.get("drivers", [])
        all_healthy = True
        for drv in drivers:
            drv_stats = drv.stats()
            drivers_info.append(drv_stats)
            if not drv_stats.get("healthy", False):
                all_healthy = False

        # Tag statistics
        tag_stats: dict[str, Any] = {}
        tag_manager = self._components.get("tag_manager")
        if tag_manager is not None and hasattr(tag_manager, "stats"):
            tag_stats = tag_manager.stats()

        # Transport statistics
        transport_stats: dict[str, Any] = {}
        udp_sender = self._components.get("udp_sender")
        if udp_sender is not None and hasattr(udp_sender, "stats"):
            transport_stats = udp_sender.stats()

        # Polling engine statistics
        engine_stats: dict[str, Any] = {}
        engine = self._components.get("polling_engine")
        if engine is not None and hasattr(engine, "stats"):
            engine_stats = engine.stats()

        overall_status = "healthy" if all_healthy and drivers else "degraded"
        if not drivers:
            overall_status = "no_drivers"

        body: dict[str, Any] = {
            "status": overall_status,
            "uptime_seconds": round(uptime, 2),
            "drivers": drivers_info,
            "tags": tag_stats,
            "transport": transport_stats,
            "polling_engine": engine_stats,
        }

        status_code = 200 if overall_status == "healthy" else 503
        return web.json_response(body, status=status_code)

    async def _handle_metrics(self, _request: web.Request) -> web.Response:
        """``GET /metrics`` — Prometheus text exposition format."""
        lines: list[str] = []

        uptime = time.monotonic() - self._started_at
        lines.append(f"# HELP osdd_gateway_uptime_seconds Gateway uptime in seconds.")
        lines.append(f"# TYPE osdd_gateway_uptime_seconds gauge")
        lines.append(f"osdd_gateway_uptime_seconds {uptime:.2f}")

        # Per-driver metrics
        drivers = self._components.get("drivers", [])
        lines.append(f"# HELP osdd_driver_healthy Driver health status (1=healthy, 0=unhealthy).")
        lines.append(f"# TYPE osdd_driver_healthy gauge")
        for drv in drivers:
            s = drv.stats()
            healthy_val = 1 if s.get("healthy") else 0
            name = s.get("name", "unknown")
            protocol = s.get("protocol", "unknown")
            lines.append(
                f'osdd_driver_healthy{{driver="{name}",protocol="{protocol}"}} {healthy_val}'
            )

        lines.append(f"# HELP osdd_driver_total_reads Total successful read cycles per driver.")
        lines.append(f"# TYPE osdd_driver_total_reads counter")
        for drv in drivers:
            s = drv.stats()
            lines.append(
                f'osdd_driver_total_reads{{driver="{s.get("name", "unknown")}"}} {s.get("total_reads", 0)}'
            )

        lines.append(f"# HELP osdd_driver_total_errors Total read errors per driver.")
        lines.append(f"# TYPE osdd_driver_total_errors counter")
        for drv in drivers:
            s = drv.stats()
            lines.append(
                f'osdd_driver_total_errors{{driver="{s.get("name", "unknown")}"}} {s.get("total_errors", 0)}'
            )

        # Polling engine metrics
        engine = self._components.get("polling_engine")
        if engine is not None and hasattr(engine, "stats"):
            es = engine.stats()
            lines.append(f"# HELP osdd_polling_total_polls Total poll cycles.")
            lines.append(f"# TYPE osdd_polling_total_polls counter")
            lines.append(f'osdd_polling_total_polls {es.get("total_polls", 0)}')

            lines.append(f"# HELP osdd_polling_total_tags_read Total tags read.")
            lines.append(f"# TYPE osdd_polling_total_tags_read counter")
            lines.append(f'osdd_polling_total_tags_read {es.get("total_tags_read", 0)}')

            lines.append(f"# HELP osdd_polling_total_tags_sent Total tags sent via transport.")
            lines.append(f"# TYPE osdd_polling_total_tags_sent counter")
            lines.append(f'osdd_polling_total_tags_sent {es.get("total_tags_sent", 0)}')

        # Tag manager metrics
        tag_manager = self._components.get("tag_manager")
        if tag_manager is not None and hasattr(tag_manager, "stats"):
            ts = tag_manager.stats()
            lines.append(f"# HELP osdd_tags_total Total number of tracked tags.")
            lines.append(f"# TYPE osdd_tags_total gauge")
            lines.append(f'osdd_tags_total {ts.get("total_tags", 0)}')

        # Transport metrics
        udp_sender = self._components.get("udp_sender")
        if udp_sender is not None and hasattr(udp_sender, "stats"):
            us = udp_sender.stats()
            lines.append(f"# HELP osdd_transport_packets_sent Total UDP packets sent.")
            lines.append(f"# TYPE osdd_transport_packets_sent counter")
            lines.append(f'osdd_transport_packets_sent {us.get("packets_sent", 0)}')

            lines.append(f"# HELP osdd_transport_bytes_sent Total bytes sent via UDP.")
            lines.append(f"# TYPE osdd_transport_bytes_sent counter")
            lines.append(f'osdd_transport_bytes_sent {us.get("bytes_sent", 0)}')

        body = "\n".join(lines) + "\n"
        return web.Response(
            text=body,
            content_type="text/plain; version=0.0.4; charset=utf-8",
        )

    async def _handle_tags(self, _request: web.Request) -> web.Response:
        """``GET /tags`` — JSON dump of all tracked tags (debug endpoint).

        Returns a complete snapshot of every tag currently held by the
        tag manager.
        """
        tag_manager = self._components.get("tag_manager")
        if tag_manager is None:
            return web.json_response(
                {"error": "tag_manager not available"},
                status=503,
            )

        if hasattr(tag_manager, "dump"):
            tags_data = tag_manager.dump()
        elif hasattr(tag_manager, "all_tags"):
            tags_data = tag_manager.all_tags()
        else:
            tags_data = {"error": "tag_manager does not support dump or all_tags"}

        # Attempt JSON serialisation; fall back to str() for non-serialisable
        # values.
        try:
            body = json.dumps(tags_data, default=str)
        except TypeError:
            body = json.dumps({"raw": str(tags_data)})

        return web.Response(
            text=body,
            content_type="application/json",
        )
