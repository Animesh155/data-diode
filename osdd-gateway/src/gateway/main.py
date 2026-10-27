"""OSDD Protocol Gateway — main entry point.

Parses CLI arguments, configures structured logging, initialises all gateway
subsystems, and runs the asynchronous event loop with graceful signal handling.

Usage
-----
::

    python -m gateway.main --config config/example_modbus.yaml --protocol modbus
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from typing import Any

import structlog

from gateway.config import ConfigError, load_config
from gateway.core.health import HealthServer
from gateway.core.polling_engine import PollingEngine

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


def _configure_logging(level: str, log_file: str | None = None) -> None:
    """Configure *structlog* with JSON rendering.

    Parameters
    ----------
    level:
        Python log-level name (e.g. ``"DEBUG"``, ``"INFO"``).
    log_file:
        Optional filesystem path.  When provided, JSON logs are also written
        to the given file.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(
        format="%(message)s",
        level=numeric_level,
        handlers=handlers,
        force=True,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


# ---------------------------------------------------------------------------
# Driver factory
# ---------------------------------------------------------------------------

_SUPPORTED_PROTOCOLS = ("modbus", "mqtt", "opcua")


def _create_driver(protocol: str, driver_config: dict[str, Any]) -> Any:
    """Instantiate a protocol driver using a simple factory pattern.

    Parameters
    ----------
    protocol:
        One of ``modbus``, ``mqtt``, or ``opcua``.
    driver_config:
        Driver-specific configuration dictionary.

    Returns
    -------
    BaseDriver
        An instance of the requested protocol driver.

    Raises
    ------
    NotImplementedError
        Until concrete driver modules are wired in.
    """
    if protocol == "modbus":
        try:
            from gateway.drivers.modbus_driver import ModbusDriver  # type: ignore[import-not-found]

            return ModbusDriver(driver_config)
        except ImportError:
            raise NotImplementedError(
                "Modbus driver not yet implemented — "
                "create gateway.drivers.modbus_driver.ModbusDriver"
            )
    elif protocol == "mqtt":
        try:
            from gateway.drivers.mqtt_driver import MqttDriver  # type: ignore[import-not-found]

            return MqttDriver(driver_config)
        except ImportError:
            raise NotImplementedError(
                "MQTT driver not yet implemented — "
                "create gateway.drivers.mqtt_driver.MqttDriver"
            )
    elif protocol == "opcua":
        try:
            from gateway.drivers.opcua_driver import OpcuaDriver  # type: ignore[import-not-found]

            return OpcuaDriver(driver_config)
        except ImportError:
            raise NotImplementedError(
                "OPC-UA driver not yet implemented — "
                "create gateway.drivers.opcua_driver.OpcuaDriver"
            )
    else:
        raise ValueError(f"Unsupported protocol: {protocol!r}")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Build and parse command-line arguments.

    Parameters
    ----------
    argv:
        Argument list (defaults to ``sys.argv[1:]``).
    """
    parser = argparse.ArgumentParser(
        prog="osdd-gateway",
        description="OSDD Protocol Gateway — secure uni-directional data transfer",
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Path to the YAML configuration file.",
    )
    parser.add_argument(
        "--protocol",
        required=True,
        choices=_SUPPORTED_PROTOCOLS,
        help="Protocol driver to use (modbus | mqtt | opcua).",
    )
    parser.add_argument(
        "--health-port",
        type=int,
        default=None,
        help="Override the health-server port (default: from config or 9090).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity (default: INFO).",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Optional path originating JSON log output to a file.",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Main async entry point
# ---------------------------------------------------------------------------


async def _async_main(args: argparse.Namespace) -> None:
    """Asynchronous core of the gateway lifecycle."""
    log = structlog.get_logger(component="main")

    # ── Load configuration ────────────────────────────────────────────
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        log.error("main.config_error", error=str(exc))
        sys.exit(1)

    # ── Initialise core components ────────────────────────────────────
    # These imports are deferred so that the modules can be developed
    # independently and the gateway still boots for integration testing.
    try:
        from gateway.core.tag_manager import TagManager  # type: ignore[import-not-found]
    except ImportError:
        log.warning("main.tag_manager_stub", reason="TagManager not found, using stub")

        class TagManager:  # type: ignore[no-redef]
            def update(self, tag: Any) -> None: ...  # noqa: E704
            def stats(self) -> dict[str, Any]:  # noqa: E704
                return {"total_tags": 0}
            def dump(self) -> dict[str, Any]:  # noqa: E704
                return {}

    try:
        from gateway.core.filter_chain import FilterChain  # type: ignore[import-not-found]
    except ImportError:
        log.warning("main.filter_chain_stub", reason="FilterChain not found, using stub")

        class FilterChain:  # type: ignore[no-redef]
            def __init__(self, config: Any = None) -> None: ...  # noqa: E704
            def apply(self, tags: list[Any]) -> list[Any]:  # noqa: E704
                return tags

    try:
        from gateway.transport.udp_sender import UdpSender  # type: ignore[import-not-found]
    except ImportError:
        log.warning("main.udp_sender_stub", reason="UdpSender not found, using stub")

        class UdpSender:  # type: ignore[no-redef]
            def __init__(self, config: Any = None) -> None: ...  # noqa: E704
            async def send(self, tags: list[Any]) -> None: ...  # noqa: E704
            async def close(self) -> None: ...  # noqa: E704
            def stats(self) -> dict[str, Any]:  # noqa: E704
                return {"packets_sent": 0, "bytes_sent": 0}

    tag_manager = TagManager()
    filter_chain = FilterChain(config.get("filters"))
    udp_sender = UdpSender(config.get("transport"))

    # ── Create driver(s) ──────────────────────────────────────────────
    drivers: list[Any] = []
    driver_configs = config.get("driver", {}).get("devices", [])
    if not driver_configs:
        # Fall back: treat the entire driver section as a single device
        driver_configs = [config.get("driver", {})]

    for drv_cfg in driver_configs:
        try:
            driver = _create_driver(args.protocol, drv_cfg)
            drivers.append(driver)
        except (NotImplementedError, ValueError) as exc:
            log.error("main.driver_init_failed", error=str(exc))
            sys.exit(1)

    # ── Polling engine ────────────────────────────────────────────────
    polling_engine = PollingEngine(tag_manager, udp_sender, filter_chain)
    for drv in drivers:
        poll_interval = drv.config.get(
            "poll_interval",
            config.get("driver", {}).get("poll_interval", 1.0),
        )
        await polling_engine.add_driver(drv, poll_interval)

    # ── Health server ─────────────────────────────────────────────────
    health_cfg = config.get("health", {})
    health_port = args.health_port or health_cfg.get("port", 9090)
    health_host = health_cfg.get("host", "0.0.0.0")
    health_server = HealthServer(host=health_host, port=health_port)

    gateway_components: dict[str, Any] = {
        "polling_engine": polling_engine,
        "tag_manager": tag_manager,
        "udp_sender": udp_sender,
        "drivers": drivers,
    }

    # ── Signal handling ───────────────────────────────────────────────
    shutdown_event = asyncio.Event()

    def _signal_handler(sig: signal.Signals) -> None:
        log.info("main.signal_received", signal=sig.name)
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler, sig)

    # ── Start everything ──────────────────────────────────────────────
    log.info("main.starting")
    await health_server.start(gateway_components)
    await polling_engine.start()
    log.info(
        "main.running",
        protocol=args.protocol,
        drivers=len(drivers),
        health_port=health_port,
    )

    # ── Wait for shutdown signal ──────────────────────────────────────
    await shutdown_event.wait()

    # ── Graceful shutdown ─────────────────────────────────────────────
    log.info("main.shutting_down")
    await polling_engine.stop()
    for drv in drivers:
        await drv.disconnect()
    if hasattr(udp_sender, "close"):
        await udp_sender.close()
    await health_server.stop()
    log.info("main.shutdown_complete")


def main(argv: list[str] | None = None) -> None:
    """Parse arguments, configure logging, and launch the async loop."""
    args = _parse_args(argv)
    _configure_logging(args.log_level, args.log_file)
    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
