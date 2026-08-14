"""Request and response shapes for the HTTP API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from meshcanvas.protocol.frequency import REGIONS, ModemPreset

Mode = Literal["dry-run", "mqtt", "rf"]
ShapeType = Literal["polygon", "freehand", "text", "image", "circle", "grid", "star"]


class ImagePayload(BaseModel):
    filename: str = ""
    mime: str = ""
    data_base64: str


class Shape(BaseModel):
    type: ShapeType
    # A single path, as (latitude, longitude) pairs from the map client.
    vertices: list[tuple[float, float]] | None = None
    # Several disjoint paths, for a freehand drawing made of separate strokes.
    # Takes precedence over vertices when present.
    paths: list[list[tuple[float, float]]] | None = None
    text: str | None = None
    image: ImagePayload | None = None
    # Primitive options.
    rows: int = 6
    cols: int = 6
    points: int = 5


class RenderRequest(BaseModel):
    shape: Shape
    center: tuple[float, float]
    scale_m: float = Field(default=1000.0, gt=0, le=200_000)
    node_count: int = Field(default=50, ge=1, le=2000)
    seed: str = "meshcanvas"


class RenderResponse(BaseModel):
    points: list[tuple[float, float]]
    node_count: int
    seed: str


class RadioSettings(BaseModel):
    region: str = "US"
    modem_preset: str = ModemPreset.LONG_FAST.value
    channel_name: str = "LongFast"
    psk_base64: str | None = None
    tx_power_dbm: int = 0
    # Manual frequency slot, one-based, matching loraConfig.channel_num. When
    # unset the slot is derived from djb2 of the channel name. A config that
    # pins a slot lands on a different frequency than the name would choose, and
    # the mismatch is silent.
    channel_num: int | None = Field(default=None, ge=1, le=256)
    hop_limit: int = 3
    precision_bits: int = 32
    node_prefix: int = Field(default=0x7F, ge=0, le=0xFF)

    def region_or_raise(self):
        if self.region not in REGIONS:
            raise ValueError(
                f"unknown region {self.region!r}; known: {', '.join(sorted(REGIONS))}"
            )
        return REGIONS[self.region]

    def preset_or_raise(self) -> ModemPreset:
        try:
            return ModemPreset(self.modem_preset)
        except ValueError:
            known = ", ".join(p.value for p in ModemPreset)
            raise ValueError(
                f"unknown modem preset {self.modem_preset!r}; known: {known}"
            ) from None


class TransmitRequest(RenderRequest, RadioSettings):
    mode: Mode = "dry-run"
    duty_cycle_override: bool = False
    airtime_target_percent: float | None = None
    # Two frames per node: NodeInfo then Position.
    send_nodeinfo: bool = True
    inter_packet_ms: int | None = None
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883


class BudgetResponse(BaseModel):
    frequency_hz: int
    frequency_mhz: float
    channel_slot: int
    spreading_factor: int
    bandwidth_khz: float
    coding_rate: int
    packet_count: int
    toa_ms_per_packet: int
    nodeinfo_toa_ms: int
    position_toa_ms: int
    total_airtime_ms: int
    inter_packet_ms: int
    eta_seconds: float
    duty_cycle_percent: float
    airtime_target_percent: float
    region_duty_cycle_limit: float
    region_power_limit_dbm: int
    tx_power_dbm: int
    within_duty_cycle: bool
    channel_hash: int
