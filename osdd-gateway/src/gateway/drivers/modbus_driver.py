"""Modbus TCP driver for OSDD Protocol Gateway."""

from __future__ import annotations

import struct
from datetime import datetime, timezone
from typing import Any

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from gateway.drivers.base import BaseDriver
from gateway.core.tag_manager import TagValue


class ModbusDriver(BaseDriver):
    """Driver for Modbus TCP devices.
    
    Reads holding registers, input registers, coils, and discrete inputs
    from one or more Modbus TCP devices. Supports decoding 32-bit and 64-bit
    values with configurable byte ordering and word swapping.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.devices = config.get("devices", [])
        self.clients: dict[str, AsyncModbusTcpClient] = {}

    @property
    def protocol_name(self) -> str:
        return "modbus"

    async def _connect(self) -> None:
        """Establish connections to all configured Modbus devices."""
        for dev in self.devices:
            host = dev["host"]
            port = dev.get("port", 502)
            timeout = dev.get("timeout_seconds", 3.0)
            
            client = AsyncModbusTcpClient(host, port=port, timeout=timeout)
            await client.connect()
            
            if not client.connected:
                # Close any successfully opened clients before raising
                await self._disconnect()
                raise ConnectionError(f"Failed to connect to Modbus device {dev['name']} at {host}:{port}")
            
            self.clients[dev["name"]] = client

    async def _disconnect(self) -> None:
        """Close connections to all devices."""
        for client in self.clients.values():
            client.close()
        self.clients.clear()

    async def _read_tags(self) -> list[TagValue]:
        """Read all configured tags from all devices."""
        results = []
        for dev in self.devices:
            client = self.clients.get(dev["name"])
            if not client or not client.connected:
                raise ConnectionError(f"Modbus client for {dev['name']} is not connected.")

            unit_id = dev.get("unit_id", 1)
            
            for tag_conf in dev.get("tags", []):
                val = await self._read_single_tag(client, unit_id, tag_conf)
                
                if val is not None:
                    # Apply scale and offset
                    scale = tag_conf.get("scale", 1.0)
                    offset = tag_conf.get("offset", 0.0)
                    if scale != 1.0 or offset != 0.0:
                        val = (val * scale) + offset

                    results.append(TagValue(
                        name=tag_conf["name"],
                        value=val,
                        data_type=tag_conf["data_type"],
                        quality="Good",
                        timestamp=datetime.now(timezone.utc),
                        unit=tag_conf.get("unit"),
                        source_driver=self.name,
                        metadata={"device": dev["name"], "address": tag_conf["address"]}
                    ))
        return results

    async def _read_single_tag(self, client: AsyncModbusTcpClient, unit_id: int, tag_conf: dict[str, Any]) -> Any:
        address = tag_conf["address"]
        reg_type = tag_conf.get("register_type", "holding_registers")
        data_type = tag_conf.get("data_type", "uint16")
        byte_order = tag_conf.get("byte_order", "big_endian")
        word_swap = tag_conf.get("word_swap", False)

        # Determine register count based on data type
        count = 1
        if data_type in ("float32", "uint32", "int32"):
            count = 2
        elif data_type in ("float64", "uint64", "int64"):
            count = 4

        try:
            if reg_type == "holding_registers":
                resp = await client.read_holding_registers(address, count=count, slave=unit_id)
            elif reg_type == "input_registers":
                resp = await client.read_input_registers(address, count=count, slave=unit_id)
            elif reg_type == "coils":
                resp = await client.read_coils(address, count=count, slave=unit_id)
            elif reg_type == "discrete_inputs":
                resp = await client.read_discrete_inputs(address, count=count, slave=unit_id)
            else:
                self.log.error("modbus.invalid_register_type", reg_type=reg_type, tag=tag_conf["name"])
                return None
            
            if resp.isError():
                raise ModbusException(f"Modbus error response: {resp}")

            if reg_type in ("coils", "discrete_inputs"):
                return resp.bits[0]

            return self._decode_registers(resp.registers, data_type, byte_order, word_swap)

        except ModbusException as exc:
            self.log.error("modbus.read_error", address=address, tag=tag_conf["name"], error=str(exc))
            raise

    def _decode_registers(self, registers: list[int], data_type: str, byte_order: str, word_swap: bool) -> Any:
        if word_swap and len(registers) >= 2:
            # Swap adjacent 16-bit words
            swapped = []
            for i in range(0, len(registers), 2):
                if i + 1 < len(registers):
                    swapped.extend([registers[i+1], registers[i]])
                else:
                    swapped.append(registers[i])
            registers = swapped

        # Pack registers to bytes (Modbus transmits words as big-endian)
        raw_bytes = struct.pack(f">{len(registers)}H", *registers)
        endian_char = ">" if byte_order == "big_endian" else "<"
        
        try:
            if data_type == "uint16":
                return struct.unpack(f"{endian_char}H", raw_bytes)[0]
            elif data_type == "int16":
                return struct.unpack(f"{endian_char}h", raw_bytes)[0]
            elif data_type == "float32":
                return struct.unpack(f"{endian_char}f", raw_bytes)[0]
            elif data_type == "uint32":
                return struct.unpack(f"{endian_char}I", raw_bytes)[0]
            elif data_type == "int32":
                return struct.unpack(f"{endian_char}i", raw_bytes)[0]
            elif data_type == "float64":
                return struct.unpack(f"{endian_char}d", raw_bytes)[0]
            else:
                return registers[0]
        except struct.error as exc:
            self.log.error("modbus.decode_error", error=str(exc), data_type=data_type)
            return None
