#!/usr/bin/env python3
"""Read the Geekworm X1203 fuel gauge once, to confirm I2C wiring works."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'code'))

try:
    from smbus2 import SMBus
    from x1203_monitor import read_voltage, read_soc
except ImportError as exc:
    print(f"Import failed: {exc}")
    print("Install the dependency with: pip3 install smbus2")
    sys.exit(1)


def find_i2c_buses():
    buses = []
    for bus_num in range(10):
        try:
            SMBus(bus_num).close()
            buses.append(bus_num)
        except (FileNotFoundError, OSError):
            pass
    return buses


def main():
    i2c_buses = find_i2c_buses()
    if not i2c_buses:
        print("No I2C buses found. Enable I2C with 'sudo raspi-config'.")
        return 1
    print(f"I2C buses found: {i2c_buses}")

    bus_num = 1 if 1 in i2c_buses else i2c_buses[0]
    print(f"Reading gauge on bus {bus_num}...")

    try:
        with SMBus(bus_num) as bus:
            voltage = read_voltage(bus)
            soc = read_soc(bus)
    except Exception as exc:
        print(f"Failed to read the gauge at 0x36: {exc}")
        return 1

    print(f"Voltage: {voltage:.3f} V")
    print(f"Charge:  {soc:.2f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
