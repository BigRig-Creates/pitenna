#!/usr/bin/env python3
"""
Geekworm X1203 battery monitor reader.

X1203 exposes a MAX17048-compatible fuel gauge at I2C address 0x36.
"""

from smbus2 import SMBus

I2C_BUS = 1
GAUGE_ADDR = 0x36


def _read_word(bus: SMBus, register: int) -> int:
    raw = bus.read_word_data(GAUGE_ADDR, register)
    return ((raw << 8) & 0xFF00) | (raw >> 8)


def read_voltage(bus: SMBus) -> float:
    """Return battery voltage in volts."""
    vcell = _read_word(bus, 0x02)
    millivolts = (vcell >> 4) * 0.078125
    return millivolts / 1000.0


def read_soc(bus: SMBus) -> float:
    """Return battery state of charge in percent."""
    soc_raw = _read_word(bus, 0x04)
    whole = soc_raw >> 8
    frac = (soc_raw & 0xFF) / 256.0
    return whole + frac
