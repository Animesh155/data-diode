"""
frame.py — The universal wire format for the data diode.

This is the exact binary layout that crosses the diode.
Every component (OT proxy, FPGA, IT proxy) must agree on this.

═══════════════════════════════════════════════════════════════════════
 TWO FORMATS LIVE HERE
═══════════════════════════════════════════════════════════════════════

  v2 BATCH  (the LIVE wire format — everything below "BATCH FORMAT")
            One datagram carries ALL readings from one poll cycle.
            Integrity = CRC-32 only. Loss recovery is handled at the
            application layer (keyframe refresh + send-on-change in the
            OT proxy), NOT by an in-frame error-correcting code.

  v1 SINGLE + Reed-Solomon  (RETAINED AS REFERENCE ONLY — not called by
            the live pipeline). This is the bit-accurate reference model
            for the eventual FPGA RS IP core. See RS_* below. It stays in
            the tree so the Verilog work has a golden model to diff against,
            but the running proxies do not use build_frame()/parse_frame().

 Why the split: on this UDP link the dominant failure is whole-packet
 LOSS, not in-flight byte corruption (Ethernet FCS already drops corrupt
 frames before they reach us). An intra-frame RS code corrects byte errors
 but does nothing for a dropped datagram, so it is the wrong tool for the
 live path. If/when a raw optical TX-only PHY exists (no link-layer FCS),
 RS belongs THERE, covering the whole frame — which is what the reference
 model below is for.

═══════════════════════════════════════════════════════════════════════
 BATCH FORMAT (v2) — WIRE LAYOUT
═══════════════════════════════════════════════════════════════════════

┌──────┬─────────┬───────┬──────┬───────────┬───────┬───────────────┬────────┐
│ SYNC │ VERSION │ FLAGS │ SEQ  │ TIMESTAMP │ COUNT │ RECORDS[COUNT] │ CRC-32 │
│ 2B   │ 1B      │ 1B    │ 4B   │ 4B        │ 2B    │ 7B each        │ 4B     │
└──────┴─────────┴───────┴──────┴───────────┴───────┴───────────────┴────────┘
 0xAA55  0x02      bits    uint32  epoch sec   uint16                  CRC over
                                                                       VERSION..
                                                                       last record

  FLAGS bit0 (0x01) = KEYFRAME  — this batch is a full tag-table refresh
  FLAGS bit1 (0x02) = HEARTBEAT — this batch is an "I'm alive" frame

  RECORD (7 bytes):  tag_id (uint16) + value (float32) + quality (uint8)
                     The per-poll TIMESTAMP is shared by every record in
                     the batch, so it is stored once in the header instead
                     of once per reading.

  Header = 14 bytes.  Total = 14 + 7*COUNT + 4.
  Example (15 sensors): 14 + 105 + 4 = 123 bytes for the whole poll,
  versus 15 separate 28-byte frames in the old per-reading format.

═══════════════════════════════════════════════════════════════════════
"""

import struct
import zlib

# ── Wire-format constants ────────────────────────────────────
SYNC_WORD = 0xAA55

# Quality codes — what the sensor is reporting about its own health
QUALITY_GOOD       = 0x00
QUALITY_UNCERTAIN  = 0x01
QUALITY_BAD        = 0x02
QUALITY_COMM_FAIL  = 0xFF

# ═════════════════════════════════════════════════════════════
#  BATCH FORMAT (v2) — the LIVE wire format
# ═════════════════════════════════════════════════════════════
BATCH_VERSION       = 0x02

# FLAGS bits
FLAG_KEYFRAME       = 0x01      # full tag-table refresh
FLAG_HEARTBEAT      = 0x02      # "I'm alive" frame (single heartbeat record)

# Header: sync(H) version(B) flags(B) seq(I) timestamp(I) count(H)
BATCH_HEADER_FMT    = '!HBBIIH'
BATCH_HEADER_SIZE   = 14
# Record: tag_id(H) value(f) quality(B)
RECORD_FMT          = '!HfB'
RECORD_SIZE         = 7
BATCH_CRC_SIZE      = 4
BATCH_OVERHEAD      = BATCH_HEADER_SIZE + BATCH_CRC_SIZE   # 18 non-record bytes

# ── CRC-32 specification (hardware parity) ───────────────────
# The batch CRC is CRC-32/ISO-HDLC — bit-for-bit identical to Python's
# zlib.crc32(). The eventual FPGA CRC unit MUST use these exact parameters
# to agree with this reference model:
#   polynomial     0x04C11DB7   (reflected form 0xEDB88320)
#   init           0xFFFFFFFF
#   refin = refout = true        (input and output reflected)
#   final XOR      0xFFFFFFFF
#   check("123456789") = 0xCBF43926
CRC32_POLY_NORMAL   = 0x04C11DB7
CRC32_POLY_REFLECT  = 0xEDB88320
CRC32_INIT          = 0xFFFFFFFF
CRC32_XOROUT        = 0xFFFFFFFF
CRC32_CHECK         = 0xCBF43926     # zlib.crc32(b"123456789")

# ── MTU / fragmentation safety ───────────────────────────────
# A batch must fit in ONE UDP datagram without IP fragmentation. A
# fragmented UDP datagram is lost entirely if any single fragment drops,
# and many OT / firewalled networks drop IP fragments outright — so a
# too-big batch would sharply worsen loss behaviour. Budget on standard
# Ethernet:
#   1500 (MTU) - 20 (IPv4) - 8 (UDP) = 1472 usable
#   1472 - 18 (batch overhead)       = 1454 for records
#   1454 // 7                        = 207 records; capped at 200 for
#                                      headroom (VLAN tags, options).
# The OT proxy SPLITS anything larger across multiple datagrams, each a
# self-contained batch with its own SEQ. The parser rejects a COUNT above
# this bound as corruption (also the crafted-COUNT / DoS guard).
ETHERNET_MTU        = 1500
IP_UDP_OVERHEAD     = 28
MAX_RECORDS_PER_DATAGRAM = min(
    200, (ETHERNET_MTU - IP_UDP_OVERHEAD - BATCH_OVERHEAD) // RECORD_SIZE)


def build_batch(seq: int, timestamp: int, records: list,
                keyframe: bool = False, heartbeat: bool = False) -> bytes:
    """
    Build one batch datagram from a list of readings.

    Args:
        seq:       batch sequence number (uint32) — one per datagram sent
        timestamp: shared poll timestamp (epoch seconds, uint32)
        records:   list of (tag_id:int, value:float, quality:int) tuples
        keyframe:  True if this is a full tag-table refresh
        heartbeat: True if this is a heartbeat frame

    Returns:
        Complete batch frame: header + records + CRC-32.
    """
    flags = 0
    if keyframe:
        flags |= FLAG_KEYFRAME
    if heartbeat:
        flags |= FLAG_HEARTBEAT

    count = len(records)
    # Contract: one batch = one un-fragmented datagram. Callers that may
    # exceed this (large keyframes) must split first — see send_batch().
    assert count <= MAX_RECORDS_PER_DATAGRAM, (
        f"batch of {count} records exceeds MTU-safe cap "
        f"{MAX_RECORDS_PER_DATAGRAM}; split before building")
    header = struct.pack(BATCH_HEADER_FMT,
                         SYNC_WORD, BATCH_VERSION, flags, seq, timestamp, count)
    body = b''.join(
        struct.pack(RECORD_FMT, tag_id, float(value), quality)
        for (tag_id, value, quality) in records
    )
    # CRC covers everything except SYNC (alignment only) and the CRC itself.
    crc = zlib.crc32(header[2:] + body) & 0xFFFFFFFF
    return header + body + struct.pack('!I', crc)


def parse_batch(data: bytes) -> dict | None:
    """
    Parse a v2 batch datagram. Returns a dict, or None if the frame is
    malformed / fails CRC (no error correction — corrupt frames are dropped).

    Returned dict:
        seq, timestamp, count, keyframe(bool), heartbeat(bool),
        records: [ {tag_id, value, quality}, ... ],
        raw, total_len
    """
    if len(data) < BATCH_HEADER_SIZE + BATCH_CRC_SIZE:
        return None

    sync, version, flags, seq, timestamp, count = struct.unpack(
        BATCH_HEADER_FMT, data[:BATCH_HEADER_SIZE])

    if sync != SYNC_WORD or version != BATCH_VERSION:
        return None

    # Bound the record count before trusting it (crafted-COUNT protection,
    # and a batch above the MTU-safe cap can't be one we legitimately sent).
    if count > MAX_RECORDS_PER_DATAGRAM:
        return None

    body_end = BATCH_HEADER_SIZE + count * RECORD_SIZE
    expected_len = body_end + BATCH_CRC_SIZE
    if len(data) < expected_len:
        return None

    received_crc = struct.unpack('!I', data[body_end:expected_len])[0]
    computed_crc = zlib.crc32(data[2:body_end]) & 0xFFFFFFFF
    if computed_crc != received_crc:
        return None

    records = []
    for i in range(count):
        off = BATCH_HEADER_SIZE + i * RECORD_SIZE
        tag_id, value, quality = struct.unpack(RECORD_FMT, data[off:off + RECORD_SIZE])
        records.append({
            'tag_id': tag_id,
            'value': round(value, 4),
            'quality': quality,
        })

    return {
        'seq': seq,
        'timestamp': timestamp,
        'count': count,
        'keyframe': bool(flags & FLAG_KEYFRAME),
        'heartbeat': bool(flags & FLAG_HEARTBEAT),
        'records': records,
        'raw': data[:expected_len],
        'total_len': expected_len,
    }


# ═════════════════════════════════════════════════════════════
#  v1 SINGLE-FRAME + REED-SOLOMON — REFERENCE MODEL ONLY
#  ---------------------------------------------------------------
#  NOT used by the live pipeline. Retained as the bit-accurate golden
#  model for the eventual FPGA Reed-Solomon IP core. Do not wire this
#  back into the proxies without revisiting the loss-vs-corruption
#  analysis in the module docstring.
# ═════════════════════════════════════════════════════════════

# ── Reed-Solomon parameters (HARDWARE-AWARE) ─────────────────
# These exact values must be used in the Verilog implementation.
RS_CODE_N           = 15        # total code word size (symbols)
RS_CODE_K           = 11        # data symbols
RS_PARITY_BYTES     = 4         # parity symbols (= n - k)
RS_CORRECTION_T     = 2         # max correctable byte errors
RS_SYMBOL_BITS      = 8         # GF(2^m), m = 8
RS_FIELD_POLY       = 0x11D     # primitive polynomial
RS_FIRST_CONSEC_ROOT = 0        # fcr
RS_GENERATOR        = 2         # generator base alpha

# ── Single-frame size constants (v1 reference) ───────────────
HEADER_SIZE         = 9         # SYNC(2) + SEQ(4) + LEN(2) + TTL(1)
SENSOR_STRUCT_SIZE  = 11        # the payload size (= RS_CODE_K)
CRC_SIZE            = 4         # CRC-32
FRAME_SIZE          = HEADER_SIZE + SENSOR_STRUCT_SIZE + RS_PARITY_BYTES + CRC_SIZE
                    #  = 9 + 11 + 4 + 4 = 28 bytes

# Field offsets (for hardware port reference)
OFFSET_SYNC         = 0         # bytes 0-1
OFFSET_SEQ          = 2         # bytes 2-5
OFFSET_LEN          = 6         # bytes 6-7
OFFSET_TTL          = 8         # byte  8
OFFSET_PAYLOAD      = 9         # bytes 9-19
OFFSET_RS_PARITY    = 20        # bytes 20-23
OFFSET_CRC          = 24        # bytes 24-27

# ── Reed-Solomon codec (lazy init) ───────────────────────────
_rs_codec = None

def _get_rs():
    """Initialize Reed-Solomon codec on first use."""
    global _rs_codec
    if _rs_codec is None:
        from reedsolo import RSCodec
        _rs_codec = RSCodec(
            nsym=RS_PARITY_BYTES,    # 4 parity symbols
            nsize=RS_CODE_N,         # 15 total
            fcr=RS_FIRST_CONSEC_ROOT,
            prim=RS_FIELD_POLY,
            generator=RS_GENERATOR,
            c_exp=RS_SYMBOL_BITS,
        )
    return _rs_codec


def rs_encode(payload: bytes) -> bytes:
    """
    Reed-Solomon encode: 11 bytes → 15 bytes (data + parity).
    Returns ONLY the 4 parity bytes (data is unchanged).

    Hardware equivalent: Xilinx RS Encoder LogiCORE configured
    with identical parameters produces bit-identical output.
    """
    assert len(payload) == RS_CODE_K, \
        f"RS input must be exactly {RS_CODE_K} bytes, got {len(payload)}"
    code_word = _get_rs().encode(payload)
    # code_word = data (11B) + parity (4B). We want just the parity.
    assert len(code_word) == RS_CODE_N
    return bytes(code_word[RS_CODE_K:])


def rs_decode(payload: bytes, parity: bytes) -> tuple:
    """
    Reed-Solomon decode: validate and (if possible) correct errors.

    Returns:
        (corrected_payload, num_errors_corrected) on success
        (None, 0) if uncorrectable (> 2 byte errors)

    Hardware equivalent: Xilinx RS Decoder LogiCORE configured
    with identical parameters produces equivalent corrections.
    """
    from reedsolo import ReedSolomonError
    if len(payload) != RS_CODE_K or len(parity) != RS_PARITY_BYTES:
        return (None, 0)
    code_word = bytes(payload) + bytes(parity)
    try:
        decoded, _, errata_pos = _get_rs().decode(code_word)
        # decoded should be the 11-byte data portion
        return (bytes(decoded), len(errata_pos))
    except ReedSolomonError:
        return (None, 0)


# ── Sensor struct (v1 reference) ─────────────────────────────
def pack_sensor_struct(timestamp: int, tag_id: int, value: float, quality: int) -> bytes:
    """
    Pack a single sensor reading into the 11-byte binary struct.
    Format: !IHfB = big-endian uint32 + uint16 + float32 + uint8
    """
    return struct.pack('!IHfB', timestamp, tag_id, value, quality)


def unpack_sensor_struct(data: bytes) -> dict:
    """Unpack the 11-byte struct back into human-readable fields."""
    timestamp, tag_id, value, quality = struct.unpack('!IHfB', data)
    return {
        'timestamp': timestamp,
        'tag_id': tag_id,
        'value': round(value, 4),
        'quality': quality,
    }


# ── Single-frame builder (v1 reference) ──────────────────────
def build_frame(seq: int, ttl: int, payload: bytes) -> bytes:
    """
    Wrap a payload in the full 28-byte frame envelope with RS+CRC.

    REFERENCE ONLY — the live pipeline uses build_batch(). Kept as the
    golden model for the FPGA Reed-Solomon IP core.
    """
    assert len(payload) == SENSOR_STRUCT_SIZE, \
        f"payload must be {SENSOR_STRUCT_SIZE} bytes, got {len(payload)}"

    # Pack the 9-byte header
    header = struct.pack('!HI H B',
        SYNC_WORD,              # 2 bytes: sync marker (alignment only)
        seq,                    # 4 bytes: sequence number
        len(payload),           # 2 bytes: payload length (= 11)
        ttl,                    # 1 byte:  copy number
    )

    # Compute Reed-Solomon parity over PAYLOAD only
    rs_parity = rs_encode(payload)   # 4 bytes

    # CRC covers everything EXCEPT sync and CRC itself.
    crc_data = header[2:] + payload + rs_parity   # bytes 2..23
    crc = zlib.crc32(crc_data) & 0xFFFFFFFF
    crc_bytes = struct.pack('!I', crc)

    return header + payload + rs_parity + crc_bytes


# ── Single-frame parser (v1 reference) ───────────────────────
def parse_frame(data: bytes) -> dict | None:
    """
    Parse a 28-byte v1 frame with CRC + RS recovery.

    REFERENCE ONLY — the live pipeline uses parse_batch(). Kept as the
    golden model for the FPGA Reed-Solomon IP core.
    """
    if len(data) < FRAME_SIZE:
        return None

    sync, seq, payload_len, ttl = struct.unpack('!HIHB', data[:HEADER_SIZE])

    if sync != SYNC_WORD:
        return None

    if payload_len != SENSOR_STRUCT_SIZE:
        return None

    expected_total = FRAME_SIZE
    if len(data) < expected_total:
        return None

    payload   = data[OFFSET_PAYLOAD : OFFSET_PAYLOAD + SENSOR_STRUCT_SIZE]
    rs_parity = data[OFFSET_RS_PARITY : OFFSET_RS_PARITY + RS_PARITY_BYTES]
    received_crc = struct.unpack('!I', data[OFFSET_CRC : OFFSET_CRC + CRC_SIZE])[0]

    # ── Fast path: verify CRC ────────────────────────────────
    crc_data = data[OFFSET_SEQ : OFFSET_CRC]   # bytes 2..23
    computed_crc = zlib.crc32(crc_data) & 0xFFFFFFFF

    if computed_crc == received_crc:
        return {
            'seq': seq, 'ttl': ttl, 'payload_len': payload_len,
            'payload': payload, 'rs_corrected': 0, 'crc': received_crc,
            'raw': data[:expected_total], 'total_len': expected_total,
        }

    # ── Slow path: try RS error correction ───────────────────
    corrected_payload, num_errors = rs_decode(payload, rs_parity)

    if corrected_payload is None:
        return None

    return {
        'seq': seq, 'ttl': ttl, 'payload_len': payload_len,
        'payload': corrected_payload, 'rs_corrected': num_errors,
        'crc': received_crc, 'raw': data[:expected_total],
        'total_len': expected_total,
    }
