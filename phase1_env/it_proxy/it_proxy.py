"""
it_proxy.py — IT-Side Proxy (with reorder tolerance)

The receiver on the corporate/cloud side of the data diode.

What this does:
  1. RECEIVE:     UDP batch datagrams from the FPGA (one per OT poll cycle)
  2. CRC CHECK:   Verify batch integrity (defense in depth — FPGA already checked)
  3. DEDUPLICATE: Same batch SEQ seen again? Keep first, drop copies.
  4. GAP DETECT:  SEQ jumped? Defer the report — wait to see if it arrives late.
  5. EXPLODE:     Unpack every record in the batch (shared timestamp per batch)
  6. DELIVER:     Console + InfluxDB (heartbeat records feed the monitor)

REORDER TOLERANCE:
  UDP doesn't guarantee in-order delivery. Under load, frames arrive
  out of sequence. The naive "see gap → report immediately" approach
  produces FALSE POSITIVES when reordered frames arrive shortly after.

  Fix: maintain a list of "pending gaps" with timestamps. Only confirm
  a gap as real loss after REORDER_TOLERANCE seconds. If the missing
  SEQ arrives before that, cancel the gap (it was just out of order).
"""

import socket
import os
import time
import sys
import logging
from datetime import datetime

sys.path.insert(0, '/app')
from frame import parse_batch

# InfluxDB client — best-effort; if missing or InfluxDB is down, IT proxy
# still works normally (we just skip metrics writes).
try:
    from influxdb_client import InfluxDBClient, Point
    from influxdb_client.client.write_api import ASYNCHRONOUS
    HAS_INFLUX = True
except ImportError:
    HAS_INFLUX = False

# ── Configuration ─────────────────────────────────────────────
LISTEN_HOST = '0.0.0.0'
LISTEN_PORT = int(os.environ.get('IT_LISTEN_PORT', '6000'))
DEDUP_WINDOW = int(os.environ.get('DEDUP_WINDOW', '1000'))

# How long to wait before declaring a missing SEQ truly lost.
# Should be > max expected UDP reorder delay.
# At 100 frames/sec wire rate, copies of one SEQ are ~300ms apart.
# Set to 0.5s to comfortably cover reorder + late copies.
REORDER_TOLERANCE = float(os.environ.get('REORDER_TOLERANCE', '0.5'))

# Heartbeat config — must match OT proxy's HEARTBEAT_TAG_ID.
# Frames with this tag are NOT sensor data — they're "I'm alive" signals.
HEARTBEAT_TAG_ID = 0xFFFF

# Seconds of silence before we declare the OT side dead.
# OT proxy sends a heartbeat frame every poll, duplicated HEARTBEAT_COPIES
# times, so P(all copies of several consecutive heartbeats lost) is tiny.
# 5s = ~5 missed heartbeats is well past coincidence.
HEARTBEAT_TIMEOUT = float(os.environ.get('HEARTBEAT_TIMEOUT', '5.0'))

# ── Dashboard / InfluxDB config ──────────────────────────────
# All optional — if INFLUX_URL is unset or InfluxDB is unreachable,
# the IT proxy logs normally but skips metric writes.
INFLUX_URL    = os.environ.get('INFLUX_URL', '')
INFLUX_ORG    = os.environ.get('INFLUX_ORG', 'diode')
INFLUX_BUCKET = os.environ.get('INFLUX_BUCKET', 'metrics')
INFLUX_TOKEN  = os.environ.get('INFLUX_TOKEN', '')

# Logger used by MetricsPublisher init for clean startup messages
logging.basicConfig(format='[IT-METRICS] %(message)s', level=logging.INFO)
log = logging.getLogger()

# Tag ID → human-readable name
TAG_NAMES = {
    # Legacy Node-RED
    0x0001: 'temperature',
    0x0002: 'pressure',
    0x0003: 'status',
    # HAI dataset (Korea ETRI) — replayed through Modbus PLC
    0x0101: 'P1_FT01_intake_flow_1',
    0x0102: 'P1_FT01Z_intake_flow_1z',
    0x0103: 'P1_FT02_transfer_flow',
    0x0104: 'P1_FT03_output_flow',
    0x0105: 'P1_LIT01_tank_level',
    0x0106: 'P1_LCV01D_lvl_valve_D',
    0x0107: 'P1_LCV01Z_lvl_valve_Z',
    0x0108: 'P1_PIT01_pressure_1',
    0x0109: 'P1_PIT02_pressure_2',
    0x010A: 'P1_PCV01D_press_valve_D',
    0x010B: 'P1_PCV01Z_press_valve_Z',
    0x010C: 'P1_TIT01_temperature_1',
    0x010D: 'P1_TIT02_temperature_2',
    0x010E: 'P1_FCV03D_flow_valve_D',
    0x010F: 'P1_FCV03Z_flow_valve_Z',
}


# ── Deduplication + Gap Detection with Reorder Tolerance ─────
class FrameReceiver:
    """
    Tracks SEQ numbers, detects duplicates, defers gap reports
    so UDP reordering doesn't trigger false-positive losses.
    """

    def __init__(self, window_size: int, reorder_tolerance: float):
        self.seen = set()
        self.window_size = window_size
        self.reorder_tolerance = reorder_tolerance
        self.expected_next = None

        # PENDING gaps: list of (missing_seqs_set, deadline_timestamp)
        # When deadline passes without the SEQs arriving → confirm loss
        self.pending_gaps = []

        # Stats
        self.total_received = 0
        self.total_unique = 0            # monotonic — unlike len(seen), which
                                         # is pruned to the dedup window and
                                         # must NOT be used as a rate denominator
        self.total_duplicates = 0
        self.confirmed_gaps = 0          # truly lost (after tolerance window)
        self.confirmed_lost_frames = 0
        self.reordered_recovered = 0     # arrived late, gap was false alarm
        # Retained for dashboard/InfluxDB schema compatibility. The live path
        # is CRC-only (no in-frame FEC), so these stay 0 unless an RS reference
        # path is ever re-enabled. See frame.py.
        self.rs_corrected_frames = 0
        self.rs_corrected_bytes = 0

    def process(self, seq: int) -> str:
        """
        Returns: 'new', 'duplicate', 'reordered', or 'suspicious'.
        Operates on the batch sequence number (one per datagram sent).
        """
        self.total_received += 1

        # First frame ever
        if self.expected_next is None:
            self.expected_next = seq + 1
            self.seen.add(seq)
            self.total_unique += 1
            return 'new'

        # Check: is this a late arrival of a pending-gap SEQ?
        # (REORDER recovery — cancel the gap report)
        for gap in self.pending_gaps:
            if seq in gap['missing']:
                gap['missing'].discard(seq)
                self.reordered_recovered += 1
                self.seen.add(seq)
                self.total_unique += 1
                return 'reordered'

        # Duplicate detection
        if seq in self.seen:
            self.total_duplicates += 1
            return 'duplicate'

        # Gap detection — SEQ jumped forward
        if seq > self.expected_next:
            gap_size = seq - self.expected_next

            # SEQ BOMB PROTECTION (unchanged from before)
            if gap_size > self.window_size:
                print(f"[ALERT] SEQ jumped from {self.expected_next} to {seq} "
                      f"(gap={gap_size}) — ignoring, possible corruption or restart")
                self.expected_next = seq + 1
                self.seen.clear()
                self.seen.add(seq)
                self.total_unique += 1
                self.pending_gaps.clear()
                return 'suspicious'

            # PENDING gap — don't report yet, wait for tolerance window
            missing_seqs = set(range(self.expected_next, seq))
            self.pending_gaps.append({
                'missing': missing_seqs,
                'deadline': time.time() + self.reorder_tolerance,
                'detected_at': time.time(),
            })

        # Accept this frame
        self.seen.add(seq)
        self.total_unique += 1
        if seq >= self.expected_next:
            self.expected_next = seq + 1

        # Prune dedup set if growing too large
        if len(self.seen) > self.window_size:
            min_keep = max(self.seen) - self.window_size
            self.seen = {s for s in self.seen if s >= min_keep}

        return 'new'

    def expire_pending_gaps(self):
        """
        Call periodically to confirm-or-cancel pending gaps.
        Anything past its deadline that's still missing = real loss.
        """
        now = time.time()
        still_pending = []

        for gap in self.pending_gaps:
            if now >= gap['deadline']:
                # Tolerance window expired
                truly_missing = gap['missing']   # whatever's left
                if truly_missing:
                    delay_ms = (now - gap['detected_at']) * 1000
                    sorted_missing = sorted(truly_missing)
                    self.confirmed_gaps += 1
                    self.confirmed_lost_frames += len(truly_missing)
                    print(f"[LOSS] Confirmed missing SEQ {sorted_missing} "
                          f"({len(truly_missing)} readings, after "
                          f"{delay_ms:.0f}ms tolerance)")
                # gap drops off the list (empty or confirmed)
            else:
                still_pending.append(gap)

        self.pending_gaps = still_pending


# ── Metrics Publisher (InfluxDB) ─────────────────────────────
class MetricsPublisher:
    """
    Pushes IT proxy data to InfluxDB for the Grafana dashboard.

    Three categories of metrics:
      sensor_reading   — per-tag value over time (the actual sensor data)
      diode_stats      — counters for received/dup/reordered/lost
      heartbeat_event  — state transitions + current heartbeat health

    All writes are ASYNCHRONOUS so a slow/down InfluxDB never blocks
    the main loop. The diode pipeline keeps running even if metrics
    can't be published.
    """

    def __init__(self):
        self.enabled = False
        self.client = None
        self.write_api = None

        if not HAS_INFLUX:
            log.warning("influxdb_client not installed — metrics disabled")
            return
        if not INFLUX_URL or not INFLUX_TOKEN:
            log.warning("INFLUX_URL/TOKEN unset — metrics disabled")
            return

        try:
            self.client = InfluxDBClient(
                url=INFLUX_URL,
                token=INFLUX_TOKEN,
                org=INFLUX_ORG,
                timeout=2_000,           # 2 sec — fail fast if InfluxDB hangs
            )
            self.write_api = self.client.write_api(write_options=ASYNCHRONOUS)
            self.enabled = True
            log.info(f"Metrics → {INFLUX_URL} bucket={INFLUX_BUCKET}")
        except Exception as e:
            log.warning(f"InfluxDB init failed: {e} — metrics disabled")

    def write_sensor(self, tag_id: int, tag_name: str, value: float,
                     quality: int, ttl: int):
        """Write one sensor reading point."""
        if not self.enabled:
            return
        try:
            p = (Point("sensor_reading")
                 .tag("tag_id", f"0x{tag_id:04X}")
                 .tag("tag_name", tag_name)
                 .tag("quality", "GOOD" if quality == 0 else "COMM_FAIL")
                 .field("value", float(value))
                 .field("ttl", int(ttl)))
            self.write_api.write(bucket=INFLUX_BUCKET, record=p)
        except Exception:
            pass   # silently drop — don't disrupt main loop

    def write_diode_stats(self, receiver, heartbeat):
        """Periodic counters. Called once every stats interval."""
        if not self.enabled:
            return
        try:
            hb_state_num = {'UNKNOWN': 0, 'HEALTHY': 1, 'SILENT': 2}.get(
                heartbeat.state, 0)
            hb_age = 0.0
            if heartbeat.last_heartbeat_monotonic is not None:
                hb_age = time.monotonic() - heartbeat.last_heartbeat_monotonic

            p = (Point("diode_stats")
                 .field("received",          receiver.total_received)
                 .field("unique",            receiver.total_unique)
                 .field("duplicates",        receiver.total_duplicates)
                 .field("reordered",         receiver.reordered_recovered)
                 .field("confirmed_lost",    receiver.confirmed_lost_frames)
                 .field("pending_gaps",      len(receiver.pending_gaps))
                 .field("rs_corrected_frames", receiver.rs_corrected_frames)
                 .field("rs_corrected_bytes",  receiver.rs_corrected_bytes)
                 .field("heartbeat_state",   hb_state_num)
                 .field("heartbeat_age_s",   float(hb_age))
                 .field("heartbeat_total",   heartbeat.total_heartbeats)
                 .field("silence_events",    heartbeat.silence_events)
                 .field("plc_healthy",       1 if heartbeat.last_quality == 0 else 0))
            self.write_api.write(bucket=INFLUX_BUCKET, record=p)
        except Exception:
            pass

    def write_heartbeat_event(self, event_type: str, message: str):
        """Discrete events (FIRST/RECOVERY/ALARM/RESTART) — for annotations."""
        if not self.enabled:
            return
        try:
            p = (Point("heartbeat_event")
                 .tag("event_type", event_type)
                 .field("message", message)
                 .field("count", 1))
            self.write_api.write(bucket=INFLUX_BUCKET, record=p)
        except Exception:
            pass


# ── Heartbeat Monitor ─────────────────────────────────────────
class HeartbeatMonitor:
    """
    Tracks heartbeat reception from the OT proxy.

    Three states:
      UNKNOWN  — initial, before first heartbeat arrives
      HEALTHY  — receiving heartbeats within timeout
      SILENT   — no heartbeat for HEARTBEAT_TIMEOUT seconds (ALARM)

    Logs only on state TRANSITIONS (not every second) to avoid log spam
    when something stays broken.

    Uses monotonic time so NTP adjustments don't trigger false alarms.
    """

    UNKNOWN  = 'UNKNOWN'
    HEALTHY  = 'HEALTHY'
    SILENT   = 'SILENT'

    def __init__(self, timeout: float, metrics=None):
        self.timeout = timeout
        self.state = self.UNKNOWN
        self.last_heartbeat_monotonic = None     # set on first heartbeat
        self.last_uptime_value = 0.0
        self.last_quality = 0x00
        # Stats
        self.total_heartbeats = 0
        self.silence_events = 0
        self.recovery_events = 0
        self.metrics = metrics    # optional MetricsPublisher

    def on_heartbeat(self, value: float, quality: int):
        """Called when a frame with tag_id=HEARTBEAT_TAG_ID is received."""
        now = time.monotonic()
        prev_state = self.state

        # Detect OT proxy restart: uptime jumps backward
        if (self.last_uptime_value > 0 and
            value < self.last_uptime_value - 5):
            msg = (f"OT proxy restarted (uptime {self.last_uptime_value:.0f}s "
                   f"→ {value:.0f}s)")
            print(f"[HEARTBEAT] {msg}")
            if self.metrics:
                self.metrics.write_heartbeat_event("RESTART", msg)

        self.last_heartbeat_monotonic = now
        self.last_uptime_value = value
        self.last_quality = quality
        self.total_heartbeats += 1

        # Transition: UNKNOWN/SILENT → HEALTHY
        if self.state != self.HEALTHY:
            if self.state == self.SILENT:
                self.recovery_events += 1
                msg = (f"OT side back, uptime={value:.0f}s, "
                       f"quality={'GOOD' if quality == 0 else 'COMM_FAIL'}")
                print(f"[HEARTBEAT] RECOVERY — {msg}")
                if self.metrics:
                    self.metrics.write_heartbeat_event("RECOVERY", msg)
            else:
                # First heartbeat ever
                msg = f"OT side alive, uptime={value:.0f}s"
                print(f"[HEARTBEAT] FIRST contact — {msg}")
                if self.metrics:
                    self.metrics.write_heartbeat_event("FIRST", msg)
            self.state = self.HEALTHY

        # Quality state changed? (PLC went down but OT proxy is alive)
        # Log inline — interesting signal, not state transition.
        # (We keep the heartbeat state HEALTHY because OT proxy itself is fine.)

    def check_timeout(self):
        """Called every iteration. Alarms on transition to SILENT."""
        if self.last_heartbeat_monotonic is None:
            return   # never got first heartbeat yet — don't alarm

        silence = time.monotonic() - self.last_heartbeat_monotonic
        if silence > self.timeout and self.state == self.HEALTHY:
            self.silence_events += 1
            self.state = self.SILENT
            msg = (f"no heartbeat for {silence:.1f}s. "
                   f"OT proxy crashed, network cut, or FPGA failed.")
            print(f"[ALARM] DIODE SILENT — {msg}")
            if self.metrics:
                self.metrics.write_heartbeat_event("ALARM", msg)

    def status_summary(self) -> str:
        """For periodic stats line."""
        if self.last_heartbeat_monotonic is None:
            return "heartbeat=NEVER"
        age = time.monotonic() - self.last_heartbeat_monotonic
        plc = 'PLC_GOOD' if self.last_quality == 0 else 'PLC_FAIL'
        return (f"heartbeat={self.state} age={age:.1f}s "
                f"uptime={self.last_uptime_value:.0f}s {plc} "
                f"recv={self.total_heartbeats} silences={self.silence_events}")


# ── Main Loop ─────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("  IT-SIDE PROXY — Data Diode Receiver (reorder-tolerant)")
    print("=" * 60)
    print(f"  Listening:         UDP {LISTEN_HOST}:{LISTEN_PORT}")
    print(f"  Dedup window:      {DEDUP_WINDOW} frames")
    print(f"  Reorder tolerance: {REORDER_TOLERANCE * 1000:.0f}ms")
    print(f"  Heartbeat timeout: {HEARTBEAT_TIMEOUT}s "
          f"(tag_id=0x{HEARTBEAT_TAG_ID:04X})")
    print("=" * 60)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((LISTEN_HOST, LISTEN_PORT))
    sock.settimeout(0.5)   # short timeout so we can expire gaps regularly

    metrics = MetricsPublisher()
    receiver = FrameReceiver(DEDUP_WINDOW, REORDER_TOLERANCE)
    heartbeat = HeartbeatMonitor(HEARTBEAT_TIMEOUT, metrics=metrics)
    last_stats_time = time.time()

    while True:
        # ── Receive ───────────────────────────────────────────
        try:
            data, addr = sock.recvfrom(2048)
        except socket.timeout:
            # No data — expire pending gaps, check heartbeat, maybe print stats
            receiver.expire_pending_gaps()
            heartbeat.check_timeout()
            now = time.time()
            if now - last_stats_time > 10.0:
                _print_stats(receiver, heartbeat)
                metrics.write_diode_stats(receiver, heartbeat)
                last_stats_time = now
            continue

        # Always check for expired gaps + heartbeat timeout each iteration
        receiver.expire_pending_gaps()
        heartbeat.check_timeout()

        # ── Validate (CRC) ───────────────────────────────────
        # parse_batch() checks SYNC + version + CRC-32. A corrupt or
        # malformed datagram returns None and is dropped. There is no
        # in-frame error correction on the live path — loss recovery is
        # the OT proxy's keyframe refresh, not retransmission or FEC.
        parsed = parse_batch(data)
        if parsed is None:
            print(f"[DROP] Bad CRC / malformed ({len(data)} bytes)")
            continue

        # ── Dedup / reorder / gap detection on batch SEQ ─────
        result = receiver.process(parsed['seq'])
        if result == 'duplicate':
            # e.g. a redundant heartbeat copy — already delivered
            continue

        # 'reordered' arrived late (was a pending gap); 'new'/'suspicious'
        # are fresh. All carry real, CRC-valid data → deliver every record.
        _deliver_batch(parsed, late=(result == 'reordered'),
                       heartbeat=heartbeat, metrics=metrics)

        # Periodic stats
        now = time.time()
        if now - last_stats_time > 10.0:
            _print_stats(receiver, heartbeat)
            metrics.write_diode_stats(receiver, heartbeat)
            last_stats_time = now


def _deliver_batch(parsed, late, heartbeat, metrics):
    """
    Explode a validated batch into its records and deliver each one:
    heartbeat records update the HeartbeatMonitor; sensor records go to
    InfluxDB + console. All records share the batch's timestamp.
    """
    ts_str = datetime.utcfromtimestamp(parsed['timestamp']).strftime('%H:%M:%S')
    seq = parsed['seq']
    kind = 'KEYFRAME' if parsed['keyframe'] else 'delta'
    prefix = '[LATE]' if late else '[DATA]'

    for rec in parsed['records']:
        tag_id = rec['tag_id']
        value = rec['value']
        quality = rec['quality']

        # HEARTBEAT INTERCEPT — not a sensor reading.
        if tag_id == HEARTBEAT_TAG_ID:
            heartbeat.on_heartbeat(value=value, quality=quality)
            continue

        tag_name = TAG_NAMES.get(tag_id, f"tag_{tag_id}")
        quality_str = 'GOOD' if quality == 0 else f"q={quality}"

        # Push to InfluxDB (best-effort, never blocks). ttl has no meaning
        # in the batch format (no per-frame copy number) — pass 0.
        metrics.write_sensor(
            tag_id=tag_id, tag_name=tag_name, value=value,
            quality=quality, ttl=0,
        )

        print(f"{prefix} seq={seq:>6d} | {ts_str} | {kind:<8s} | "
              f"{tag_name:<25s} = {value:>10.2f} | {quality_str}")


def _print_stats(receiver, heartbeat):
    """Print stats with reorder-aware metrics + heartbeat status + RS recoveries."""
    pending = sum(len(g['missing']) for g in receiver.pending_gaps)
    print(f"[STATS] received={receiver.total_received} "
          f"unique={receiver.total_unique} "
          f"duplicates={receiver.total_duplicates} "
          f"reordered={receiver.reordered_recovered} "
          f"confirmed_lost={receiver.confirmed_lost_frames} "
          f"rs_fixed={receiver.rs_corrected_frames} "
          f"pending={pending}")

    # Loss rate uses CONFIRMED losses only. Denominator is the monotonic
    # unique counter — len(seen) is pruned to the dedup window and would
    # overstate the rate ~300x once the window fills.
    total_processed = (receiver.total_unique +
                       receiver.confirmed_lost_frames)
    if total_processed > 0:
        loss_pct = (receiver.confirmed_lost_frames / total_processed) * 100
        print(f"[STATS] confirmed_loss_rate={loss_pct:.4f}% "
              f"(saved by reorder tolerance: {receiver.reordered_recovered})")

    # Heartbeat status (separate line so easy to grep)
    print(f"[STATS] {heartbeat.status_summary()}")


if __name__ == '__main__':
    main()
