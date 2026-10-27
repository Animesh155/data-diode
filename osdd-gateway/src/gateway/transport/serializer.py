"""Binary serializer — msgpack-based packet framing for data-diode transport.

Serialises :class:`TagValue` instances into size-bounded UDP packets using
`msgpack <https://msgpack.org>`_ and reconstructs them on the receiving end.

Packet layout (msgpack map)::

    {
        "v": uint8,          # protocol version
        "seq": uint32,       # sequence counter
        "ts": uint64,        # epoch milliseconds
        "src": str,          # source gateway identifier
        "tags": [            # array of tag maps
            {"n": ..., "val": ..., "dt": ..., "q": ..., "ts": ..., "u": ..., "sd": ..., "md": ...},
            ...
        ]
    }
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import msgpack
import structlog

from src.gateway.core.tag_manager import TagValue

logger = structlog.get_logger(__name__)

# MTU-safe ceiling — leaves room for IP + UDP headers.
MAX_PACKET_SIZE: int = 1400

# Current wire-protocol version.
_PROTOCOL_VERSION: int = 1


@dataclass(slots=True)
class TagPacket:
    """Deserialised representation of a single wire packet.

    Attributes:
        version: Protocol version the packet was encoded with.
        sequence: Monotonically increasing sequence number.
        timestamp: Epoch milliseconds when the packet was created.
        source_gateway: Identifier of the sending gateway.
        tags: Contained tag readings as plain dictionaries.
    """

    version: int
    sequence: int
    timestamp: int
    source_gateway: str
    tags: list[dict[str, Any]] = field(default_factory=list)


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _tag_to_dict(tag: TagValue) -> dict[str, Any]:
    """Convert a :class:`TagValue` to a compact dictionary for wire encoding."""
    ts_ms: int = 0
    if tag.timestamp is not None:
        ts_ms = int(tag.timestamp.timestamp() * 1000)
    d: dict[str, Any] = {
        "n": tag.name,
        "val": tag.value,
        "dt": tag.data_type,
        "q": tag.quality,
        "ts": ts_ms,
    }
    if tag.unit is not None:
        d["u"] = tag.unit
    if tag.source_driver is not None:
        d["sd"] = tag.source_driver
    if tag.metadata:
        d["md"] = tag.metadata
    return d


def _dict_to_tag(d: dict[str, Any]) -> dict[str, Any]:
    """Return the raw tag dict as-is (kept for potential future transforms)."""
    return dict(d)


def _build_packet_bytes(
    tags_dicts: list[dict[str, Any]],
    sequence: int,
    source: str,
    timestamp_ms: int,
) -> bytes:
    """Pack a single packet payload and return the raw bytes."""
    payload: dict[str, Any] = {
        "v": _PROTOCOL_VERSION,
        "seq": sequence,
        "ts": timestamp_ms,
        "src": source,
        "tags": tags_dicts,
    }
    return msgpack.packb(payload, use_bin_type=True)


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------


def serialize_tags(
    tags: list[TagValue],
    sequence: int,
    source: str,
) -> list[bytes]:
    """Serialize a list of tags into one or more MTU-bounded packets.

    If the full batch exceeds :data:`MAX_PACKET_SIZE`, tags are split across
    multiple packets, each sharing the same *sequence* base (incremented by
    their sub-index).

    Args:
        tags: Tag snapshots to send.
        sequence: Starting sequence number.
        source: Source gateway identifier embedded in every packet.

    Returns:
        Ordered list of msgpack-encoded byte buffers, each ≤
        :data:`MAX_PACKET_SIZE`.
    """
    if not tags:
        return []

    timestamp_ms = int(time.time() * 1000)
    tag_dicts = [_tag_to_dict(t) for t in tags]

    # Optimistic single-packet attempt.
    single = _build_packet_bytes(tag_dicts, sequence, source, timestamp_ms)
    if len(single) <= MAX_PACKET_SIZE:
        return [single]

    # Split into multiple packets.
    packets: list[bytes] = []
    current_batch: list[dict[str, Any]] = []
    sub_seq = 0

    for td in tag_dicts:
        current_batch.append(td)
        trial = _build_packet_bytes(current_batch, sequence + sub_seq, source, timestamp_ms)
        if len(trial) > MAX_PACKET_SIZE:
            if len(current_batch) == 1:
                # Single tag already exceeds limit — send anyway with a warning.
                logger.warning(
                    "serializer.oversized_tag",
                    tag=td.get("n"),
                    size=len(trial),
                    limit=MAX_PACKET_SIZE,
                )
                packets.append(trial)
                current_batch = []
                sub_seq += 1
            else:
                # Pop last, flush current batch, start new one.
                current_batch.pop()
                packets.append(
                    _build_packet_bytes(current_batch, sequence + sub_seq, source, timestamp_ms)
                )
                sub_seq += 1
                current_batch = [td]

    if current_batch:
        packets.append(
            _build_packet_bytes(current_batch, sequence + sub_seq, source, timestamp_ms)
        )

    logger.debug(
        "serializer.split",
        tag_count=len(tags),
        packet_count=len(packets),
    )
    return packets


def deserialize_tags(data: bytes) -> TagPacket:
    """Deserialize a single msgpack packet into a :class:`TagPacket`.

    Args:
        data: Raw bytes received from the wire.

    Returns:
        Parsed :class:`TagPacket`.

    Raises:
        ValueError: If the payload is malformed or missing required fields.
    """
    try:
        raw = msgpack.unpackb(data, raw=False)
    except (msgpack.UnpackException, ValueError) as exc:
        raise ValueError(f"Failed to unpack msgpack data: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"Expected dict payload, got {type(raw).__name__}")

    required = {"v", "seq", "ts", "src", "tags"}
    missing = required - set(raw.keys())
    if missing:
        raise ValueError(f"Missing required fields: {missing}")

    return TagPacket(
        version=int(raw["v"]),
        sequence=int(raw["seq"]),
        timestamp=int(raw["ts"]),
        source_gateway=str(raw["src"]),
        tags=[_dict_to_tag(t) for t in raw["tags"]],
    )
