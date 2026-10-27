"""Standalone Modbus TCP Simulator for testing."""

import argparse
import asyncio
import random
import logging
import struct

from pymodbus.server import StartAsyncTcpServer
from pymodbus.datastore import ModbusSequentialDataBlock, ModbusSlaveContext, ModbusServerContext

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("modbus_sim")

async def update_values(context: ModbusServerContext):
    """Periodically update register values to simulate live sensor data."""
    logger.info("Starting background value updater...")
    slave_id = 1
    
    temp1 = 23.5
    temp2 = 45.2
    
    while True:
        await asyncio.sleep(1.0)
        
        # Add random noise
        temp1 += random.uniform(-0.5, 0.5)
        temp2 += random.uniform(-1.0, 1.0)
        
        # Pack floats into Modbus holding registers (big-endian float32 -> two 16-bit words)
        t1_regs = struct.unpack(">HH", struct.pack(">f", temp1))
        t2_regs = struct.unpack(">HH", struct.pack(">f", temp2))
        
        slave_context = context[slave_id]
        
        # Update holding registers (address 0 and 2)
        # 3 is the function code for Holding Registers
        slave_context.setValues(3, 0, list(t1_regs))
        slave_context.setValues(3, 2, list(t2_regs))
        
        # Toggle a coil (address 0)
        # 1 is the function code for Coils
        current_coil = slave_context.getValues(1, 0, count=1)[0]
        slave_context.setValues(1, 0, [not current_coil])

async def run_server(host: str, port: int):
    # Initialize 100 registers for each type (discrete inputs, coils, holding regs, input regs)
    store = ModbusSlaveContext(
        di=ModbusSequentialDataBlock(0, [0]*100),
        co=ModbusSequentialDataBlock(0, [0]*100),
        hr=ModbusSequentialDataBlock(0, [0]*100),
        ir=ModbusSequentialDataBlock(0, [0]*100)
    )
    context = ModbusServerContext(slaves={1: store}, single=False)

    asyncio.create_task(update_values(context))

    logger.info(f"Starting Modbus TCP Simulator on {host}:{port}")
    await StartAsyncTcpServer(
        context=context,
        address=(host, port),
    )

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Modbus TCP Simulator")
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5020)
    args = parser.parse_args()
    
    try:
        asyncio.run(run_server(args.host, args.port))
    except KeyboardInterrupt:
        logger.info("Simulator stopped.")
