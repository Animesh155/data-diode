"""
opcua_adapter.py — OPC UA client for the OT proxy.

Reads the 15 Double variables under Objects.ProcessVariables on the
plc-opcua server and returns the universal sensor struct.

Uses asyncua.sync to keep the rest of ot_proxy.py blocking — the diode
pipeline is a synchronous poll loop and isn't worth porting to asyncio
for one adapter.

Tag-id namespace: 0x0301-0x030F (see s7_adapter for the full map).
"""

import logging
import time

from asyncua import ua
from asyncua.sync import Client


QUALITY_GOOD      = 0x00
QUALITY_COMM_FAIL = 0xFF

OPCUA_TAG_MAP = [
    (0x0301, 'P1_FT01_intake_flow_1'),
    (0x0302, 'P1_FT01Z_intake_flow_1z'),
    (0x0303, 'P1_FT02_transfer_flow'),
    (0x0304, 'P1_FT03_output_flow'),
    (0x0305, 'P1_LIT01_tank_level'),
    (0x0306, 'P1_LCV01D_lvl_valve_D'),
    (0x0307, 'P1_LCV01Z_lvl_valve_Z'),
    (0x0308, 'P1_PIT01_pressure_1'),
    (0x0309, 'P1_PIT02_pressure_2'),
    (0x030A, 'P1_PCV01D_press_valve_D'),
    (0x030B, 'P1_PCV01Z_press_valve_Z'),
    (0x030C, 'P1_TIT01_temperature_1'),
    (0x030D, 'P1_TIT02_temperature_2'),
    (0x030E, 'P1_FCV03D_flow_valve_D'),
    (0x030F, 'P1_FCV03Z_flow_valve_Z'),
]

NAMESPACE_URI = "http://lab.diode/plc"


class OpcUaAdapter:
    """Polls an OPC UA server via asyncua.sync and emits normalized readings."""

    def __init__(self, url):
        self.url = url
        self.client = None
        self.nodes = None     # resolved on connect()
        self.log = logging.getLogger('opcua_adapter')

    def connect(self):
        try:
            client = Client(url=self.url)
            client.connect()
            ns_idx = client.get_namespace_index(NAMESPACE_URI)
            # Resolve each tag by its string NodeId (ns=<idx>;s=<name>) rather
            # than by walking browse paths. The NodeId is the stable handle a
            # real server publishes for a tag — Kepware/PLC4X hand out exactly
            # this form (e.g. ns=2;s=Channel1.Device1.Tag). Browse names depend
            # on how the address space happened to be built and can sit in a
            # different namespace than the NodeId, which is fragile to rely on.
            nodes = []
            for tag_id, name in OPCUA_TAG_MAP:
                node = client.get_node(ua.NodeId(name, ns_idx))
                nodes.append((tag_id, name, node))
            # get_node() is purely local — it builds a handle without touching
            # the wire. Probe one read so a wrong namespace or missing tag
            # fails here at connect time instead of silently on every poll.
            nodes[0][2].read_value()
            self.client = client
            self.nodes = nodes
            self.log.info(f"Connected to OPC UA server at {self.url} (ns={ns_idx})")
            return True
        except Exception as e:
            self.log.error(f"OPC UA connect failed: {e}")
            self._teardown()
            return False

    def poll(self):
        if self.client is None:
            if not self.connect():
                return self._comm_fail_readings()

        timestamp = int(time.time())
        readings = []
        try:
            for tag_id, name, node in self.nodes:
                value = node.read_value()
                readings.append({
                    'timestamp': timestamp,
                    'tag_id':    tag_id,
                    'value':     float(value),
                    'quality':   QUALITY_GOOD,
                    'name':      name,
                })
        except Exception as e:
            self.log.error(f"OPC UA read exception: {e}")
            self._teardown()
            return self._comm_fail_readings()
        return readings

    def _comm_fail_readings(self):
        timestamp = int(time.time())
        return [
            {
                'timestamp': timestamp,
                'tag_id':    tag_id,
                'value':     0.0,
                'quality':   QUALITY_COMM_FAIL,
                'name':      name,
            }
            for tag_id, name in OPCUA_TAG_MAP
        ]

    def _teardown(self):
        if self.client is not None:
            try:
                self.client.disconnect()
            except Exception:
                pass
        self.client = None
        self.nodes = None

    def close(self):
        self._teardown()
