"""
fpga_sim.py — FPGA Simulator

Simulates what the real FPGA hardware does:
  1. Receive bytes on INPUT port  (from OT proxy via diode-net)
  2. Find SYNC word (0xAA55)
  3. Read COUNT to compute the (variable) batch length
  4. Verify CRC-32
  5. If CRC good → push frame out OUTPUT port (to IT proxy via it-net)
  6. If CRC bad  → drop silently (no error response — there IS no response path)

What this does NOT do (just like real FPGA):
  - No protocol parsing (doesn't know what's inside the payload)
  - No data transformation
  - No sending back to the OT side — EVER
  - No TCP — only UDP (no handshake = no back-channel)

The simulation also optionally CORRUPTS random frames to exercise the
CRC-reject path (a corrupt frame fails CRC and is dropped — there is no
in-frame error correction; loss recovery lives in the OT proxy's keyframe
refresh).
"""

import socket
import struct
import os
import random
import time
import sys
from collections import deque

# We need the frame parser — copy it or share it
# In Docker, we'll mount frame.py into this container too
sys.path.insert(0, '/app')
from frame import parse_batch, SYNC_WORD

# ── Configuration ─────────────────────────────────────────────
LISTEN_HOST    = '0.0.0.0'
LISTEN_PORT    = int(os.environ.get('FPGA_LISTEN_PORT', '5000'))
OUTPUT_HOST    = os.environ.get('IT_PROXY_HOST', 'it-proxy')
OUTPUT_PORT    = int(os.environ.get('IT_PROXY_PORT', '6000'))

# Simulated error rate — probability of corrupting a frame
# Set to 0 for clean test, 0.05 for 5% corruption to test redundancy
CORRUPT_RATE   = float(os.environ.get('CORRUPT_RATE', '0.0'))

# FIFO buffer depth (max frames to buffer)
FIFO_DEPTH     = int(os.environ.get('FIFO_DEPTH', '64'))


# ── Simulated FIFO ────────────────────────────────────────────
class FIFO:
    """
    Simulates the FPGA's internal block RAM FIFO.

    In real hardware:
    - This is a dual-clock FIFO (input clock domain → output clock domain)
    - Typically 4-8 KB of block RAM
    - Overflow means data loss — no backpressure signal to sender

    In simulation:
    - Simple deque with a max size
    - Overflow drops the oldest frame (tail drop)
    """

    def __init__(self, depth: int):
        self.depth = depth
        self.buffer = deque(maxlen=depth)
        self.overflow_count = 0

    def push(self, frame: bytes) -> bool:
        if len(self.buffer) >= self.depth:
            self.overflow_count += 1
            return False   # overflow — frame lost
        self.buffer.append(frame)
        return True

    def pop(self) -> bytes | None:
        if self.buffer:
            return self.buffer.popleft()
        return None

    def size(self) -> int:
        return len(self.buffer)


# ── Corruption Simulator ─────────────────────────────────────
def maybe_corrupt(data: bytes) -> tuple[bytes, bool]:
    """
    Simulate bit errors from electrical noise.
    Flips a random byte in the frame to trigger CRC failure.
    """
    if random.random() < CORRUPT_RATE:
        corrupted = bytearray(data)
        pos = random.randint(0, len(corrupted) - 1)
        corrupted[pos] ^= 0xFF   # flip all bits of one byte
        return bytes(corrupted), True
    return data, False


# ── Main Loop ─────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("  FPGA SIMULATOR — Unidirectional Gate")
    print("=" * 60)
    print(f"  Input:         UDP {LISTEN_HOST}:{LISTEN_PORT} (from OT proxy)")
    print(f"  Output:        UDP {OUTPUT_HOST}:{OUTPUT_PORT} (to IT proxy)")
    print(f"  FIFO depth:    {FIFO_DEPTH} frames")
    print(f"  Corrupt rate:  {CORRUPT_RATE * 100:.1f}%")
    print(f"  Direction:     INPUT → OUTPUT only (no return path)")
    print("=" * 60)

    # INPUT socket — receives from OT proxy
    rx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx_sock.bind((LISTEN_HOST, LISTEN_PORT))
    rx_sock.settimeout(0.1)  # non-blocking-ish for stats printing

    # OUTPUT socket — sends to IT proxy (NEVER sends back to OT side)
    tx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    fifo = FIFO(FIFO_DEPTH)

    # Counters
    stats = {
        'rx_total': 0,        # total frames received
        'rx_corrupted': 0,    # frames we simulated corruption on
        'crc_pass': 0,        # frames that passed CRC
        'crc_fail': 0,        # frames dropped due to bad CRC
        'tx_total': 0,        # frames forwarded to IT side
        'fifo_overflow': 0,   # frames lost to FIFO overflow
    }
    last_stats_time = time.time()

    while True:
        # ── RECEIVE from OT proxy ─────────────────────────────
        try:
            data, addr = rx_sock.recvfrom(2048)
            stats['rx_total'] += 1
        except socket.timeout:
            # No data — print stats periodically
            if time.time() - last_stats_time > 5.0:
                if stats['rx_total'] > 0:
                    print(f"[FPGA] rx={stats['rx_total']} "
                          f"crc_pass={stats['crc_pass']} "
                          f"crc_fail={stats['crc_fail']} "
                          f"tx={stats['tx_total']} "
                          f"fifo_overflow={stats['fifo_overflow']} "
                          f"corrupted_sim={stats['rx_corrupted']}")
                last_stats_time = time.time()
            continue

        # ── SIMULATE BIT ERRORS (noise on the wire) ───────────
        data, was_corrupted = maybe_corrupt(data)
        if was_corrupted:
            stats['rx_corrupted'] += 1

        # ── CRC CHECK (this is what FPGA does in hardware) ────
        # The FPGA doesn't "parse" the frame in a software sense.
        # It finds SYNC, reads COUNT to compute the frame length, computes
        # CRC-32 over the right bytes, and compares. It never inspects the
        # records — it's protocol-agnostic. That's ~50 lines of Verilog.
        # Any corruption fails CRC and is dropped (no error correction here).
        parsed = parse_batch(data)

        if parsed is None:
            # CRC failed or malformed — drop silently
            # In real FPGA: frame bits just don't make it to the output FIFO
            stats['crc_fail'] += 1
            continue

        stats['crc_pass'] += 1

        # ── FIFO BUFFER ──────────────────────────────────────
        if not fifo.push(parsed['raw']):
            stats['fifo_overflow'] += 1
            continue

        # ── TRANSMIT to IT proxy (output side only) ──────────
        frame_out = fifo.pop()
        if frame_out:
            tx_sock.sendto(frame_out, (OUTPUT_HOST, OUTPUT_PORT))
            stats['tx_total'] += 1


if __name__ == '__main__':
    main()
