"""The contract every transmit backend implements.

Backends take finished frame bytes. They know nothing about protobufs, channels
or shapes, which is what lets the null backend stand in as a complete test rig
for every layer above.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


class TransmitError(RuntimeError):
    """A frame could not be transmitted. Carries the backend's own message."""


@dataclass(frozen=True)
class RadioParams:
    """Physical layer settings for one session."""

    frequency_hz: int
    bandwidth_khz: float
    spreading_factor: int
    coding_rate: int
    tx_power_dbm: int
    preamble_symbols: int = 16
    sync_word: int = 0x2B


@dataclass
class TransmitResult:
    frame: bytes
    airtime_ms: int
    detail: str = ""


@runtime_checkable
class RadioBackend(Protocol):
    """Implemented by null, catsniffer and mqtt backends."""

    name: str

    def configure(self, params: RadioParams) -> None:
        """Apply physical layer settings. Must be called before transmit()."""

    def transmit(self, frame: bytes) -> TransmitResult:
        """Send one frame. Raises TransmitError on failure."""

    def close(self) -> None:
        """Release the port or connection. Safe to call more than once."""


@dataclass
class RecordingBackend:
    """Base for backends that keep what they were asked to send."""

    name: str = "recording"
    params: RadioParams | None = None
    sent: list[bytes] = field(default_factory=list)

    def configure(self, params: RadioParams) -> None:
        self.params = params

    def require_configured(self) -> RadioParams:
        if self.params is None:
            raise TransmitError(f"{self.name} backend was not configured")
        return self.params

    def close(self) -> None:
        return None
