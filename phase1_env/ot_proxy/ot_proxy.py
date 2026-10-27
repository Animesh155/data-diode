"""
ot_proxy.py — OT-Side Proxy (batched, keyframe-refresh version)

The bridge between the industrial world and the data diode.

What this does:
  1. POLL:       Fetch sensor data via PLUGGABLE ADAPTERS (Modbus, S7, OPC UA)
  2. NORMALIZE:  Adapters convert protocol-specific responses to universal readings
  3. SELECT:     Send-on-change — only readings that moved past the deadband,
                 EXCEPT every KEYFRAME_INTERVAL seconds we send the full tag
                 table (a "keyframe"). A lost packet self-heals at the next
                 keyframe, so no back-channel / retransmission is needed.
  4. BATCH:      Pack all selected readings for the poll into ONE datagram
                 (shared timestamp + CRC amortized over every reading).
  5. SEND:       One UDP datagram per poll to the FPGA (one-way, no response).
  6. HEARTBEAT:  A tiny separate frame sent EVERY cycle, duplicated
                 HEARTBEAT_COPIES times (it's the one can't-miss signal, and
                 3× of a ~21-byte frame is negligible bandwidth).

Loss recovery is application-layer (keyframe refresh), not an in-frame
error-correcting code — see frame.py for why. Integrity is CRC-32.

Adapter selection: set the ADAPTER environment variable
    ADAPTER=modbus | s7 | opcua | http
"""

import time
import socket
import urllib.request
import json
import os
from frame import (
    build_batch, MAX_RECORDS_PER_DATAGRAM,
    QUALITY_GOOD, QUALITY_COMM_FAIL,
)

# ── Heartbeat constants ───────────────────────────────────────
# Reserved tag_id for "I'm alive" frames. The IT proxy treats any
# record with this tag as a heartbeat (not a sensor reading).
HEARTBEAT_TAG_ID = 0xFFFF

# ── Configuration ─────────────────────────────────────────────
ADAPTER_TYPE      = os.environ.get('ADAPTER', 'modbus').lower()
FPGA_HOST         = os.environ.get('FPGA_HOST', 'fpga-sim')
FPGA_PORT         = int(os.environ.get('FPGA_PORT', '5000'))
POLL_INTERVAL     = float(os.environ.get('POLL_INTERVAL', '1.0'))

# Loss recovery / bandwidth knobs.
#   KEYFRAME_INTERVAL — seconds between full tag-table refreshes. This is
#     the recovery bound: a lost delta is corrected within this window.
#   CHANGE_DEADBAND   — fractional change required to send a tag between
#     keyframes. 0.0 = send on ANY difference (lossless, but analog signals
#     then move nearly every poll). 0.01 = 1% deadband (historian-style
#     compression — big bandwidth win, small fidelity trade-off).
#   HEARTBEAT_COPIES  — duplicate count for the heartbeat frame only.
KEYFRAME_INTERVAL = float(os.environ.get('KEYFRAME_INTERVAL', '5.0'))
CHANGE_DEADBAND   = float(os.environ.get('CHANGE_DEADBAND', '0.0'))
HEARTBEAT_COPIES  = int(os.environ.get('HEARTBEAT_COPIES', '3'))

# Modbus adapter config
MODBUS_HOST       = os.environ.get('MODBUS_HOST', 'plc-modbus')
MODBUS_PORT       = int(os.environ.get('MODBUS_PORT', '502'))

# S7 (Siemens) adapter config
S7_HOST           = os.environ.get('S7_HOST', 'plc-s7')
S7_RACK           = int(os.environ.get('S7_RACK', '0'))
S7_SLOT           = int(os.environ.get('S7_SLOT', '1'))

# OPC UA adapter config
OPCUA_URL         = os.environ.get('OPCUA_URL', 'opc.tcp://plc-opcua:4840/lab/')

# Legacy HTTP adapter config (for backward compatibility with Node-RED)
SENSOR_POLL_URL   = os.environ.get('SENSOR_URL', 'http://ot-simulator:1880/api/plc-data')

# Legacy TAG_MAP for HTTP adapter
TAG_MAP_HTTP = {
    'temperature': 0x0001,
    'pressure':    0x0002,
    'status':      0x0003,
}


# ── Legacy HTTP Adapter (kept for backward compatibility) ─────
def poll_http_json() -> list[dict]:
    """Original Node-RED HTTP/JSON poll. Used if ADAPTER=http."""
    try:
        req = urllib.request.Request(SENSOR_POLL_URL)
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"[POLL] HTTP failed: {e}")
        return []

    timestamp = int(time.time())
    readings = []
    tags = data.get('tags', {})

    if 'temperature' in tags:
        readings.append({
            'timestamp': timestamp, 'tag_id': TAG_MAP_HTTP['temperature'],
            'value': float(tags['temperature']), 'quality': QUALITY_GOOD,
            'name': 'temperature',
        })
    if 'pressure' in tags:
        readings.append({
            'timestamp': timestamp, 'tag_id': TAG_MAP_HTTP['pressure'],
            'value': float(tags['pressure']), 'quality': QUALITY_GOOD,
            'name': 'pressure',
        })
    if 'status' in tags:
        status_val = 0.0 if tags['status'] == 'RUNNING' else 1.0
        readings.append({
            'timestamp': timestamp, 'tag_id': TAG_MAP_HTTP['status'],
            'value': status_val, 'quality': QUALITY_GOOD,
            'name': 'status',
        })
    return readings


# ── Send-on-change tracking ──────────────────────────────────
def send_batch(sock, dest, seq: int, timestamp: int, records: list,
               keyframe: bool = False) -> int:
    """
    Send `records` as one or more MTU-safe batch datagrams, splitting when
    the record count exceeds MAX_RECORDS_PER_DATAGRAM so no datagram ever
    IP-fragments. Each chunk is a self-contained batch with its own SEQ
    (a split keyframe is still N keyframes — the IT side converges once it
    has seen all chunks). Returns the next free SEQ.
    """
    cap = MAX_RECORDS_PER_DATAGRAM
    for i in range(0, len(records), cap):
        chunk = records[i:i + cap]
        frame = build_batch(seq, timestamp, chunk, keyframe=keyframe)
        sock.sendto(frame, dest)
        seq += 1
    return seq


def has_changed(tag_id: int, value: float, quality: int,
                last_sent: dict, deadband: float) -> bool:
    """
    Decide whether a reading is worth sending between keyframes.
    Sends if the tag is new, its quality changed, or its value moved
    past the (fractional) deadband relative to what we last transmitted.
    """
    if tag_id not in last_sent:
        return True
    prev_value, prev_quality = last_sent[tag_id]
    if quality != prev_quality:
        return True
    denom = max(abs(prev_value), 1e-6)
    return abs(value - prev_value) / denom > deadband


# ── Adapter Factory ──────────────────────────────────────────
def create_adapter():
    """Build the adapter specified by ADAPTER env var."""
    if ADAPTER_TYPE == 'modbus':
        from adapters.modbus_adapter import ModbusAdapter
        adapter = ModbusAdapter(host=MODBUS_HOST, port=MODBUS_PORT)
        print(f"[ADAPTER] Modbus TCP → {MODBUS_HOST}:{MODBUS_PORT}")
        return adapter
    elif ADAPTER_TYPE == 's7':
        from adapters.s7_adapter import S7Adapter
        adapter = S7Adapter(host=S7_HOST, rack=S7_RACK, slot=S7_SLOT)
        print(f"[ADAPTER] S7comm → {S7_HOST} (rack {S7_RACK}, slot {S7_SLOT})")
        return adapter
    elif ADAPTER_TYPE == 'opcua':
        from adapters.opcua_adapter import OpcUaAdapter
        adapter = OpcUaAdapter(url=OPCUA_URL)
        print(f"[ADAPTER] OPC UA → {OPCUA_URL}")
        return adapter
    elif ADAPTER_TYPE == 'http':
        print(f"[ADAPTER] HTTP/JSON (legacy) → {SENSOR_POLL_URL}")
        return None   # use legacy poll_http_json()
    else:
        raise ValueError(f"Unknown ADAPTER type: {ADAPTER_TYPE}")


# ── Main Loop ─────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("  OT-SIDE PROXY — Batched / Keyframe-Refresh Diode Sender")
    print("=" * 60)
    print(f"  Adapter:      {ADAPTER_TYPE}")
    print(f"  Sending to:   {FPGA_HOST}:{FPGA_PORT} (UDP)")
    print(f"  Poll:         {POLL_INTERVAL}s")
    print(f"  Keyframe:     every {KEYFRAME_INTERVAL}s (full refresh)")
    print(f"  Deadband:     {CHANGE_DEADBAND * 100:.2f}% (send-on-change)")
    print(f"  HB copies:    {HEARTBEAT_COPIES}x")
    print("=" * 60)

    adapter = create_adapter()

    # Wait for adapter to be ready
    print("[INIT] Waiting for sensor source to come up...")
    while True:
        if adapter is None:
            try:
                urllib.request.urlopen(SENSOR_POLL_URL, timeout=2.0)
                print("[INIT] HTTP source is up!")
                break
            except Exception:
                time.sleep(2)
        else:
            if adapter.connect():
                print("[INIT] PLC is up!")
                break
            time.sleep(2)

    # UDP socket — send only
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    seq = 0
    total_readings_sent = 0
    last_sent = {}                       # tag_id -> (value, quality)
    # Force a keyframe on the very first poll.
    last_keyframe_monotonic = None
    start_time_monotonic = time.monotonic()

    while True:
        # ── Step 1: Poll ──────────────────────────────────────
        if adapter is None:
            readings = poll_http_json()
        else:
            readings = adapter.poll()

        now_mono = time.monotonic()
        timestamp = int(time.time())

        # ── Step 2: Keyframe vs delta selection ───────────────
        is_keyframe = (last_keyframe_monotonic is None or
                       (now_mono - last_keyframe_monotonic) >= KEYFRAME_INTERVAL)

        if is_keyframe:
            selected = list(readings)
            last_keyframe_monotonic = now_mono
        else:
            selected = [
                r for r in readings
                if has_changed(r['tag_id'], r['value'], r['quality'],
                               last_sent, CHANGE_DEADBAND)
            ]

        # ── Step 3: Batch + send sensor data (split if > 1 MTU) ─
        sent_kind = None
        if selected:
            records = [(r['tag_id'], r['value'], r['quality']) for r in selected]
            start_seq = seq
            seq = send_batch(sock, (FPGA_HOST, FPGA_PORT), seq, timestamp,
                             records, keyframe=is_keyframe)
            for r in selected:
                last_sent[r['tag_id']] = (r['value'], r['quality'])
            total_readings_sent += len(selected)
            sent_kind = 'KEYFRAME' if is_keyframe else 'delta'
            n_datagrams = seq - start_seq

        # ── Step 4: Heartbeat (own frame, duplicated) ─────────
        uptime_s = now_mono - start_time_monotonic
        adapter_healthy = bool(readings) and any(
            r.get('quality', QUALITY_COMM_FAIL) == QUALITY_GOOD
            for r in readings
        )
        hb_quality = QUALITY_GOOD if adapter_healthy else QUALITY_COMM_FAIL
        hb_records = [(HEARTBEAT_TAG_ID, float(uptime_s), hb_quality)]
        hb_frame = build_batch(seq, timestamp, hb_records, heartbeat=True)
        for _ in range(HEARTBEAT_COPIES):
            sock.sendto(hb_frame, (FPGA_HOST, FPGA_PORT))
            time.sleep(0.005)   # small wire-time gap between copies
        seq += 1

        # ── Log ───────────────────────────────────────────────
        if sent_kind:
            sample = ", ".join(
                f"{r.get('name', '?')}={r['value']:.1f}"
                for r in selected[:3]
            )
            span = (f"seq={start_seq}" if n_datagrams == 1
                    else f"seq={start_seq}..{seq - 1} ({n_datagrams} datagrams)")
            print(f"[TX] {span} | {sent_kind:<8s} | "
                  f"{len(selected)} readings | sample: {sample}")
        else:
            print(f"[TX] no change this poll (heartbeat only)")

        time.sleep(POLL_INTERVAL)


if __name__ == '__main__':
    main()
