"""Find the Modbus address of an HG803 sensor using broadcast address 0x00."""

import asyncio
import sys

from pymodbus.client import AsyncModbusSerialClient


async def find_address(port: str, baudrate: int = 9600) -> None:
    """Read the device address register via broadcast.

    :param port: Serial port path.
    :param baudrate: Baud rate (default 9600).
    """
    client = AsyncModbusSerialClient(
        port=port, baudrate=baudrate, timeout=2, parity="N", stopbits=1, bytesize=8
    )
    await client.connect()

    try:
        # Read address from hold register 0x0100 using broadcast 0x00
        result = await client.read_holding_registers(address=0x0100, count=1, device_id=0)
        if result.isError():
            print("Error: %s" % result)
            return

        address = result.registers[0]
        print("Device address: %d (0x%02X)" % (address, address))

        # Also read a measurement to confirm the sensor is working
        result = await client.read_holding_registers(address=0x0004, count=4, device_id=0)
        if not result.isError():
            regs = result.registers
            temp = regs[0] if regs[0] < 0x8000 else regs[0] - 0x10000
            print("Temperature: %.1f°C, Humidity: %.1f%%RH" % (temp / 10.0, regs[1] / 10.0))
    finally:
        client.close()


if __name__ == "__main__":
    port = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB1"
    asyncio.run(find_address(port))
