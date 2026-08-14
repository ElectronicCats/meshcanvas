"""Channel hash and PSK expansion, checked against known firmware values.

The anchor vector is the real-world LongFast channel hash 0x08. It is the byte
every stock Meshtastic node in the world puts in its header, so reproducing it
proves the name expansion, the PSK expansion and the XOR are all correct at once.
"""

import pytest

from meshcanvas.protocol.channel import (
    DEFAULT_PSK,
    channel_hash,
    djb2,
    expand_psk,
    xor_hash,
)


def test_xor_hash_of_longfast_name():
    assert xor_hash(b"LongFast") == 0x0A


def test_xor_hash_of_default_psk():
    assert xor_hash(DEFAULT_PSK) == 0x02


def test_longfast_default_channel_hash_is_0x08():
    # The canonical public channel: empty name expands to the preset display
    # name, PSK shorthand byte 1 expands to DEFAULT_PSK.
    assert channel_hash("", psk=b"\x01", preset_name="LongFast") == 0x08
    assert channel_hash("LongFast", psk=DEFAULT_PSK) == 0x08


@pytest.mark.parametrize(
    "name,expected",
    [("MediumSlow", 0x18), ("ShortFast", 0x70)],
)
def test_other_default_preset_hashes(name, expected):
    assert channel_hash(name, psk=DEFAULT_PSK) == expected


class TestPskExpansion:
    def test_empty_psk_means_no_encryption(self):
        assert expand_psk(b"") == b""

    def test_shorthand_zero_disables_encryption(self):
        assert expand_psk(b"\x00") == b""

    def test_shorthand_one_is_the_default_psk(self):
        assert expand_psk(b"\x01") == DEFAULT_PSK

    def test_shorthand_n_bumps_the_last_byte(self):
        # index of 1 means no change vs defaultpsk, so index 2 adds 1.
        assert expand_psk(b"\x02") == DEFAULT_PSK[:-1] + bytes([DEFAULT_PSK[-1] + 1])

    def test_shorthand_wraps_the_last_byte_like_uint8(self):
        # defaultpsk ends in 0x01, so index 256 would overflow a uint8.
        assert expand_psk(bytes([0xFF]))[-1] == (DEFAULT_PSK[-1] + 0xFE) & 0xFF

    def test_short_key_is_zero_padded_to_16(self):
        assert expand_psk(b"\xaa" * 4) == b"\xaa" * 4 + b"\x00" * 12

    def test_16_byte_key_is_used_as_is(self):
        key = bytes(range(16))
        assert expand_psk(key) == key

    def test_between_17_and_31_is_zero_padded_to_32(self):
        assert expand_psk(b"\xbb" * 20) == b"\xbb" * 20 + b"\x00" * 12

    def test_32_byte_key_is_used_as_is(self):
        key = bytes(range(32))
        assert expand_psk(key) == key


def test_channel_hash_uses_the_expanded_key_not_the_raw_psk():
    # If the implementation XOR'd the raw 1-byte shorthand instead of expanding
    # it, this would come out as xor_hash(b"LongFast") ^ 0x01 == 0x0b.
    assert channel_hash("LongFast", psk=b"\x01") == 0x08


def test_unencrypted_channel_hash_ignores_the_key():
    assert channel_hash("LongFast", psk=b"\x00") == xor_hash(b"LongFast")


class TestDjb2:
    """The frequency-slot hash. Distinct from the channel hash above."""

    def test_empty_string_is_the_seed(self):
        assert djb2("") == 5381

    def test_single_char(self):
        assert djb2("a") == 5381 * 33 + ord("a")

    def test_stays_in_uint32(self):
        assert djb2("LongFast" * 20) <= 0xFFFFFFFF

    def test_is_not_the_channel_hash(self):
        # Guards the single most costly confusion in this codebase: using the
        # header hash for slot selection puts the radio on the wrong frequency.
        assert djb2("LongFast") != channel_hash("LongFast", psk=DEFAULT_PSK)
