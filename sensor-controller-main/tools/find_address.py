"""Scan the RS-485 bus for HG803 sensors.

Usage::

    uv run tools/find_address.py /dev/ttyUSB0
    uv run tools/find_address.py /dev/ttyUSB0 --start 1 --end 20
"""

import argparse
import sys
import time

import modbus_tk.defines as cst
import modbus_tk.modbus_rtu as modbus_rtu
import serial

# HG803 0.1-resolution temperature register
_REG_TEMP = 0x0004

# Default gap between consecutive probes.  Modbus RTU requires ≥3.5 char
# times of silence between frames, and the RS-485 transceiver needs time
# to swap direction.  Real-world USB-to-RS485 adapters usually need more
# than the theoretical minimum — bump this up if probes skip.
_INTER_PROBE_DELAY = 0.15


def scan(
    port: str,
    baudrate: int,
    start: int,
    end: int,
    delay: float,
) -> list[int]:
    """Probe each address in [start, end] and return those that respond.

    :param port: Serial device path.
    :param baudrate: Baud rate.
    :param start: First Modbus address to try.
    :param end: Last Modbus address to try (inclusive).
    :param delay: Pause between probes (seconds).
    :returns: List of addresses that returned a valid response.
    :rtype: list[int]
    """
    ser = serial.Serial(
        port=port,
        baudrate=baudrate,
        bytesize=8,
        parity="N",
        stopbits=1,
        timeout=0.3,
        inter_byte_timeout=0.05,
    )
    master = modbus_rtu.RtuMaster(ser)
    master.set_timeout(0.3)

    found: list[int] = []
    for addr in range(start, end + 1):
        # Drop any bytes still sitting in the OS/USB buffer from the
        # previous response — otherwise they get read as the head of the
        # next response, fail CRC, and the probe looks like a timeout.
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        try:
            master.execute(addr, cst.READ_HOLDING_REGISTERS, _REG_TEMP, 1)
            found.append(addr)
            print("  [+] Address %d — responding" % addr)
        except Exception:
            pass
        time.sleep(delay)

    master.close()
    return found


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Scan RS-485 bus for HG803 sensors",
    )
    parser.add_argument("port", help="Serial port (e.g. /dev/ttyUSB0)")
    parser.add_argument("--baudrate", type=int, default=9600)
    parser.add_argument("--start", type=int, default=1, help="First address")
    parser.add_argument("--end", type=int, default=247, help="Last address")
    parser.add_argument(
        "--delay",
        type=float,
        default=_INTER_PROBE_DELAY,
        help="Seconds to wait between probes (increase if probes skip)",
    )
    args = parser.parse_args()

    print(
        "Scanning %s at %d baud, addresses %d-%d (delay %.2fs) …"
        % (args.port, args.baudrate, args.start, args.end, args.delay)
    )
    found = scan(
        args.port,
        args.baudrate,
        args.start,
        args.end,
        args.delay,
    )

    if found:
        print("\nFound %d sensor(s): %s" % (len(found), found))
    else:
        print("\nNo sensors found.")
        sys.exit(1)


if __name__ == "__main__":
    main()
