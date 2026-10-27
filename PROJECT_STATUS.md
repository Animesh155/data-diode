# Data Diode — Project Status

**Last updated:** 2026-07-29
**Milestone:** investor / customer demo
**Product base (decided):** `osdd-gateway`
**Critical path:** FPGA Ethernet MAC integration — blocked on hardware

---

## 1. What this is

A one-way security gateway for industrial OT networks. OT telemetry flows out to
IT for monitoring; nothing can flow back. Built as a working software pipeline
plus an FPGA "pump" that will eventually enforce one-wayness in physics rather
than configuration.

```
 PLC ──Modbus/S7/OPC UA──► OT PROXY ──UDP──► FPGA PUMP ──one-way──► IT PROXY ──► historian
      (two-way, local)     protocol break    crc + policy            dedup/gap
                           batch + keyframe  + fifo                  + heartbeat
```

The two-way part of every protocol happens on the OT side of the protocol break.
Only read-only tag values cross, in a format that structurally cannot express a
command.

---

## 2. Communication layer — DONE and verified

### 2.1 The redesign

The original design sent **3 copies of every single reading** as separate frames.
It was replaced with **batching + keyframe refresh**:

| | Before | Now |
|---|---|---|
| Unit on the wire | 3 frames, 1 reading each | 1 batch = all readings, sent once |
| Reliability from | spatial redundancy (3 copies) | temporal redundancy (keyframe every 5s + send-on-change) |
| Redundancy kept | everything ×3 | **heartbeat only ×3** |
| Bandwidth | ~35 Kbps | **~3.5 Kbps (~10× less)** |
| Packets/sec | 48 | ~4 |

**Why it holds the same reliability:** SCADA tags are *state*, not events. A lost
delta is corrected by the next full keyframe within 5s. Copies were traded for
repeated, decorrelated sends over time — which survives burst loss better than
3 clustered copies did. The heartbeat stays ×3 because a missed heartbeat is a
point-event that cannot be recovered and would cause a false "diode dead" alarm.

**Reed-Solomon FEC was removed from the live path.** Intra-frame RS corrects byte
corruption but does nothing for whole-packet loss, which is the dominant failure
on a UDP link. It is retained in `frame.py` as a labelled bit-accurate reference
model for a future raw-optical PHY.

### 2.2 Wire format v2 (frozen, spec'd)

Full spec: [`phase1_env/docs/WIRE_FORMAT.md`](phase1_env/docs/WIRE_FORMAT.md)

```
 off  size  field
  0    2    SYNC       0xAA55            (not covered by CRC)
  2    1    VERSION    0x02
  3    1    FLAGS      bit0 KEYFRAME, bit1 HEARTBEAT
  4    4    SEQ        uint32
  8    4    TIMESTAMP  uint32, shared by all records
 12    2    COUNT      uint16
 14   7*N   RECORDS    tag_id(2) value(float32) quality(1)
14+7N  4    CRC-32     over bytes [2 .. 14+7N)
```

- 15 sensors = **123 bytes** for a whole poll cycle
- **CRC-32/ISO-HDLC** pinned: poly `0x04C11DB7`, init/xorout `0xFFFFFFFF`,
  refin/refout true, check `0xCBF43926`
- **MTU-safe:** max 200 records/datagram; larger keyframes split across
  datagrams, each self-contained
- `VERSION` byte + 6 spare `FLAGS` bits give a clean forward-compat path

### 2.3 Verified

- 35/35 round-trip tests (build/parse, corruption rejection, crafted-COUNT
  guard, send-on-change, dedup/gap/reorder, split keyframes)
- **Live in Docker for 5+ days continuous** — 521k frames through the FPGA sim,
  100% CRC pass, 0 FIFO overflow, heartbeat healthy
- **Verified under 5% injected loss:** effective data loss stayed ~0.5% (not 5%)
  because keyframes repair lost deltas; heartbeat never blipped

---

## 3. Protocol adapters — 4/4 working, frozen by decision

| Protocol | Library | Status |
|---|---|---|
| Modbus TCP | `pymodbus` | ✅ live in the running stack |
| S7comm | `snap7` | ✅ integration-tested against `plc-s7` |
| OPC UA | `asyncua` | ✅ **fixed** (see below) |
| MQTT | `paho` | ✅ 34/34 unit tests |

**OPC UA fix:** two bugs, one each side. The adapter resolved tags by browse
path; it now resolves by **string NodeId** (`ns=2;s=<tag>`) — the stable handle
real servers (Kepware/PLC4X) publish — plus a probe read at connect so a wrong
address space fails fast. The simulator's `add_variable` used a bare-string
browse name that defaulted to ns=0 while its NodeId was ns=2; now qualified.
This also repaired a lab inconsistency — `lab_protocols/ANSWER_KEY.md` documents
`ns=2` browse output, which the server had not been producing.

**Strategic decision:** do not compete with Kepware on protocol breadth. Sit
*downstream* of a normalizer (Kepware / Apache PLC4X / Telegraf) and consume one
interface. The adapters' job is the **protocol break**, not protocol coverage —
a different component doing a job Kepware doesn't do at all.

---

## 4. FPGA RTL — 74/74 passing, board-independent work complete

```
fpga/
├── rtl/crc32.v            CRC-32/ISO-HDLC, 1 byte/clock
├── rtl/frame_validator.v  SYNC → header → COUNT bounds → records → CRC → verdict
├── rtl/tag_policy.v       per-tag range / NaN / whitelist checks
├── rtl/fifo.v             tail-drop buffer (see gap in §7)
├── tb/                    3 testbenches + generated vectors
├── gen_vectors.py         golden vectors + policy table, from frame.py
└── Makefile               `make` runs everything
```

| Testbench | Checks | Status |
|---|---|---|
| `tb_crc32` | 7 vectors | ✅ |
| `tb_frame_validator` | 20 vectors | ✅ |
| `tb_fifo` | 47 checks | ✅ |

### 4.1 Why the tests have teeth

Vectors are generated from `frame.py`, so **Python is the single source of truth**
and hardware/software cannot drift. Vector 0 is the canonical `0xCBF43926` check
value — it proves genuine CRC-32/ISO-HDLC, not a lookalike.

Both directions are covered:

- **Accept:** keyframe, delta, heartbeat, empty, max-200-record, boundary values
- **Drop:** corrupt payload, corrupt CRC, bad VERSION, crafted COUNT=999,
  out-of-range value, NaN, Inf, unknown tag, mixed good+bad
- **Stream:** truncated → *no verdict at all* (won't false-accept), resync after
  garbage, two frames back-to-back

Vectors 0–4 fail if the design rejects everything; 5–9 fail if it accepts
everything. The suite genuinely discriminates.

**Mutation-tested.** Breaking tail-drop → 2 failures. Bypassing the policy check
→ exactly 6 failures, all semantic vectors. A test that cannot fail is worse
than no test.

### 4.2 What the FPGA enforces

**Structural (well-formed):** SYNC, VERSION==0x02, COUNT ≤ 200, CRC-32.

**Semantic (well-behaved)** — added because the `value` field was otherwise 4
arbitrary bytes the hardware never questioned (~480 bits/sec of covert channel):

1. NaN/Infinity rejected (exponent all-ones)
2. **Unknown tag_id rejected** — whitelist
3. Value outside the tag's engineering limits rejected

Float comparison needs no FPU: IEEE-754 maps monotonically to unsigned by
setting the MSB for positives and inverting for negatives.

*Honest limit:* range checking **reduces** the covert channel, it does not close
it. Elimination needs quantisation at the source plus validation here.

### 4.3 Diode-specific design decisions in RTL

- **Tail-drop, never backpressure.** A diode has no reverse channel, so "slow
  down" cannot be signalled. On overflow the write is dropped and counted;
  losses self-heal via keyframe refresh.
- **Protocol-blind.** The FPGA never interprets records. An OT-side protocol
  exploit has nothing to land on in hardware.
- **Store-and-forward is mandatory** — you cannot retract bytes that have crossed
  a one-way barrier, so cut-through forwarding is not an option.

---

## 5. Competitive / reference research

| Project | Hardware | Across the diode | Protocols | Notes |
|---|---|---|---|---|
| **OSDD** (Dutch gov, Apache-2.0) | Intel MAX 10 | UDP, 9-byte header | Kafka, UDP only | Datapath is **8 lines of VHDL** — pure direction enforcement, no validation. Dormant since Nov 2022 |
| **Owl** | Two electrical diodes | **Non-routable ATM** | Modbus, OPC, files | Entry level **under $6,000**, Common Criteria/NERC CIP accredited |
| **Waterfall** | Laser → photocell | Proprietary | **Hundreds of connectors** | Sells on **NERC CIP-005 granting 37 requirement exemptions** — regulatory relief, not security, is the ROI |
| **Us** | FPGA validator + optics (pending) | UDP + our batch format | Modbus, S7, OPC UA, MQTT | Validation in hardware; open, auditable |

**Key findings:**

- **OSDD independently arrived at heartbeats too** — convergent validation. They
  have *no* loss recovery, using backpressure instead, because Kafka messages are
  *events* (not re-sendable) whereas our SCADA tags are *state*. Same problem,
  opposite correct solutions.
- **OSDD's FPGA does far less than ours** — deliberately. Simpler to certify
  ("there is no reverse wire" is one page). Ours filters more. Both defensible;
  be able to argue which and why.
- Their hardware (block design, Quartus project, VHDL) is Apache-2.0 and
  **legally reusable** — their RGMII/DDIO and MDIO code is a proven reference.
- **Correction to earlier assumption:** incumbents are *not* $50k+. Owl starts
  under $6k, accredited, 10-yr MTBF. "Cheaper than incumbents" is a weak wedge.
- Owl and Waterfall both cover OT protocols, so protocol coverage differentiates
  only against OSDD.

**Papers reviewed:** DNV "Reevaluating the Role of Data Diodes" (CEPSI 2025) —
argues diodes are mandated by regulation and often misapplied. We are the
*legitimate* case it doesn't dispute: egress telemetry with a real protocol
break. Its sharpest point stands though: **a diode that isn't physics-enforced
is just a firewall.**

---

## 6. Hardware plan

**Selection logic, ranked:**

1. **Can it do physical one-way?** Gigabit copper *cannot* (all 4 pairs
   bidirectional). 100BASE-TX can (cut RX pair). **Fibre via SFP is cleanest** —
   separate strands, and an optical power meter *proves* zero reverse light.
2. **Does a proven MAC exist for that board?** Biggest schedule risk. Our logic
   is ~700 LUTs; a GbE MAC is ~2–3k LUTs plus months if self-written.
3. **PHY interface** — RGMII needs ~2ns skew handling that fails silently.
4. **Toolchain licensing** — Vivado free covers Artix-7/Spartan-7; Kintex-7 and
   above need paid licences.
5. *Irrelevant:* logic capacity, BRAM, speed grade. We move 3.5 Kbps.

**Candidate: ALINX AX7102** (XC7A100T, 1× RJ45 GbE, **2× SFP+ @ 6.6 Gbps**).
Scores well: SFP on real GTP SerDes, free-tier part, more relevant I/O than
video-oriented boards at lower cost.

**Topology:** RJ45 in ← OT host, **SFP+ out → fibre with TX strand only**.

**MAC path:** RGMII → `verilog-ethernet` `eth_mac_1g_rgmii` for input; GTP →
Xilinx PCS/PMA (free IP) → `verilog-ethernet` MAC for SFP output. Avoid Xilinx
TEMAC (licensing).

**Bench BOM:** board + 2 SFP modules + LC fibre + **optical power meter** (the
proof instrument) + two hosts.

**Demo:** attacker laptop on IT side runs ping/nmap/nc → all fail; `tcpdump` on
OT host stays empty; power meter reads zero reverse light; Grafana keeps
updating throughout.

---

## 7. Open items

### 7.1 Immediate — blocking hardware

- [ ] **Commit/rollback FIFO.** *Design gap.* The validator learns the verdict
      only at the final CRC byte, by which time all bytes have streamed past.
      The current FIFO has no rollback, so wiring it up today would **forward
      rejected frames**. Needs provisional write pointer + commit on accept /
      rollback on reject (standard Ethernet MAC RX pattern).
- [ ] **Pump top-level** wiring validator → FIFO → output, with a test proving a
      rejected frame produces **zero bytes** at the output.
- [ ] Ethernet MAC integration (step 4) — needs the board.

### 7.2 Product P0s (deferred for the demo)

- [ ] **Cryptographic MAC.** CRC is not authentication — anything that can send
      UDP to the FPGA's port can inject telemetry. Essential once the proxy and
      FPGA aren't point-to-point cabled. Do it in *software* at both proxies,
      keep the FPGA keyless. Truncate to 8–16 bytes. Add an **epoch/session ID**
      at the same time (SEQ resets on restart → cross-restart replay).
      *Deferred:* `VERSION` byte gives a clean v3 upgrade path.
- [ ] **Store-and-forward buffering** — if the IT side is down, data is currently
      lost permanently. No customer will accept this.
- [ ] **Converge to `osdd-gateway`** — two divergent codebases; it still runs the
      old `redundancy: 3` transport.
- [ ] **Test against one real PLC** — every adapter was validated only against
      simulators we wrote.

### 7.3 Smaller

- [ ] Read-only enforcement as an *asserted* invariant in the driver interface
      (currently emergent: no write method exists, but nothing prevents one)
- [ ] Sensor timestamps use `time.time()` — an NTP step writes bad data
- [ ] Pluggable IT-side outputs (see §8)
- [ ] Outbound anomaly / covert-channel monitoring
- [ ] PHASE1_STATUS ASCII diagram still describes the old 3-copy flow

---

## 8. Key assumptions that could break the design

**Tier 1 — could invalidate the product claim**

1. **One-wayness will be physics-enforced.** Today it's Docker config — which is
   a firewall rule, exactly what DNV says makes diodes pointless. Untested.
2. **The OT-side proxy is trustworthy.** The diode does nothing if it's
   compromised.
3. **CRC is sufficient integrity.** It isn't, against an attacker.
4. **The outbound direction is safe.** Only inbound threats have been modelled.

**Tier 2 — would force redesign**

5. **Every reading is absolute state, so loss self-heals.** True for SCADA tags,
   **false for events** (alarms, counters). A customer wanting alarm delivery
   breaks the keyframe model. *Most load-bearing assumption in the design.*
6. **Losing data during an IT-side outage is acceptable.** No store-and-forward.
7. **~15 tags at 1 Hz is representative.** At 2,000 tags a keyframe becomes 10
   datagrams and the 5s self-heal promise degrades.
8. **The customer will adopt InfluxDB and Grafana.** They won't — this was a lab
   convenience presented as architecture.

**Tier 3 — will bite during bring-up**

9. FIFO is single-clock; Ethernet RX/TX need dual-clock with Gray-coded pointers
10. `rd_data` combinational memory read may need registering for block RAM
11. 1 byte/clock ≈ 1 Gbps class; 10G needs a wider datapath + parallel CRC
12. Standard 1500-byte MTU assumed
13. Adapters never tested against real vendor hardware
14. Sequence numbers wrap at 2³²; behaviour at wraparound untested

---

## 9. Architecture notes worth keeping

**The protocol break.** Terminate → extract → re-originate. The gateway is a real
client of each protocol; only `value + quality + id` survive; a brand-new frame
is minted. Broken at four layers: network (isolated segments), transport (TCP
dies, UDP crosses), application (protocol discarded), semantic (only readings).

**Why we don't do conventional DPI.** DPI is a filter for a *forwarding*
architecture; ours *terminates*. There is no original packet on the far side to
inspect. Whitelist extraction is stronger than blacklist signatures: DPI blocks
what it recognises and fails open on novel attacks; our format simply cannot
represent anything but a tag reading. DPI also belongs in the *proxy*, never in
the diode — a complex parser in the security-critical path destroys the
auditability that makes diodes certifiable.

**The replica concept (not yet built).** Rather than delivering data in our
format and requiring customers to adopt Grafana, expose a **read-only server
speaking a protocol they already use** (OPC UA, MQTT, Modbus), backed by the
IT-side tag-state table. Existing tools then repoint by IP instead of being
re-integrated. This is how Waterfall supports "hundreds of connectors". The
replica protocol is chosen by the **destination**, not the source — the universal
tag format decouples them.

**Why not a Kepware replacement.** Protocol breadth is commoditised (PLC4X,
Telegraf, Node-RED, libplctag). One-way transport with a genuine protocol break
is not. Spend engineering on the differentiator.
