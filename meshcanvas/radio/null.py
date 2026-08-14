"""The dry run backend: everything except the radio.

This is the default mode. It runs the full pipeline, records every frame and
computes the airtime each frame would have cost, but emits nothing. It is also
the test rig for every layer above the radio, so it has to be a complete stand
in for a real backend rather than a stub.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from meshcanvas.protocol.frequency import time_on_air_us
from meshcanvas.radio.base import RadioParams, RecordingBackend, TransmitResult


@dataclass
class NullBackend(RecordingBackend):
    """Records frames and airtime, transmits nothing."""

    name: str = "null"
    airtimes_us: list[int] = field(default_factory=list)

    def transmit(self, frame: bytes) -> TransmitResult:
        params = self.require_configured()
        airtime_us = _airtime_us(len(frame), params)

        self.sent.append(bytes(frame))
        self.airtimes_us.append(airtime_us)

        return TransmitResult(
            frame=bytes(frame),
            airtime_ms=airtime_us // 1000,
            detail=f"dry run, {len(frame)} bytes not transmitted",
        )

    @property
    def frame_count(self) -> int:
        return len(self.sent)

    @property
    def total_airtime_us(self) -> int:
        return sum(self.airtimes_us)

    @property
    def total_airtime_ms(self) -> int:
        """Truncated once, at the end.

        Summing the per-frame millisecond values instead would lose up to 1 ms
        per frame, and a duty cycle budget that under-reports is the direction
        that puts us over a regional limit.
        """
        return self.total_airtime_us // 1000

    def reset(self) -> None:
        self.sent.clear()
        self.airtimes_us.clear()


def _airtime_us(frame_len: int, params: RadioParams) -> int:
    return time_on_air_us(
        frame_len,
        spreading_factor=params.spreading_factor,
        bandwidth_khz=params.bandwidth_khz,
        coding_rate=params.coding_rate,
        preamble_symbols=params.preamble_symbols,
    )
