"""Remote protocol of Rosler ceiling fans (JH-RM1/RM2/RM3 remotes).

One button press is a 33-bit frame, sent 7 times, on-off keyed near 447.885 MHz:

    [remote id: 20][button: 4][flag: 1][counter: 3][checksum: 4][0]

Every bit lasts 1 ms. A 0 is 250 us on then 750 us off, a 1 is 750 us on then
250 us off. Frames are separated by about 7 ms of silence.

    checksum = button XOR (flag << 3 | counter) XOR constant

The counter goes up by one on every press, whatever the button. The fan drops
a frame whose counter equals the counter of the last frame it accepted, so a
transmitter has to change the counter on every command.

This module has no Home Assistant dependency.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

ID_BITS = 20
FRAME_BITS = 33
MAX_REMOTE_ID = (1 << ID_BITS) - 1

# Transmit encoding: 4 symbols of 250 us per bit, then 7 ms of silence.
TX_BPS = 4000
_TX_ONE = "1110"
_TX_ZERO = "1000"
_TX_GAP = "0" * 28

# Counter values used for transmitting, in order. 6 is left out on purpose:
# Bond commands stored as two bursts (counter 2 then 6, see the README) leave
# the fan at 6, and skipping it here means such a command and this integration
# can never cancel each other out.
COUNTER_SEQUENCE = (0, 1, 2, 3, 4, 5, 7)

# Decoder tolerances, in microseconds.
_GAP_US = 2500  # a silence at least this long ends a frame
_PERIOD_MIN_US = 700
_PERIOD_MAX_US = 1300
_PULSE_MIN_US = 125
_PULSE_MAX_US = 1000
_PULSE_THRESHOLD_US = 500
_SMOOTH_US = 100
_MAX_SAMPLES = 400_000

# Identical frames needed in one capture before it is trusted.
MIN_MATCHING_FRAMES = 3


class ProtocolError(Exception):
    """Base class for protocol errors."""


class UnsupportedSignalError(ProtocolError):
    """The capture is in a format this decoder cannot read."""


class NoFrameError(ProtocolError):
    """No trustworthy frame was found in a capture."""


class Command(Enum):
    """Remote buttons as (button value, flag)."""

    SPEED_1 = (0x8, 0)
    SPEED_2 = (0x9, 0)
    SPEED_3 = (0xE, 0)
    SPEED_4 = (0x5, 0)
    SPEED_5 = (0x7, 1)
    SPEED_6 = (0x6, 0)
    ALL_OFF = (0x3, 0)
    FAN_OFF = (0xB, 0)
    LIGHT = (0x4, 0)
    FORWARD = (0x2, 0)
    BREEZE = (0xA, 1)
    REVERSE = (0x8, 1)
    TIMER_1H = (0x1, 0)
    TIMER_2H = (0x4, 1)
    TIMER_4H = (0xC, 1)

    @property
    def button(self) -> int:
        """Return the 4-bit button value."""
        return self.value[0]

    @property
    def flag(self) -> int:
        """Return the flag bit."""
        return self.value[1]

    @classmethod
    def from_code(cls, button: int, flag: int) -> Command | None:
        """Return the command for a button value and flag, if it is known."""
        try:
            return cls((button, flag))
        except ValueError:
            return None


SPEED_COMMANDS = (
    Command.SPEED_1,
    Command.SPEED_2,
    Command.SPEED_3,
    Command.SPEED_4,
    Command.SPEED_5,
    Command.SPEED_6,
)


def checksum(button: int, flag: int, counter: int, constant: int) -> int:
    """Return the 4-bit checksum of a frame."""
    return (button ^ (flag << 3 | counter) ^ constant) & 0xF


@dataclass(frozen=True, slots=True)
class Frame:
    """A decoded 33-bit frame."""

    remote_id: int
    button: int
    flag: int
    counter: int
    checksum: int

    @property
    def constant(self) -> int:
        """Return the checksum constant this frame implies."""
        return self.button ^ (self.flag << 3 | self.counter) ^ self.checksum

    @property
    def command(self) -> Command | None:
        """Return the remote button, if it is a known one."""
        return Command.from_code(self.button, self.flag)

    @classmethod
    def from_bits(cls, bits: str) -> Frame:
        """Build a frame from 33 characters of 0 and 1."""
        if len(bits) != FRAME_BITS or set(bits) - {"0", "1"}:
            raise ValueError("a frame is 33 bits")
        return cls(
            remote_id=int(bits[:20], 2),
            button=int(bits[20:24], 2),
            flag=int(bits[24], 2),
            counter=int(bits[25:28], 2),
            checksum=int(bits[28:32], 2),
        )


@dataclass(frozen=True, slots=True)
class Capture:
    """The frame a capture agrees on."""

    frame: Frame
    matching: int  # frames identical to this one
    total: int  # well-formed frames in the capture


def frame_bits(remote_id: int, command: Command, counter: int, constant: int) -> str:
    """Return the 33 bits of a frame as a string of 0 and 1."""
    if not 0 <= remote_id <= MAX_REMOTE_ID:
        raise ValueError("remote id must fit in 20 bits")
    if not 0 <= counter <= 7:
        raise ValueError("counter must be 0 to 7")
    if not 0 <= constant <= 0xF:
        raise ValueError("checksum constant must be 0 to 15")
    check = checksum(command.button, command.flag, counter, constant)
    return (
        f"{remote_id:020b}{command.button:04b}{command.flag:01b}"
        f"{counter:03b}{check:04b}0"
    )


def tx_signal(
    remote_id: int,
    command: Command,
    counter: int,
    constant: int,
    *,
    frequency_khz: int,
    repeats: int,
) -> dict[str, Any]:
    """Return the body for Bond's PUT /v2/signal/tx."""
    bits = frame_bits(remote_id, command, counter, constant)
    data = "".join(_TX_ONE if bit == "1" else _TX_ZERO for bit in bits) + _TX_GAP
    return {
        "freq": frequency_khz,
        "modulation": "OOK",
        "encoding": "cq",
        "bps": TX_BPS,
        "reps": repeats,
        "data": data,
    }


def tx_duration(repeats: int) -> float:
    """Return how long a transmission lasts, in seconds."""
    return repeats * (FRAME_BITS * 4 + len(_TX_GAP)) / TX_BPS


def _expand_cq(data: str) -> str:
    """Expand Bond's cq run-length encoding into samples."""
    out: list[str] = []
    size = 0
    for char in data:
        if char in "01":
            piece = char
        elif "C" <= char <= "Q":
            piece = "0" * (1 << (ord(char) - ord("C")))
        elif "c" <= char <= "q":
            piece = "1" * (1 << (ord(char) - ord("c")))
        elif char == "A":
            piece = "110"
        elif char == "B":
            piece = "011"
        elif char.isspace():
            continue
        else:
            raise UnsupportedSignalError(f"unexpected character {char!r} in cq data")
        size += len(piece)
        if size > _MAX_SAMPLES:
            raise UnsupportedSignalError("signal is too long")
        out.append(piece)
    return "".join(out)


def _expand(signal: Mapping[str, Any]) -> tuple[str, float]:
    """Return a signal as samples of 0 and 1 and the sample length in us."""
    try:
        data = str(signal["data"])
        bps = float(signal["bps"])
    except (KeyError, TypeError, ValueError) as err:
        raise UnsupportedSignalError("signal has no data or bps") from err
    if not 100 <= bps <= 40000:
        raise UnsupportedSignalError(f"unsupported bps {bps}")

    encoding = signal.get("encoding")
    if encoding == "hex":
        try:
            samples = "".join(
                f"{int(char, 16):04b}" for char in data if not char.isspace()
            )
        except ValueError as err:
            raise UnsupportedSignalError("invalid hex data") from err
    elif encoding == "cq":
        samples = _expand_cq(data)
    else:
        raise UnsupportedSignalError(f"unsupported encoding {encoding!r}")

    try:
        repeats = max(1, int(signal.get("reps") or 1))
    except (TypeError, ValueError):
        repeats = 1
    if len(samples) * repeats > _MAX_SAMPLES:
        raise UnsupportedSignalError("signal is too long")
    return samples * repeats, 1_000_000 / bps


def _smooth(samples: str, width: int) -> str:
    """Majority-filter samples to drop glitches shorter than half the width."""
    if width <= 1 or len(samples) < width:
        return samples
    half = width // 2
    values = [0] * half + [1 if char == "1" else 0 for char in samples] + [0] * half
    window = sum(values[:width])
    out = []
    for index in range(len(samples)):
        out.append("1" if window > half else "0")
        window += values[index + width] if index + width < len(values) else 0
        window -= values[index]
    return "".join(out)


def _pulses(samples: str, sample_us: float) -> list[tuple[float, float]]:
    """Return every pulse as (on, following off) durations in us."""
    pulses: list[tuple[float, float]] = []
    index = samples.find("1")
    length = len(samples)
    while 0 <= index < length:
        start = index
        while index < length and samples[index] == "1":
            index += 1
        high = (index - start) * sample_us
        start = index
        while index < length and samples[index] == "0":
            index += 1
        pulses.append((high, (index - start) * sample_us))
    return pulses


def _is_bit(pulse: tuple[float, float]) -> bool:
    """Return whether a pulse and its off time have the shape of one bit."""
    high, low = pulse
    return (
        _PULSE_MIN_US <= high <= _PULSE_MAX_US
        and _PERIOD_MIN_US <= high + low <= _PERIOD_MAX_US
    )


def _frame_at(pulses: list[tuple[float, float]], start: int) -> str | None:
    """Return the 33 bits of a frame whose first pulse is at start, if any."""
    last = start + FRAME_BITS - 1
    if last >= len(pulses):
        return None
    # A frame is not the tail end of a longer run of bits.
    if start and _is_bit(pulses[start - 1]):
        return None
    if not all(_is_bit(pulse) for pulse in pulses[start:last]):
        return None
    # The last bit is always 0, and its off time runs into the silence that
    # separates frames, or into the end of the capture.
    high, low = pulses[last]
    if not _PULSE_MIN_US <= high < _PULSE_THRESHOLD_US:
        return None
    if low < _GAP_US and last != len(pulses) - 1:
        return None
    return (
        "".join(
            "1" if pulse[0] >= _PULSE_THRESHOLD_US else "0"
            for pulse in pulses[start:last]
        )
        + "0"
    )


def decode_frames(signal: Mapping[str, Any]) -> list[Frame]:
    """Return every well-formed frame in a Bond scan result.

    Bits are read from the length of each pulse rather than from a fixed
    clock, so a remote running a few percent fast or slow still decodes, and
    a stray pulse next to a frame does not spoil it.
    """
    samples, sample_us = _expand(signal)
    width = int(_SMOOTH_US / sample_us) | 1 if sample_us <= _SMOOTH_US / 2 else 1
    pulses = _pulses(_smooth(samples, width), sample_us)
    frames = []
    index = 0
    while index < len(pulses):
        bits = _frame_at(pulses, index)
        if bits is None:
            index += 1
        else:
            frames.append(Frame.from_bits(bits))
            index += FRAME_BITS
    return frames


def identify(signal: Mapping[str, Any]) -> Capture:
    """Return the frame a scan result agrees on.

    A press repeats its frame 7 times, so a good capture holds several
    identical frames. Anything less than MIN_MATCHING_FRAMES is rejected.
    """
    frames = decode_frames(signal)
    if not frames:
        raise NoFrameError("no frame found")
    frame, matching = Counter(frames).most_common(1)[0]
    if matching < MIN_MATCHING_FRAMES:
        raise NoFrameError(
            f"only {matching} of {len(frames)} frames agree, need {MIN_MATCHING_FRAMES}"
        )
    return Capture(frame=frame, matching=matching, total=len(frames))


def format_remote_id(remote_id: int) -> str:
    """Return a remote id as 5 hex digits."""
    return f"{remote_id:05X}"


def parse_remote_id(text: str) -> int:
    """Parse a remote id given as 5 hex digits or 20 binary digits."""
    cleaned = text.strip().replace(" ", "").replace("_", "")
    if len(cleaned) == ID_BITS and not set(cleaned) - {"0", "1"}:
        return int(cleaned, 2)
    if cleaned.lower().startswith("0x"):
        cleaned = cleaned[2:]
    if not 1 <= len(cleaned) <= 5:
        raise ValueError("remote id is 5 hex digits or 20 binary digits")
    value = int(cleaned, 16)
    if not 0 <= value <= MAX_REMOTE_ID:
        raise ValueError("remote id must fit in 20 bits")
    return value
