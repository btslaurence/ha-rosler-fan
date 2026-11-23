"""Tests for the remote protocol."""

from __future__ import annotations

import itertools

import jinja2
import pytest

from custom_components.rosler_fan.protocol import (
    COUNTER_SEQUENCE,
    Command,
    Frame,
    NoFrameError,
    UnsupportedSignalError,
    checksum,
    decode_frames,
    format_remote_id,
    frame_bits,
    identify,
    parse_remote_id,
    tx_duration,
    tx_signal,
)

from .common import CONSTANT, REMOTE_ID, press, render_scan

# The rest_command payload template that was verified against a real fan
# (speed 1, speed 3 and fan off). The integration must produce the same bytes.
_VERIFIED_TEMPLATE = (
    "{%- set b = button | int -%}"
    "{%- set c4 = (flag | int) * 8 + (cnt | int) % 8 -%}"
    "{%- set k = b | bitwise_xor(c4) | bitwise_xor(8) -%}"
    "{%- set bits = id ~ '{:04b}'.format(b) ~ '{:04b}'.format(c4)"
    " ~ '{:04b}'.format(k) ~ '0' -%}"
    "{%- set data = bits | replace('1','x') | replace('0','1000')"
    " | replace('x','1110') -%}"
    "{{ data }}{{ '0' * 28 }}"
)


def test_known_frames() -> None:
    """Frames match the two examples worked out by hand in the write-up."""
    # Speed 3 is button 1110. Counter 2 gives checksum 0100, counter 6 gives 0000.
    assert frame_bits(0, Command.SPEED_3, 2, 8)[20:] == "1110001001000"
    assert frame_bits(0, Command.SPEED_3, 6, 8)[20:] == "1110011000000"


def test_checksum_formula() -> None:
    """The checksum is button XOR (flag, counter) XOR constant."""
    assert checksum(0xE, 0, 2, 8) == 0x4
    assert checksum(0x8, 1, 5, 8) == 0x8 ^ 0xD ^ 0x8


def test_button_codes_are_unique() -> None:
    """Every button has its own (value, flag) pair."""
    assert len({command.value for command in Command}) == len(Command) == 15
    assert Command.from_code(0x8, 0) is Command.SPEED_1
    assert Command.from_code(0x8, 1) is Command.REVERSE
    assert Command.from_code(0x4, 0) is Command.LIGHT
    assert Command.from_code(0x4, 1) is Command.TIMER_2H
    assert Command.from_code(0xF, 0) is None


def test_matches_verified_template() -> None:
    """Output equals the template that moved a real fan, for every frame."""
    env = jinja2.Environment(autoescape=False)
    env.filters["bitwise_xor"] = lambda a, b: a ^ b
    template = env.from_string(_VERIFIED_TEMPLATE)
    for command in Command:
        for counter in range(8):
            expected = template.render(
                id=f"{REMOTE_ID:020b}",
                button=command.button,
                flag=command.flag,
                cnt=counter,
            )
            signal = tx_signal(
                REMOTE_ID, command, counter, 8, frequency_khz=447885, repeats=7
            )
            assert signal["data"] == expected
            assert signal == {
                "freq": 447885,
                "modulation": "OOK",
                "encoding": "cq",
                "bps": 4000,
                "reps": 7,
                "data": expected,
            }


def test_tx_signal_shape() -> None:
    """A frame is 160 symbols, so 7 repeats last 0.28 s."""
    signal = tx_signal(
        REMOTE_ID, Command.FAN_OFF, 0, 8, frequency_khz=447885, repeats=7
    )
    assert len(signal["data"]) == 160
    assert set(signal["data"]) == {"0", "1"}
    assert tx_duration(7) == pytest.approx(0.28)
    # Bond rejects signals longer than about 0.61 s.
    assert tx_duration(15) <= 0.61


def test_counter_sequence_never_repeats() -> None:
    """Two commands in a row never share a counter, and 6 is never used."""
    assert 6 not in COUNTER_SEQUENCE
    cycle = [*COUNTER_SEQUENCE, COUNTER_SEQUENCE[0]]
    assert all(a != b for a, b in itertools.pairwise(cycle))


@pytest.mark.parametrize("command", list(Command))
@pytest.mark.parametrize("counter", range(8))
def test_round_trip(command: Command, counter: int) -> None:
    """A rendered press decodes back to the same frame."""
    capture = identify(press(command, counter))
    assert capture.matching == capture.total == 7
    assert capture.frame.remote_id == REMOTE_ID
    assert capture.frame.command is command
    assert capture.frame.counter == counter
    assert capture.frame.constant == CONSTANT


@pytest.mark.parametrize("clock", [0.93, 0.97, 1.0, 1.02, 1.05, 1.08])
def test_clock_drift(clock: float) -> None:
    """A remote running fast or slow still decodes."""
    capture = identify(press(Command.SPEED_5, 3, clock=clock))
    assert capture.frame.command is Command.SPEED_5
    assert capture.matching == 7


@pytest.mark.parametrize("seed", range(20))
def test_glitches(seed: int) -> None:
    """Short glitches are filtered out or outvoted."""
    capture = identify(press(Command.BREEZE, 4, glitches=40, seed=seed))
    assert capture.frame.command is Command.BREEZE
    assert capture.frame.counter == 4
    assert capture.frame.remote_id == REMOTE_ID


@pytest.mark.parametrize("stray_us", [75, 100, 1200, 3000])
@pytest.mark.parametrize("stray_before_us", [300, 1000, 2000])
def test_stray_pulse_before_frames(stray_us: float, stray_before_us: float) -> None:
    """A noise pulse just before each frame does not cost the frame."""
    capture = identify(
        press(Command.SPEED_4, 2, stray_us=stray_us, stray_before_us=stray_before_us)
    )
    assert capture.frame.command is Command.SPEED_4
    assert capture.matching == 7


def test_bit_shaped_pulse_before_frame_is_not_a_frame() -> None:
    """34 bits in a row are not read as a 33-bit frame."""
    bits = frame_bits(REMOTE_ID, Command.SPEED_4, 2, CONSTANT)
    assert decode_frames(render_scan(["0" + bits] * 7)) == []
    assert decode_frames(render_scan(["1" + bits] * 7)) == []


def test_noise_after_the_press() -> None:
    """Random noise in the silence after the frames is ignored."""
    signal = press(Command.SPEED_4, 2, tail_us=300000, glitches=400, seed=7)
    capture = identify(signal)
    assert capture.frame.command is Command.SPEED_4
    assert capture.matching >= 3


def test_other_constant() -> None:
    """A remote with another checksum constant reports that constant."""
    capture = identify(press(Command.SPEED_2, 1, constant=0x3))
    assert capture.frame.constant == 0x3


def test_capture_starting_mid_frame() -> None:
    """A capture that begins inside a frame drops that frame only."""
    # 20 ms of lead-in, then cut 30 ms: the capture starts 10 ms into frame 1.
    capture = identify(press(Command.SPEED_1, 0, cut_start_us=30000))
    assert capture.total == 6
    assert capture.frame.command is Command.SPEED_1


def test_capture_ending_mid_frame() -> None:
    """A frame cut off by the end of the capture is dropped."""
    bits = frame_bits(REMOTE_ID, Command.SPEED_1, 0, CONSTANT)
    signal = render_scan([bits] * 4 + [bits[:20]], tail_us=0)
    assert identify(signal).total == 4


def test_too_few_frames() -> None:
    """Fewer than three matching frames is not trusted."""
    with pytest.raises(NoFrameError):
        identify(press(Command.SPEED_1, 0, repeats=2))


def test_silence() -> None:
    """A capture without a signal raises."""
    with pytest.raises(NoFrameError):
        identify(render_scan([]))


def test_wrong_frame_length() -> None:
    """Frames that are not 33 bits are ignored."""
    assert decode_frames(render_scan(["1011" * 6] * 7)) == []
    assert decode_frames(render_scan(["10" * 20] * 7)) == []


def test_last_bit_must_be_zero() -> None:
    """A 33-bit burst ending in 1 is not a frame."""
    bits = frame_bits(REMOTE_ID, Command.SPEED_1, 0, CONSTANT)
    assert decode_frames(render_scan([bits[:-1] + "1"] * 7)) == []


def test_decodes_own_transmission() -> None:
    """The cq signal that is transmitted decodes to the frame it encodes."""
    signal = tx_signal(
        REMOTE_ID, Command.TIMER_4H, 5, CONSTANT, frequency_khz=447885, repeats=7
    )
    capture = identify(signal)
    assert capture.matching == 7
    assert capture.frame.command is Command.TIMER_4H
    assert capture.frame.counter == 5


def test_cq_run_lengths() -> None:
    """cq letters expand to runs: A is 110, c is one 1, D is two 0s, G is 16."""
    bits = frame_bits(REMOTE_ID, Command.FORWARD, 2, CONSTANT)
    one, zero = "1A", "cDC"  # 1110 and 1000
    data = "".join(one if bit == "1" else zero for bit in bits) + "GFE"  # 28 zeros
    signal = {"encoding": "cq", "bps": 4000, "reps": 5, "data": data}
    capture = identify(signal)
    assert capture.matching == 5
    assert capture.frame.command is Command.FORWARD
    assert capture.frame.counter == 2


@pytest.mark.parametrize(
    "signal",
    [
        {"encoding": "hex", "bps": 40000},
        {"encoding": "base64", "bps": 40000, "data": "abcd"},
        {"encoding": "hex", "bps": 40000, "data": "xyz"},
        {"encoding": "cq", "bps": 4000, "data": "10Z"},
        {"encoding": "hex", "bps": 5, "data": "ff"},
        {"encoding": "cq", "bps": 4000, "data": "q" * 100},
    ],
)
def test_unsupported_signals(signal: dict) -> None:
    """Malformed captures raise instead of decoding to garbage."""
    with pytest.raises(UnsupportedSignalError):
        decode_frames(signal)


def test_frame_from_bits() -> None:
    """A frame parses into its fields."""
    frame = Frame.from_bits(frame_bits(REMOTE_ID, Command.REVERSE, 7, CONSTANT))
    assert (frame.remote_id, frame.button, frame.flag, frame.counter) == (
        REMOTE_ID,
        0x8,
        1,
        7,
    )
    assert frame.command is Command.REVERSE
    with pytest.raises(ValueError):
        Frame.from_bits("101")


def test_remote_id_text() -> None:
    """Remote ids are read as hex or binary and written as hex."""
    assert format_remote_id(0xA1) == "000A1"
    assert parse_remote_id("5a3c7") == 0x5A3C7
    assert parse_remote_id("0x5A3C7") == 0x5A3C7
    assert parse_remote_id(f"{0x5A3C7:020b}") == 0x5A3C7
    for bad in ("", "123456", "zzzzz", "1" * 21):
        with pytest.raises(ValueError):
            parse_remote_id(bad)


def test_argument_checks() -> None:
    """Out-of-range values are refused."""
    with pytest.raises(ValueError):
        frame_bits(1 << 20, Command.SPEED_1, 0, 8)
    with pytest.raises(ValueError):
        frame_bits(1, Command.SPEED_1, 8, 8)
    with pytest.raises(ValueError):
        frame_bits(1, Command.SPEED_1, 0, 16)
