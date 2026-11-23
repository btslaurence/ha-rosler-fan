"""Helpers shared by the tests."""

from __future__ import annotations

import random
from typing import Any

from custom_components.rosler_fan.protocol import Command, frame_bits

# An arbitrary id used only in tests.
REMOTE_ID = 0x5A3C7
CONSTANT = 8


def render_scan(
    frames: list[str],
    *,
    bps: int = 40000,
    clock: float = 1.0,
    lead_us: float = 20000,
    gap_us: float = 7750,
    tail_us: float = 30000,
    glitches: int = 0,
    seed: int = 1,
    cut_start_us: float = 0,
    stray_us: float = 0,
    stray_before_us: float = 1000,
) -> dict[str, Any]:
    """Render frames the way a Bond scan returns them: raw samples as hex.

    clock scales every duration, to imitate a remote running fast or slow.
    """
    rng = random.Random(seed)
    sample_us = 1_000_000 / bps
    samples: list[int] = []

    def emit(level: int, duration_us: float) -> None:
        samples.extend([level] * round(duration_us * clock / sample_us))

    emit(0, lead_us)
    for index, bits in enumerate(frames):
        if stray_us:
            # A noise pulse shortly before the frame, as a receiver's gain
            # control produces in silence.
            del samples[-round((stray_us + stray_before_us) / sample_us) :]
            emit(1, stray_us)
            emit(0, stray_before_us)
        for bit in bits:
            emit(1, 750 if bit == "1" else 250)
            emit(0, 250 if bit == "1" else 750)
        if index < len(frames) - 1:
            emit(0, gap_us - 750)
    emit(0, tail_us)

    if cut_start_us:
        samples = samples[round(cut_start_us / sample_us) :]
    for _ in range(glitches):
        position = rng.randrange(len(samples) - 2)
        flipped = 1 - samples[position]
        samples[position] = flipped
        if rng.random() < 0.5:
            samples[position + 1] = flipped

    while len(samples) % 4:
        samples.append(0)
    data = "".join(
        f"{int(''.join(map(str, samples[i : i + 4])), 2):x}"
        for i in range(0, len(samples), 4)
    )
    return {
        "freq": 447885,
        "modulation": "OOK",
        "encoding": "hex",
        "bps": bps,
        "reps": 1,
        "data": data,
    }


def press(
    command: Command,
    counter: int,
    *,
    remote_id: int = REMOTE_ID,
    constant: int = CONSTANT,
    repeats: int = 7,
    **kwargs: Any,
) -> dict[str, Any]:
    """Return the scan result of one button press."""
    bits = frame_bits(remote_id, command, counter, constant)
    return render_scan([bits] * repeats, **kwargs)
