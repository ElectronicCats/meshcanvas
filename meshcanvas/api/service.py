"""Shape rendering, airtime budgeting and the transmit run loop.

Framework free on purpose: everything here is callable from a test without
starting a server.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from meshcanvas.api.models import (
    BudgetResponse,
    RadioSettings,
    RenderRequest,
    Shape,
    TransmitRequest,
)
from meshcanvas.geometry import shape_to_latlon
from meshcanvas.geometry.raster import (
    circle_bitmap,
    grid_bitmap,
    image_to_bitmap,
    latlon_paths_to_bitmap,
    star_bitmap,
    text_to_bitmap,
)
from meshcanvas.protocol.channel import channel_hash
from meshcanvas.protocol.frequency import (
    HEADER_LENGTH,
    PRESET_DISPLAY_NAMES,
    ModemPreset,
    channel_number,
    frequency_hz,
    min_interval_ms,
    preset_params,
    time_on_air_ms,
)
from meshcanvas.protocol.packet import (
    build_frame,
    generate_nodes,
    nodeinfo_payload,
    packet_id_for,
    position_payload,
    write_session_csv,
)
from meshcanvas.radio.base import RadioParams, TransmitError
from meshcanvas.radio.null import NullBackend

# A synthetic node is announced with NodeInfo, then placed with Position.
FRAMES_PER_NODE = 2

# Default share of airtime to occupy, capped further by the region limit.
# US and ANZ permit 100 percent, and pacing to that means keying the transmitter
# continuously for the whole run, which starves every other node in range even
# where it is legal. Half is still fast and leaves the channel usable.
DEFAULT_AIRTIME_TARGET_PERCENT = 50.0


def effective_duty_cycle(region_limit: float, target: float | None = None) -> float:
    """The region limit is a hard ceiling; the target only ever lowers it."""
    wanted = DEFAULT_AIRTIME_TARGET_PERCENT if target is None else target
    return min(region_limit, wanted)


class AbortedError(RuntimeError):
    """The run was cancelled by /api/abort."""


def decode_psk(psk_base64: str | None) -> bytes:
    """An unset PSK means the default public channel shorthand, matching a
    stock node. An empty string means no encryption."""
    if psk_base64 is None:
        return b"\x01"
    if psk_base64 == "":
        return b""
    try:
        return base64.b64decode(psk_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"psk_base64 is not valid base64: {exc}") from None


def build_bitmap(shape: Shape, center_lat: float = 0.0) -> np.ndarray:
    if shape.type in ("polygon", "freehand"):
        paths = shape.paths or ([shape.vertices] if shape.vertices else [])
        paths = [p for p in paths if p]
        fill = shape.type == "polygon"
        minimum = 3 if fill else 2

        if not paths:
            raise ValueError(f"a {shape.type} needs at least one path")
        if not any(len(p) >= minimum for p in paths):
            raise ValueError(
                f"a {shape.type} needs at least {minimum} points in a path"
            )

        # Points arrive as (latitude, longitude) from the map client, not as
        # normalized image coordinates. A freehand trace is stroked rather than
        # filled so a squiggle stays a squiggle, and its strokes share one
        # bounding box so they keep their positions relative to each other.
        return latlon_paths_to_bitmap(
            [p for p in paths if len(p) >= minimum], center_lat, fill=fill
        )
    if shape.type == "text":
        if not (shape.text or "").strip():
            raise ValueError("text shape needs a non-empty string")
        return text_to_bitmap(shape.text)
    if shape.type == "image":
        if shape.image is None:
            raise ValueError("image shape needs an image payload")
        try:
            raw = base64.b64decode(shape.image.data_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"image data is not valid base64: {exc}") from None
        return image_to_bitmap(io.BytesIO(raw))
    if shape.type == "circle":
        return circle_bitmap()
    if shape.type == "grid":
        return grid_bitmap(rows=shape.rows, cols=shape.cols)
    if shape.type == "star":
        return star_bitmap(points=shape.points)
    raise ValueError(f"unknown shape type {shape.type!r}")


def render_points(request: RenderRequest) -> list[tuple[float, float]]:
    bitmap = build_bitmap(request.shape, center_lat=request.center[0])
    active = int(np.count_nonzero(bitmap))
    if active < request.node_count:
        raise ValueError(
            f"shape has {active} active pixels but {request.node_count} nodes were "
            "requested; draw a larger shape or lower the node count"
        )
    return shape_to_latlon(
        bitmap,
        request.node_count,
        request.center[0],
        request.center[1],
        request.scale_m,
        seed=abs(hash(request.seed)) % (2**32),
    )


def radio_params(settings: RadioSettings) -> RadioParams:
    region = settings.region_or_raise()
    preset = settings.preset_or_raise()
    params = preset_params(preset, wide_lora=region.wide_lora)
    name = settings.channel_name or PRESET_DISPLAY_NAMES[preset]

    power = min(settings.tx_power_dbm, region.power_limit)
    return RadioParams(
        frequency_hz=frequency_hz(
            name, region, params.bandwidth_khz, settings.channel_num
        ),
        bandwidth_khz=params.bandwidth_khz,
        spreading_factor=params.spreading_factor,
        coding_rate=params.coding_rate,
        tx_power_dbm=power,
    )


def compute_budget(settings: RadioSettings, node_count: int,
                   inter_packet_ms: int | None = None,
                   airtime_target_percent: float | None = None) -> BudgetResponse:
    """Airtime and duty cycle for a whole run, before anything is sent."""
    region = settings.region_or_raise()
    preset = settings.preset_or_raise()
    params = preset_params(preset, wide_lora=region.wide_lora)
    name = settings.channel_name or PRESET_DISPLAY_NAMES[preset]
    psk = decode_psk(settings.psk_base64)

    toa_kwargs = dict(
        spreading_factor=params.spreading_factor,
        bandwidth_khz=params.bandwidth_khz,
        coding_rate=params.coding_rate,
    )
    # Representative payload sizes, measured from real frames.
    nodeinfo_ms = time_on_air_ms(HEADER_LENGTH + 34, **toa_kwargs)
    position_ms = time_on_air_ms(HEADER_LENGTH + 26, **toa_kwargs)

    packets = node_count * FRAMES_PER_NODE
    total_ms = node_count * (nodeinfo_ms + position_ms)
    mean_ms = total_ms / packets if packets else 0

    target = effective_duty_cycle(region.duty_cycle, airtime_target_percent)
    floor_ms = min_interval_ms(mean_ms, target)
    gap_ms = int(inter_packet_ms if inter_packet_ms is not None else round(floor_ms))

    elapsed_ms = gap_ms * packets
    duty = (total_ms / elapsed_ms * 100.0) if elapsed_ms else 100.0

    return BudgetResponse(
        frequency_hz=frequency_hz(
            name, region, params.bandwidth_khz, settings.channel_num
        ),
        frequency_mhz=frequency_hz(
            name, region, params.bandwidth_khz, settings.channel_num
        ) / 1e6,
        channel_slot=channel_number(
            name, region, params.bandwidth_khz, settings.channel_num
        ),
        spreading_factor=params.spreading_factor,
        bandwidth_khz=params.bandwidth_khz,
        coding_rate=params.coding_rate,
        packet_count=packets,
        toa_ms_per_packet=round(mean_ms),
        nodeinfo_toa_ms=nodeinfo_ms,
        position_toa_ms=position_ms,
        total_airtime_ms=total_ms,
        inter_packet_ms=gap_ms,
        eta_seconds=elapsed_ms / 1000.0,
        duty_cycle_percent=round(duty, 3),
        airtime_target_percent=target,
        region_duty_cycle_limit=region.duty_cycle,
        region_power_limit_dbm=region.power_limit,
        tx_power_dbm=min(settings.tx_power_dbm, region.power_limit),
        within_duty_cycle=duty <= region.duty_cycle + 1e-9,
        channel_hash=channel_hash(name, psk, PRESET_DISPLAY_NAMES[preset]),
    )


def make_backend(request: TransmitRequest):
    if request.mode == "dry-run":
        return NullBackend()
    if request.mode == "rf":
        from meshcanvas.radio.catsniffer import CatSnifferBackend

        return CatSnifferBackend()
    if request.mode == "mqtt":
        from meshcanvas.radio.mqtt import MqttBackend

        return MqttBackend(host=request.mqtt_host, port=request.mqtt_port)
    raise ValueError(f"unknown mode {request.mode!r}")


@dataclass
class RunState:
    """One transmit run. Only one runs at a time."""

    aborted: bool = False
    running: bool = False
    sent: int = 0
    total: int = 0
    session_csv: Path | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def abort(self) -> None:
        self.aborted = True


async def run_transmit(
    request: TransmitRequest,
    state: RunState,
    emit,
    backend=None,
    session_dir: Path | None = None,
) -> None:
    """Render, build frames and transmit, reporting progress through emit().

    emit is an async callable taking one JSON-serializable dict.
    """
    state.aborted = False
    state.running = True
    state.sent = 0

    try:
        budget = compute_budget(
            request, request.node_count, request.inter_packet_ms,
            request.airtime_target_percent,
        )

        if not budget.within_duty_cycle and not request.duty_cycle_override:
            raise ValueError(
                f"projected duty cycle {budget.duty_cycle_percent}% exceeds the "
                f"{budget.region_duty_cycle_limit}% limit for {request.region}. "
                "Increase inter_packet_ms or set duty_cycle_override."
            )

        points = render_points(request)
        nodes = generate_nodes(
            points, seed=request.seed, prefix=request.node_prefix
        )
        state.total = len(nodes) * (FRAMES_PER_NODE if request.send_nodeinfo else 1)

        directory = session_dir or Path("sessions")
        state.session_csv = directory / f"session-{int(time.time())}.csv"
        write_session_csv(nodes, state.session_csv)
        await emit({
            "type": "log", "level": "info",
            "message": f"node map written to {state.session_csv}",
        })

        params = radio_params(request)
        backend = backend if backend is not None else make_backend(request)
        psk = decode_psk(request.psk_base64)
        preset_name = PRESET_DISPLAY_NAMES[request.preset_or_raise()]

        await emit({
            "type": "log", "level": "info",
            "message": (
                f"mode={request.mode} freq={params.frequency_hz / 1e6:.3f}MHz "
                f"SF{params.spreading_factor} BW{params.bandwidth_khz:g} "
                f"CR4/{params.coding_rate} power={params.tx_power_dbm}dBm "
                f"channel_hash=0x{budget.channel_hash:02x}"
            ),
        })

        backend.configure(params)
        gap_s = budget.inter_packet_ms / 1000.0

        try:
            for node in nodes:
                payloads = []
                if request.send_nodeinfo:
                    payloads.append(("nodeinfo", nodeinfo_payload(node)))
                payloads.append((
                    "position",
                    position_payload(node, precision_bits=request.precision_bits),
                ))

                for sequence, (kind, payload) in enumerate(payloads):
                    if state.aborted:
                        raise AbortedError("aborted by request")

                    frame = build_frame(
                        payload,
                        sender=node.node_num,
                        packet_id=packet_id_for(node.node_num, sequence, request.seed),
                        channel_name=request.channel_name,
                        psk=psk,
                        preset_name=preset_name,
                        hop_limit=request.hop_limit,
                    )
                    result = backend.transmit(frame)
                    state.sent += 1

                    await emit({
                        "type": "progress",
                        "sent": state.sent,
                        "total": state.total,
                        "node_id": node.node_id,
                        "kind": kind,
                        "airtime_ms": result.airtime_ms,
                    })
                    await asyncio.sleep(gap_s)
        finally:
            backend.close()

        await emit({
            "type": "done",
            "sent": state.sent,
            "message": f"sent {state.sent} frames, session map at {state.session_csv}",
        })

    except AbortedError as exc:
        await emit({"type": "done", "sent": state.sent, "message": str(exc)})
    except (ValueError, TransmitError) as exc:
        await emit({"type": "error", "message": str(exc)})
    finally:
        state.running = False
