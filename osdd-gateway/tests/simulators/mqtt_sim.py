#!/usr/bin/env python3
"""MQTT test publisher — simulates sensor data for the OSDD MQTT gateway.

Publishes to a local Mosquitto (or any MQTT) broker at a configurable
interval with small random noise on numeric values.

Usage::

    # From the project root:
    python -m tests.simulators.mqtt_sim

    # Custom broker / interval:
    python -m tests.simulators.mqtt_sim --host 192.168.1.50 --port 1883 --interval 2.0
"""

from __future__ import annotations

import argparse
import json
import random
import signal
import sys
import time

import paho.mqtt.client as mqtt


# ── Simulated data sources ───────────────────────────────────────────────────

SENSORS: list[dict] = [
    {
        "topic": "factory/temperature/zone1",
        "base": 23.5,
        "noise": 1.5,
        "unit": "C",
        "fmt": "json",
    },
    {
        "topic": "factory/temperature/zone2",
        "base": 45.2,
        "noise": 2.0,
        "unit": "C",
        "fmt": "json",
    },
    {
        "topic": "factory/humidity/zone1",
        "base": 65.0,
        "noise": 3.0,
        "unit": "%",
        "fmt": "json",
    },
    {
        "topic": "factory/status/pump1",
        "values": ["RUNNING", "RUNNING", "RUNNING", "IDLE"],  # mostly running
        "fmt": "plain",
    },
]


def _generate_payload(sensor: dict) -> tuple[str, str | bytes]:
    """Return ``(topic, payload)`` for a single sensor."""
    topic = sensor["topic"]

    if sensor["fmt"] == "json":
        value = round(sensor["base"] + random.uniform(-sensor["noise"], sensor["noise"]), 2)
        payload = json.dumps({"value": value, "unit": sensor["unit"]})
    else:
        payload = random.choice(sensor["values"])

    return topic, payload


# ── Main publish loop ────────────────────────────────────────────────────────

_running = True


def _signal_handler(sig: int, frame: object) -> None:
    global _running
    _running = False
    print("\n⏹  Stopping simulator…")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MQTT test publisher for OSDD gateway",
    )
    parser.add_argument(
        "--host",
        default="localhost",
        help="MQTT broker hostname (default: localhost)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=1883,
        help="MQTT broker port (default: 1883)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Publish interval in seconds (default: 1.0)",
    )
    parser.add_argument(
        "--client-id",
        default="osdd-mqtt-sim",
        help="MQTT client ID (default: osdd-mqtt-sim)",
    )
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    print(f"🚀 Connecting to MQTT broker at {args.host}:{args.port} …")

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=args.client_id,
    )

    try:
        client.connect(args.host, args.port, keepalive=60)
    except Exception as exc:
        print(f"❌ Failed to connect: {exc}")
        sys.exit(1)

    client.loop_start()
    print(f"✅ Connected.  Publishing every {args.interval}s.  Ctrl+C to stop.\n")

    msg_count = 0
    while _running:
        for sensor in SENSORS:
            topic, payload = _generate_payload(sensor)
            client.publish(topic, payload, qos=0)
            msg_count += 1
            print(f"  📤  {topic:<35s}  →  {payload}")

        print(f"  ── cycle complete ({msg_count} messages total) ──\n")
        time.sleep(args.interval)

    client.loop_stop()
    client.disconnect()
    print(f"👋 Disconnected after {msg_count} messages.")


if __name__ == "__main__":
    main()
