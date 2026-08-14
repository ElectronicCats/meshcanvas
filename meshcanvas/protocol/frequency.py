"""LoRa regions, modem presets, frequency slot selection and time on air.

Verified against meshtastic/firmware origin/master @ 6de6c3d:
  src/mesh/RadioInterface.cpp  regions[] table, applyModemConfig()
  src/mesh/MeshRadio.h         modemPresetToParams()
  src/mesh/RadioLibInterface.h getPacketConfig(), computePacketTime()
and against RadioLib @ 3c960d0 SX126x::calculateTimeOnAir().

The slot math runs in float32 because the firmware does. On the regions checked
float64 agrees, but a band whose width lands near an exact multiple of the
bandwidth can round the other way and shift every node onto a dead frequency.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

import numpy as np

# The firmware's on-air constants. src/mesh/RadioLibInterface.h and RadioInterface.h.
SYNC_WORD = 0x2B
PREAMBLE_SYMBOLS = 16
PREAMBLE_SYMBOLS_WIDE_LORA = 12
HEADER_LENGTH = 16
MAX_LORA_PAYLOAD_LEN = 255
MAX_ENCRYPTED_PAYLOAD = MAX_LORA_PAYLOAD_LEN - HEADER_LENGTH  # 239


@dataclass(frozen=True)
class Region:
    name: str
    freq_start: float  # MHz
    freq_end: float  # MHz
    duty_cycle: float  # percent
    spacing: float  # MHz, 0 for every region in master
    power_limit: int  # dBm
    wide_lora: bool = False


# src/mesh/RadioInterface.cpp regions[]. Values are quoted from the RDEF rows.
REGIONS: dict[str, Region] = {
    r.name: r
    for r in [
        Region("US", 902.0, 928.0, 100, 0, 30),
        Region("EU_433", 433.0, 434.0, 10, 0, 10),
        Region("EU_868", 869.4, 869.65, 10, 0, 27),
        Region("CN", 470.0, 510.0, 100, 0, 19),
        Region("JP", 920.5, 923.5, 100, 0, 13),
        Region("ANZ", 915.0, 928.0, 100, 0, 30),
        Region("ANZ_433", 433.05, 434.79, 100, 0, 14),
        Region("RU", 868.7, 869.2, 100, 0, 20),
        Region("KR", 920.0, 923.0, 100, 0, 23),
        Region("TW", 920.0, 925.0, 100, 0, 27),
        Region("IN", 865.0, 867.0, 100, 0, 30),
        Region("NZ_865", 864.0, 868.0, 100, 0, 36),
        Region("TH", 920.0, 925.0, 10, 0, 27),
        Region("UA_433", 433.0, 434.7, 10, 0, 10),
        Region("UA_868", 868.0, 868.6, 1, 0, 14),
        Region("MY_433", 433.0, 435.0, 100, 0, 20),
        Region("MY_919", 919.0, 924.0, 100, 0, 27),
        Region("SG_923", 917.0, 925.0, 100, 0, 20),
        Region("PH_433", 433.0, 434.7, 100, 0, 10),
        Region("PH_868", 868.0, 869.4, 100, 0, 14),
        Region("PH_915", 915.0, 918.0, 100, 0, 24),
        Region("KZ_433", 433.075, 434.775, 100, 0, 10),
        Region("KZ_863", 863.0, 868.0, 100, 0, 30),
        Region("NP_865", 865.0, 868.0, 100, 0, 30),
        Region("BR_902", 902.0, 907.5, 100, 0, 30),
        Region("LORA_24", 2400.0, 2483.5, 100, 0, 10, wide_lora=True),
        Region("UNSET", 902.0, 928.0, 100, 0, 30),
    ]
}


class ModemPreset(str, Enum):
    LONG_FAST = "LONG_FAST"
    LONG_SLOW = "LONG_SLOW"
    LONG_MODERATE = "LONG_MODERATE"
    LONG_TURBO = "LONG_TURBO"
    MEDIUM_FAST = "MEDIUM_FAST"
    MEDIUM_SLOW = "MEDIUM_SLOW"
    SHORT_FAST = "SHORT_FAST"
    SHORT_SLOW = "SHORT_SLOW"
    SHORT_TURBO = "SHORT_TURBO"


@dataclass(frozen=True)
class ModemParams:
    bandwidth_khz: float
    spreading_factor: int
    coding_rate: int  # denominator, 5 means 4/5


# src/mesh/MeshRadio.h modemPresetToParams(). (narrow, wide) bandwidth pairs.
_PRESETS: dict[ModemPreset, tuple[float, float, int, int]] = {
    ModemPreset.SHORT_TURBO: (500.0, 1625.0, 7, 5),
    ModemPreset.SHORT_FAST: (250.0, 812.5, 7, 5),
    ModemPreset.SHORT_SLOW: (250.0, 812.5, 8, 5),
    ModemPreset.MEDIUM_FAST: (250.0, 812.5, 9, 5),
    ModemPreset.MEDIUM_SLOW: (250.0, 812.5, 10, 5),
    ModemPreset.LONG_TURBO: (500.0, 1625.0, 11, 8),
    ModemPreset.LONG_MODERATE: (125.0, 406.25, 11, 8),
    ModemPreset.LONG_SLOW: (125.0, 406.25, 12, 8),
    ModemPreset.LONG_FAST: (250.0, 812.5, 11, 5),
}

# src/graphics/DisplayFormatters.cpp getModemPresetDisplayName(useShortName=false).
# These strings are what an empty channel name expands to, so they feed both the
# channel hash and the frequency slot. LONG_MODERATE is "LongMod", not
# "LongModerate".
PRESET_DISPLAY_NAMES: dict[ModemPreset, str] = {
    ModemPreset.LONG_FAST: "LongFast",
    ModemPreset.LONG_SLOW: "LongSlow",
    ModemPreset.LONG_MODERATE: "LongMod",
    ModemPreset.LONG_TURBO: "LongTurbo",
    ModemPreset.MEDIUM_FAST: "MediumFast",
    ModemPreset.MEDIUM_SLOW: "MediumSlow",
    ModemPreset.SHORT_FAST: "ShortFast",
    ModemPreset.SHORT_SLOW: "ShortSlow",
    ModemPreset.SHORT_TURBO: "ShortTurbo",
}


def preset_params(preset: ModemPreset, wide_lora: bool = False) -> ModemParams:
    narrow_bw, wide_bw, sf, cr = _PRESETS[preset]
    return ModemParams(wide_bw if wide_lora else narrow_bw, sf, cr)


def num_channels(region: Region, bandwidth_khz: float) -> int:
    """Number of frequency slots in a region. Firmware: applyModemConfig().

        numChannels = floor((freqEnd - freqStart) / (spacing + bw/1000))
    """
    f32 = np.float32
    span = f32(region.freq_end) - f32(region.freq_start)
    step = f32(region.spacing) + (f32(bandwidth_khz) / f32(1000))
    if step <= 0:
        raise ValueError("bandwidth must be positive")
    return int(math.floor(float(span / step)))


def channel_number(
    channel_name: str,
    region: Region,
    bandwidth_khz: float,
    manual_channel_num: int | None = None,
) -> int:
    """Zero-based frequency slot.

    Uses djb2 over the channel name, NOT the header channel hash. The manual
    override is one-based in the config, matching loraConfig.channel_num.
    """
    from meshcanvas.protocol.channel import djb2

    total = num_channels(region, bandwidth_khz)
    if total <= 0:
        raise ValueError(
            f"region {region.name} is narrower than {bandwidth_khz} kHz; "
            "the firmware would fall back to LongFast here"
        )
    raw = (manual_channel_num - 1) if manual_channel_num else djb2(channel_name)
    return raw % total


def frequency_hz(
    channel_name: str,
    region: Region,
    bandwidth_khz: float,
    manual_channel_num: int | None = None,
    frequency_offset_hz: float = 0.0,
) -> int:
    """Center frequency in Hz. Firmware: applyModemConfig().

        freq = freqStart + (bw/2000) + channel_num * (bw/1000) + frequency_offset

    The bw/2000 term is half the bandwidth in MHz, which keeps slot 0 fully
    inside the band.

    The slot index is chosen in float32 to match the firmware, but this last
    step runs in float64. Firmware float32 puts EU_868 LongFast at 869.525024
    MHz rather than the documented 869.525; that 24 Hz is 25 SX1262 tuning steps
    and roughly a 360th of the +/-10 ppm a crystal drifts anyway, so it cannot
    affect whether a node decodes us. Float64 is used so the number we report
    and program is the published one.
    """
    slot = channel_number(channel_name, region, bandwidth_khz, manual_channel_num)
    freq_mhz = (
        region.freq_start
        + (bandwidth_khz / 2000.0)
        + (slot * (bandwidth_khz / 1000.0))
    )
    return int(round(freq_mhz * 1e6 + frequency_offset_hz))


def low_data_rate_optimize(spreading_factor: int, bandwidth_khz: float) -> bool:
    """On when symbol time reaches 16 ms. Firmware: getPacketConfig().

    Firmware writes (1 << sf) / bw >= 16 in integer-vs-float C; bw is kHz so the
    quotient is milliseconds.
    """
    return ((1 << spreading_factor) / bandwidth_khz) >= 16


def time_on_air_us(
    payload_len: int,
    spreading_factor: int,
    bandwidth_khz: float,
    coding_rate: int,
    preamble_symbols: int = PREAMBLE_SYMBOLS,
    crc_enabled: bool = True,
    implicit_header: bool = False,
    ldro: bool | None = None,
) -> int:
    """Time on air in microseconds, replicating RadioLib's integer arithmetic.

    RadioLib @ 3c960d0 SX126x::calculateTimeOnAir. The truncation is deliberate;
    a float model drifts from what the firmware and the chip actually schedule.

    payload_len is the full on-air frame including the 16-byte Meshtastic header,
    matching RadioInterface::getPacketTime.
    """
    if ldro is None:
        ldro = low_data_rate_optimize(spreading_factor, bandwidth_khz)

    # bandwidth is scaled by 10 so 812.5 kHz stays exact in integer arithmetic.
    symbol_length_us = (10_000 << spreading_factor) // int(bandwidth_khz * 10)

    sf_coeff1_x4 = 17  # 4.25 * 4
    sf_coeff2 = 8
    if spreading_factor in (5, 6):
        sf_coeff1_x4 = 25  # 6.25 * 4
        sf_coeff2 = 0

    sf_divisor = 4 * (spreading_factor - 2) if ldro else 4 * spreading_factor

    bits_per_crc = 16
    n_symbol_header = 0 if implicit_header else 20

    bit_count = (
        8 * payload_len
        + (bits_per_crc if crc_enabled else 0)
        - 4 * spreading_factor
        + sf_coeff2
        + n_symbol_header
    )
    bit_count = max(bit_count, 0)

    # Integer ceiling division, as RadioLib writes it.
    n_pre_coded_symbols = (bit_count + (sf_divisor - 1)) // sf_divisor

    n_symbol_x4 = (
        (preamble_symbols + 8) * 4 + sf_coeff1_x4 + n_pre_coded_symbols * coding_rate * 4
    )

    return (symbol_length_us * n_symbol_x4) // 4


def time_on_air_ms(payload_len: int, **kwargs) -> int:
    """Milliseconds, truncated. Firmware: computePacketTime() does /1000."""
    return time_on_air_us(payload_len, **kwargs) // 1000


def min_interval_ms(airtime_ms: float, duty_cycle_percent: float) -> float:
    """Shortest spacing between transmissions that stays inside the duty cycle."""
    if duty_cycle_percent <= 0:
        raise ValueError("duty cycle must be positive")
    return airtime_ms * (100.0 / duty_cycle_percent)
