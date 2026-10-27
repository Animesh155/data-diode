# 🔒 Hardware Data Diode for Industrial Security

## The Problem

Factories, power plants, and water treatment facilities run on old industrial computers called **PLCs** (Programmable Logic Controllers). These machines control pumps, valves, and motors — things that can cause real physical damage if hacked.

Engineers need to **monitor** these systems from their office network (IT side) — see temperatures, pressures, flow rates on dashboards. But the moment you connect the factory network (OT side) to the office network, you create a door that hackers can walk through **backwards** to attack the factory.

**Firewalls try to block this, but they're software — and software has bugs.** One misconfiguration, one vulnerability, and an attacker on the office network can send commands to a PLC and cause a disaster. This is exactly how attacks like Stuxnet worked.

## The Solution: A One-Way Wire

A **data diode** is a device that makes reverse traffic **physically impossible**. Not blocked by rules. Not filtered by software. **Physically impossible** — like a one-way mirror for network data.

We use fibre optics: the factory side has a **laser transmitter** (TX). The office side has a **light receiver** (RX). There is **no laser on the office side** and **no receiver on the factory side**. You can't send data back through a wire that literally doesn't exist.

```
  FACTORY (OT)                    OFFICE (IT)
  ════════════                    ══════════
                                  
  ┌─────────┐     ┌──────────┐     ┌──────────┐     ┌─────────┐
  │   PLC   │────▶│ OT Proxy │────▶│   FPGA   │────▶│IT Proxy │────▶ Dashboard
  │ Sensors │     │ (reads   │     │ (checks  │     │(stores  │     (Grafana)
  │ & Data  │     │  & packs)│     │  & gates)│     │ & shows)│
  └─────────┘     └──────────┘     └──────────┘     └─────────┘
                                  
  ◀══════════ IMPOSSIBLE ══════════▶
       No reverse path exists.
       No ACKs. No TCP. No way back.
```

## How Data Flows (Step by Step)

### 1. Read the sensors
The **OT Proxy** talks to PLCs using industrial protocols (Modbus, S7comm, OPC UA). Every second, it asks: *"What's the tank level? What's the pressure? What's the flow rate?"*

### 2. Pack into a simple format
Readings get packed into a small binary packet — just the essentials:

| Field | Size | What it is |
|-------|------|------------|
| SYNC | 2 bytes | Magic marker `0xAA55` — "a frame starts here" |
| VERSION | 1 byte | Format version (`0x02`) |
| FLAGS | 1 byte | Is this a keyframe? A heartbeat? |
| SEQ | 4 bytes | Sequence number — catch lost packets |
| TIMESTAMP | 4 bytes | When this was read |
| COUNT | 2 bytes | How many readings follow |
| RECORDS | 7 bytes each | Tag ID + value + quality per sensor |
| CRC-32 | 4 bytes | Checksum — detect corruption |

A typical packet with 15 sensors = **123 bytes**. Tiny.

### 3. Fire across the diode (UDP, one-way)
The packet is sent over UDP (no handshakes, no ACKs needed — because **there's no way to send an ACK back anyway**). It crosses the one-way fibre link into the FPGA.

### 4. FPGA validates in hardware
The FPGA chip is the **gatekeeper**. It runs at wire speed and does three checks:

- **Is the frame well-formed?** Valid sync, correct version, sane record count
- **Is the checksum correct?** CRC-32 must match — any corruption = drop
- **Are the values legitimate?** Each sensor reading is bounds-checked against a whitelist — if a temperature claims to be 99,999°C, that's physically impossible and gets rejected

If a frame fails **any** check → **silently dropped**. No error message (there's no way to send one). Good frames get forwarded. Bad frames vanish.

### 5. IT Proxy receives, deduplicates, stores
On the office side, the **IT Proxy**:
- Verifies the CRC again (defense in depth)
- Removes duplicate packets (UDP can deliver copies)
- Handles out-of-order arrivals (waits 500ms before calling a gap "real loss")
- Monitors heartbeats — if the factory side goes silent for 5 seconds, it raises an alarm
- Writes everything to **InfluxDB** for time-series storage

### 6. See it on a dashboard
**Grafana** displays live sensor charts, packet loss counters, and link health — the factory's vital signs, visible from the office, with zero risk of sending anything back.

## But What If Packets Get Lost?

UDP doesn't guarantee delivery. Packets can get lost or arrive out of order. Without a back-channel, we can't do retransmissions. So we use two tricks:

1. **Keyframe refresh every 5 seconds** — the OT proxy sends ALL current sensor values, not just the ones that changed. Even if you miss a delta, the full picture arrives within 5 seconds. Self-healing.

2. **Heartbeat frames (3 copies)** — a tiny "I'm alive" message sent every second, duplicated 3 times. The IT side knows the link is healthy even during data gaps.

**Result:** 352,512 frames received, **0 confirmed lost** (0.0000% loss rate). 3,523 attack packets fired during testing, **0 reached the IT side** (100% block rate).

## What's in This Repo

```
├── phase1_env/                     # Full simulation (runs with Docker)
│   ├── docker-compose.yml          #   Spins up the entire pipeline
│   ├── ot_simulators/              #   Fake PLCs replaying real industrial data
│   ├── ot_proxy/                   #   Reads PLCs, packs frames, sends one-way
│   ├── fpga_sim/                   #   Software model of the FPGA gatekeeper
│   ├── it_proxy/                   #   Receives, deduplicates, stores to InfluxDB
│   ├── attack_replayer/            #   Throws real attack traffic at the diode
│   └── dashboard/                  #   Grafana dashboards
│
├── fpga/                           # Real hardware (Verilog RTL)
│   ├── rtl/
│   │   ├── frame_validator.v       #   Streaming FSM — validates every byte
│   │   ├── crc32.v                 #   Hardware CRC-32 engine (1 byte/clock)
│   │   ├── tag_policy.v            #   Per-tag value bounds checker
│   │   └── fifo.v                  #   Buffer with tail-drop (no backpressure)
│   ├── tb/                         #   Testbenches & test vectors
│   └── Makefile                    #   Build with Icarus Verilog
│
├── osdd-gateway/                   # Next-gen protocol gateway
│   ├── src/gateway/
│   │   ├── drivers/                #   Modbus TCP, MQTT, OPC UA adapters
│   │   ├── core/                   #   Polling engine, filter chain, health
│   │   └── transport/              #   Batched UDP sender, rate limiter
│   ├── config/                     #   YAML configs with JSON schema validation
│   └── tests/                      #   Driver simulators and tests
│
└── lab-ot-security/                # Teaching lab (5 hands-on modules)
    └── (reconnaissance, Modbus, exploitation, forensics, multi-protocol)
```

## Try It Yourself

### Start the simulation
```bash
cd phase1_env
docker compose up -d
```

### Open the dashboard
```
http://localhost:3000/d/diode-live
```

### Watch data flow
```bash
docker logs -f it-proxy      # see frames arriving on the IT side
```

### Simulate a failure
```bash
docker compose stop plc-modbus   # kill a PLC — watch quality degrade
docker compose stop ot-proxy     # kill the OT proxy — heartbeat alarm fires in 5s
docker compose start ot-proxy plc-modbus   # recover — watch self-healing
```

### Run attack traffic
```bash
docker compose --profile attack up -d attack-replayer
docker logs -f attack-replayer    # 3,523 attack packets → 0 get through
```

### Shut down
```bash
docker compose down -v
```

## The FPGA: Why Hardware Matters

The Python `fpga_sim` proves the logic works. But in production, this runs on an **actual FPGA chip** (target: Xilinx Artix-7 on ALINX AX7102 board). Why?

- **Software can be tricked.** Buffer overflows, logic bugs, timing attacks — software validators have attack surface. An FPGA is a fixed circuit. It doesn't run an OS. It doesn't have a shell. It can't be "hacked into" — it literally just does XOR gates and comparators at wire speed.

- **The fibre optic link is the real diode.** The FPGA board has SFP+ ports for fibre. We connect **only the TX strand** on the output side. No RX fibre = no reverse path. An optical power meter proves zero reverse light. **This is the physics that makes it a real data diode, not just a fancy firewall.**

## Hardware Plan

| Component | What | Why |
|-----------|------|-----|
| **Board** | ALINX AX7102 (Artix-7 XC7A100T) | 2× SFP+ ports, free Vivado toolchain |
| **Input** | RJ45 Gigabit Ethernet | OT host connects via copper |
| **Output** | SFP+ fibre (TX strand only) | Physical one-way — no RX strand |
| **Proof** | Optical power meter | Measures zero reverse light |

## Key Design Decisions

| Decision | Choice | Why |
|----------|--------|-----|
| Transport | UDP only | No handshakes = no back-channel needed |
| Loss recovery | Keyframe every 5s | Self-heals without retransmissions |
| Integrity | CRC-32 | Dominant failure is packet loss, not corruption |
| Protocol handling | Terminate → extract → re-originate | The original PLC protocol dies at the OT proxy; only readings survive |
| FPGA overflow | Tail-drop (silent) | No reverse channel = no backpressure possible |
| Value validation | Hardware bounds + tag whitelist | Prevents covert channel data exfiltration |

## What Makes This Different from Commercial Diodes

| | Owl/Waterfall (commercial) | OSDD (open source) | This Project |
|---|---|---|---|
| Price | \$6k–\$50k+ | Free | Free |
| FPGA validation | CRC only | Minimal (no reverse wire = 1-page proof) | CRC + tag whitelist + value bounds |
| Protocol support | 100+ connectors | Kafka/NATS | Modbus, S7, OPC UA, MQTT |
| Open source | No | Yes (Apache-2.0) | Yes |
| Covert channel defense | Not disclosed | Not addressed | Float bounds + NaN/Inf rejection |

## Tech Stack

- **Languages:** Python 3.11, Verilog (IEEE 1364-2005)
- **Simulation:** Docker Compose, 3 isolated networks
- **FPGA toolchain:** Icarus Verilog (sim), Vivado (synthesis)
- **Protocols:** Modbus TCP, Siemens S7comm, OPC UA, MQTT
- **Storage:** InfluxDB 2.7
- **Dashboard:** Grafana
- **Target hardware:** Xilinx Artix-7 (ALINX AX7102)
- **Data sources:** HAI 21.03 (Korea ETRI), Edge-IIoTset attack PCAPs

## Stats

```
Frames received:         352,512
Confirmed lost:          0  (0.0000%)
Attack packets blocked:  3,523 / 3,523  (100%)
Continuous uptime:       2+ hours
Self-heal time:          ≤ 5 seconds (keyframe interval)
```

## Citations

- **HAI Dataset:** Shin et al., "HAI 1.0: HIL-based Augmented ICS Security Dataset" (2020), ETRI Korea
- **Edge-IIoTset:** Ferrag et al., "Edge-IIoTset: A New Comprehensive Realistic Cyber Security Dataset" IEEE Access (2022)
- **DNV Paper:** "Reevaluating the Role of Data Diodes" (CEPSI 2025)
