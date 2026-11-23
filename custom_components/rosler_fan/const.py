"""Constants for the Rosler ceiling fan integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "rosler_fan"

CONF_REMOTE_ID: Final = "remote_id"
CONF_CHECKSUM_CONSTANT: Final = "checksum_constant"
CONF_FREQUENCY: Final = "frequency_khz"
CONF_REPEATS: Final = "repeats"
CONF_BOND_ID: Final = "bond_id"

# Measured on a JH-RM2 remote: Bond captures it cleanly from 447.775 to
# 448.025 MHz, so the centre of that window is used for both scan and transmit.
DEFAULT_FREQUENCY_KHZ: Final = 447885
MIN_FREQUENCY_KHZ: Final = 446000
MAX_FREQUENCY_KHZ: Final = 449000

# The remote sends each frame 7 times. One repeat lasts 40 ms and a Bond
# signal may not exceed about 0.61 s, which caps this at 15.
DEFAULT_REPEATS: Final = 7
MIN_REPEATS: Final = 3
MAX_REPEATS: Final = 15

DEFAULT_CHECKSUM_CONSTANT: Final = 8

MANUFACTURER: Final = "Rosler"
MODEL: Final = "Ceiling fan (JH-RM remote)"
