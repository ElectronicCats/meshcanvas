"""Frequency slot and airtime, checked against published Meshtastic values.

The three anchor frequencies below are documented on meshtastic.org, so they are
an external control on the formula rather than a restatement of it.
"""

import pytest

from meshcanvas.protocol.frequency import (
    REGIONS,
    ModemPreset,
    frequency_hz,
    low_data_rate_optimize,
    min_interval_ms,
    num_channels,
    preset_params,
    time_on_air_ms,
    time_on_air_us,
)
from meshcanvas.protocol.channel import djb2


class TestPublishedFrequencies:
    """Anchors: meshtastic.org radio-settings frequency slot tables."""

    def test_us_longfast_is_slot_20_at_906_875_mhz(self):
        region, bw = REGIONS["US"], 250.0
        assert num_channels(region, bw) == 104
        assert frequency_hz("LongFast", region, bw) == 906_875_000

    def test_eu868_longfast_is_the_only_slot_at_869_525_mhz(self):
        region, bw = REGIONS["EU_868"], 250.0
        assert num_channels(region, bw) == 1
        assert frequency_hz("LongFast", region, bw) == 869_525_000

    def test_eu433_longfast_is_slot_4_at_433_875_mhz(self):
        region, bw = REGIONS["EU_433"], 250.0
        assert num_channels(region, bw) == 4
        assert frequency_hz("LongFast", region, bw) == 433_875_000


class TestSlotSelection:
    def test_slot_uses_djb2_not_the_channel_hash(self):
        region, bw = REGIONS["US"], 250.0
        expected = djb2("LongFast") % 104
        assert expected == 19
        # slot 19 zero-based is the "Slot 20" of the docs, which are one-based.
        assert frequency_hz("LongFast", region, bw) == int(
            round((902.0 + 0.125 + 19 * 0.25) * 1e6)
        )

    def test_manual_channel_num_is_one_based(self):
        region, bw = REGIONS["US"], 250.0
        # Manual slot 1 is the bottom of the band.
        assert frequency_hz("ignored", region, bw, manual_channel_num=1) == 902_125_000

    def test_frequency_offset_is_applied_last(self):
        region, bw = REGIONS["US"], 250.0
        base = frequency_hz("LongFast", region, bw)
        shifted = frequency_hz("LongFast", region, bw, frequency_offset_hz=1500)
        assert shifted - base == 1500

    def test_a_different_channel_name_lands_on_a_different_slot(self):
        region, bw = REGIONS["US"], 250.0
        assert frequency_hz("LongFast", region, bw) != frequency_hz(
            "meshcanvas-lab", region, bw
        )

    def test_narrow_region_with_wide_bandwidth_is_rejected(self):
        # EU_868 is 250 kHz wide, so a 500 kHz preset yields zero slots. The
        # firmware silently reverts to LongFast here; we refuse instead of
        # transmitting on a frequency the user did not choose.
        with pytest.raises(ValueError, match="narrower"):
            frequency_hz("LongFast", REGIONS["EU_868"], 500.0)


class TestPresets:
    def test_longfast_is_sf11_bw250_cr45(self):
        params = preset_params(ModemPreset.LONG_FAST)
        assert (params.bandwidth_khz, params.spreading_factor, params.coding_rate) == (
            250.0,
            11,
            5,
        )

    def test_long_moderate_uses_125khz_and_cr48(self):
        params = preset_params(ModemPreset.LONG_MODERATE)
        assert (params.bandwidth_khz, params.spreading_factor, params.coding_rate) == (
            125.0,
            11,
            8,
        )

    def test_wide_lora_scales_bandwidth(self):
        assert preset_params(ModemPreset.LONG_FAST, wide_lora=True).bandwidth_khz == 812.5


class TestLowDataRateOptimize:
    def test_off_for_longfast(self):
        # 2048 / 250 = 8.192 ms symbol time, below the 16 ms threshold.
        assert low_data_rate_optimize(11, 250.0) is False

    def test_on_for_long_moderate(self):
        # 2048 / 125 = 16.384 ms.
        assert low_data_rate_optimize(11, 125.0) is True

    def test_on_for_long_slow(self):
        assert low_data_rate_optimize(12, 125.0) is True


class TestTimeOnAir:
    def test_symbol_time_matches_longfast(self):
        # A zero-payload frame is dominated by preamble plus fixed overhead.
        # Symbol time for SF11/BW250 is 8192 us; the result must be a whole
        # number of quarter-symbols.
        us = time_on_air_us(0, spreading_factor=11, bandwidth_khz=250.0, coding_rate=5)
        assert us % (8192 // 4) == 0

    def test_typical_position_frame_longfast(self):
        # 16-byte header plus a ~40-byte encrypted Data payload.
        ms = time_on_air_ms(
            56, spreading_factor=11, bandwidth_khz=250.0, coding_rate=5
        )
        # Sanity band: LongFast is ~1.07 kbps, so 56 bytes cannot be under 100 ms
        # nor over 1 s.
        assert 100 < ms < 1000

    def test_airtime_grows_with_payload(self):
        kw = dict(spreading_factor=11, bandwidth_khz=250.0, coding_rate=5)
        assert time_on_air_us(16, **kw) < time_on_air_us(128, **kw) < time_on_air_us(
            239, **kw
        )

    def test_slower_preset_takes_longer(self):
        fast = time_on_air_us(56, spreading_factor=7, bandwidth_khz=250.0, coding_rate=5)
        slow = time_on_air_us(56, spreading_factor=12, bandwidth_khz=125.0, coding_rate=8)
        assert slow > fast * 10

    def test_ldro_changes_the_result(self):
        kw = dict(spreading_factor=11, bandwidth_khz=125.0, coding_rate=8)
        assert time_on_air_us(56, ldro=True, **kw) != time_on_air_us(
            56, ldro=False, **kw
        )

    def test_returns_integers_not_floats(self):
        us = time_on_air_us(56, spreading_factor=11, bandwidth_khz=250.0, coding_rate=5)
        assert isinstance(us, int)

    def test_negative_bit_count_is_clamped(self):
        # A tiny payload at high SF drives the bit count negative before the
        # clamp; without it the symbol count would go backwards.
        us = time_on_air_us(1, spreading_factor=12, bandwidth_khz=125.0, coding_rate=8)
        assert us > 0


class TestDutyCycle:
    def test_eu868_ten_percent_needs_nine_times_the_airtime_as_gap(self):
        assert min_interval_ms(100.0, 10.0) == 1000.0

    def test_unrestricted_region_allows_back_to_back(self):
        assert min_interval_ms(100.0, 100.0) == 100.0

    def test_zero_duty_cycle_is_rejected(self):
        with pytest.raises(ValueError):
            min_interval_ms(100.0, 0.0)


class TestRegionTable:
    def test_eu868_is_the_narrow_band_not_863_to_870(self):
        region = REGIONS["EU_868"]
        assert (region.freq_start, region.freq_end) == (869.4, 869.65)
        assert region.duty_cycle == 10

    def test_anz_is_915_to_928(self):
        # The brief called this "ANZ865", which does not exist in the firmware.
        assert "ANZ865" not in REGIONS
        assert (REGIONS["ANZ"].freq_start, REGIONS["ANZ"].freq_end) == (915.0, 928.0)

    def test_power_limits_are_present_for_the_supported_regions(self):
        assert REGIONS["US"].power_limit == 30
        assert REGIONS["EU_868"].power_limit == 27
        assert REGIONS["ANZ"].power_limit == 30
