# Data Diode — Phase 1 Status

**Status:** ✅ Complete (Software simulation)
**Date:** End of Phase 1
**Next:** Phase 2 — Analytics (anomaly / attack detection)

---

## What This Is

A software simulation of a hardware data diode — a one-way security gateway for industrial OT networks. Built end-to-end in Docker as a proof-of-architecture before moving to real FPGA hardware.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  WATER TREATMENT PLANT (simulated, HAI dataset replay)              │
│                                                                     │
│   Modbus TCP PLC                                                    │
│   15 sensors from HAI 21.03 (Korea ETRI testbed)                    │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ Modbus TCP (port 502, ot-net)
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│  OT-SIDE PROXY                                                      │
│  1. Modbus adapter polls PLC every 1 sec                            │
│  2. Normalizes to universal 11-byte struct                          │
│  3. Wraps in 24-byte frame (SYNC + SEQ + LEN + TTL + payload + CRC) │
│  4. Generates 3 copies per reading + 1 heartbeat                    │
│  5. Interleaves copies across time                                  │
│  6. Sends UDP to FPGA (no response expected)                        │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ ~32 Kbps UDP (diode-net)
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│  FPGA SIMULATOR (one-way enforcement)                               │
│  Receives → Validates CRC → FIFO → Forwards to IT side              │
│  NEVER sends back to OT (architectural one-way)                     │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ UDP (it-net)
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│  IT-SIDE PROXY                                                      │
│  1. CRC re-check (defense in depth)                                 │
│  2. Deduplicate (3 copies → 1)                                      │
│  3. Reorder tolerance (500ms window)                                │
│  4. Gap detection (with SEQ bomb protection)                        │
│  5. Heartbeat monitor (silent death detection)                      │
│  6. Push to InfluxDB                                                │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
                           ▼
                  ┌──────────────────┐
                  │   InfluxDB +     │  Grafana dashboard at
                  │   Grafana        │  http://localhost:3000
                  └──────────────────┘
```

## What's Built

### Core Pipeline
- ✅ Custom 24-byte binary wire format
- ✅ Universal 11-byte sensor struct (timestamp/tag_id/value/quality)
- ✅ Multi-protocol adapter architecture (Modbus implemented)
- ✅ 3-copy redundancy with lane-based interleaving
- ✅ CRC-32 integrity validation
- ✅ Docker network isolation (3 internal networks)

### Data Sources
- ✅ HAI 21.03 (Korea ETRI water+power testbed) — currently replayed
- ✅ Edge-IIoTset PCAPs (downloaded, used for attack testing)
- ⚠️  SWaT.A9 Nov 2022 (downloaded, not integrated yet)
- ⚠️  iTrust IoT dataset (downloaded, not yet used)

### Protections
- ✅ CRC validation rejects malformed frames at FPGA gateway
- ✅ Reorder tolerance prevents false-gap alarms from UDP reordering
- ✅ SEQ bomb protection prevents DoS via crafted sequence numbers
- ✅ Heartbeat detection catches OT-side failures (silent death problem)
- ✅ Distinguishes "PLC down" from "OT proxy crashed" via quality field

### Security Validation
- ✅ Attack replayer using Edge-IIoTset PCAPs
- ✅ Tested attacks: MITM (ARP+DNS), OS Fingerprinting, Port Scanning
- ✅ Result: 3,523 attack packets fired, **0** reached IT side (100% block rate)
- ✅ Normal traffic continued uninterrupted during attacks

### Observability
- ✅ InfluxDB time-series store
- ✅ Grafana dashboard with 9 panels
- ✅ Heartbeat state indicator (green/yellow/red)
- ✅ PLC health indicator
- ✅ Frame counters (received / lost / reordered)
- ✅ Per-sensor time-series charts (grouped by signal type)
- ✅ Heartbeat event log (FIRST / ALARM / RECOVERY / RESTART)

## File Structure

```
phase1_env/
├── docker-compose.yml          # 8 services across 3 isolated networks
├── ot_simulators/
│   └── modbus_plc/             # pymodbus PLC replaying HAI data
│       ├── Dockerfile
│       ├── modbus_server.py
│       ├── hai_replay.py       # HAI CSV → Modbus register values
│       └── sensors.py          # original synthetic models (unused now)
├── ot_proxy/
│   ├── Dockerfile
│   ├── ot_proxy.py             # main loop + heartbeat sender
│   ├── frame.py                # universal frame format
│   └── adapters/
│       └── modbus_adapter.py   # Modbus → universal struct
├── fpga_sim/
│   └── fpga_sim.py             # CRC validation + FIFO + forward
├── it_proxy/
│   ├── Dockerfile
│   ├── it_proxy.py             # dedup + reorder + heartbeat + InfluxDB
│   ├── frame.py
│   └── requirements.txt        # influxdb-client
├── attack_replayer/            # On-demand PCAP replay (profile=attack)
│   ├── Dockerfile
│   └── replayer.py
├── dashboard/
│   └── grafana/
│       ├── provisioning/       # Datasource + dashboard auto-config
│       └── dashboards/
│           └── diode.json      # The dashboard definition
├── hai/                        # HAI dataset (Korea ETRI)
└── datasets/
    └── edge_iiotset/           # Attack PCAPs (12 GB, gitignored)
```

## How To Run

### Start everything
```bash
cd phase1_env
docker compose up -d
```

### Open the dashboard
```
http://localhost:3000/d/diode-live
(admin/admin, or just view as anonymous)
```

### Trigger an attack (separate command)
```bash
docker compose --profile attack up -d attack-replayer
docker logs -f attack-replayer
```

### Watch heartbeat scenarios
```bash
# Healthy state
docker logs -f it-proxy

# Kill the PLC (sensor data fails, OT proxy still alive)
docker compose stop plc-modbus

# Kill the OT proxy (heartbeat goes silent, alarm fires in 5s)
docker compose stop ot-proxy

# Recovery
docker compose start ot-proxy plc-modbus
```

### Stop everything (preserves data)
```bash
docker compose stop
```

### Tear down (deletes Docker state)
```bash
docker compose down -v   # -v removes named volumes
```

## Key Design Decisions

| Decision | Choice | Why |
|----------|--------|-----|
| Wire format | v2 batch: all readings from one poll in one datagram | Amortizes header+CRC over every reading; fewer packets = less loss |
| Loss recovery | Keyframe refresh + send-on-change | No back-channel needed; a lost delta self-heals at the next full refresh |
| Redundancy | Heartbeat frame only (duplicated) | Bulk data relies on refresh; only the tiny can't-miss frame is copied |
| Integrity | CRC-32 (no in-frame FEC on live path) | On this UDP link loss dominates corruption; RS retained as FPGA reference only |
| Protocol break | Adapter pattern | FPGA stays protocol-agnostic |
| Frame size | Variable batch (14B header + 7B/record + 4B CRC) | One poll = one datagram |
| Transport | UDP | Matches one-way nature (no ACKs) |
| Heartbeat | Same frame, tag 0xFFFF | No format change needed |
| Reorder tolerance | 500ms window | > 3× our 150ms copy spacing |
| Diode enforcement | Docker network isolation | Software-enforced in sim |
| Time source | `time.monotonic()` | Immune to NTP / clock jumps |
| Storage | InfluxDB 2.7 | Time-series standard, Grafana-native |
| Dashboard | Grafana | Industry standard for ops dashboards |

## Bugs Found & Fixed During Phase 1

| Bug | Symptom | Fix |
|-----|---------|-----|
| SEQ bomb DoS | Single crafted UDP frame could OOM the IT proxy | Bounded gap handling, reset state on huge jumps |
| pymodbus offset | Modbus reads returned shifted data | Write to register offset 1 (pymodbus quirk) |
| UDP reorder false gaps | ~7% loss reported, actual ~0% | 500ms tolerance window before declaring loss |
| Silent death undetectable | Whole pipeline could die unnoticed | Heartbeat frames + 5s timeout alarm |
| Internal network couldn't pip install | OT proxy broke on Modbus deps | Dockerfile with pre-installed packages |

## Open Issues (Carried Into Phase 2)

- ⚠️ Per-tag staleness detection (one sensor dying while pipeline stays up)
- ⚠️ No actual anomaly detection on sensor values (just transport health)
- ⚠️ Single-threaded FPGA sim can't realistically test FIFO overflow
- ⚠️ Code-enforced one-way (not physics-enforced) — only real FPGA fixes this
- ⚠️ Only one PLC (multi-PLC scenarios untested)
- ⚠️ Dashboard hardcoded to HAI P1_* tags (would need rework for SWaT)

## What's NOT Built (And Why)

| Not Built | Why Deferred |
|-----------|--------------|
| OPC UA adapter | Kepware does this in production deployments |
| MQTT adapter | Lower priority than diode-specific features |
| In-frame Reed-Solomon FEC (live path) | Wrong tool here — intra-frame RS fixes byte corruption, not the packet loss that dominates a UDP diode link. Kept in frame.py as the bit-accurate FPGA reference model for the eventual raw-optical PHY. |
| ML anomaly detection | Phase 2 work |
| Verilog port | Phase 3+ (real hardware path) |
| SWaT integration | Designed but not implemented |
| Per-sensor bounds checks | Phase 2 work |
| Multi-PLC support | Single PLC validates the architecture |

## Stats (Last Known State)

```
Frames received:    352,512
Confirmed lost:     0
Loss rate:          0.0000%
Attack packets blocked: 3,523 / 3,523 (100%)
Uptime in last test: 2+ hours continuous
Heartbeat events: state transitions tracked in InfluxDB
```

## Citations & Data Sources

- **HAI Dataset**: Shin et al., "HAI 1.0: HIL-based Augmented ICS Security Dataset" (2020), ETRI Korea — https://github.com/icsdataset/hai
- **Edge-IIoTset**: Ferrag et al., "Edge-IIoTset: A New Comprehensive Realistic Cyber Security Dataset" IEEE Access (2022)
- **SWaT.A9**: Umer et al., "Attack pattern mining to discover hidden threats to ICS" Int. J. Inf. Secur. (2026)

## Next Phase

See PHASE2_PLAN.md (TBD) for analytics direction.
