"""The 16-byte on-air header."""

import struct

import pytest

from meshcanvas.protocol.header import (
    BROADCAST_ADDR,
    HEADER_LENGTH,
    HOP_MAX,
    NO_NEXT_HOP_PREFERENCE,
    NO_RELAY_NODE,
    PacketHeader,
    last_byte_of_node_num,
)


def test_header_is_exactly_16_bytes():
    assert len(PacketHeader(sender=0x1234ABCD, packet_id=7).pack()) == HEADER_LENGTH


def test_field_order_and_little_endian_layout():
    header = PacketHeader(
        to=BROADCAST_ADDR,
        sender=0x1234ABCD,
        packet_id=0xDEADBEEF,
        hop_limit=3,
        hop_start=3,
        channel_hash=0x08,
    )
    raw = header.pack()

    assert raw[0:4] == b"\xff\xff\xff\xff"
    assert raw[4:8] == struct.pack("<I", 0x1234ABCD)
    assert raw[8:12] == struct.pack("<I", 0xDEADBEEF)
    assert raw[13] == 0x08


class TestFlags:
    def test_hop_limit_occupies_the_low_three_bits(self):
        assert PacketHeader(hop_limit=5, hop_start=0).flags & 0x07 == 5

    def test_want_ack_is_bit_3(self):
        assert PacketHeader(hop_limit=0, hop_start=0, want_ack=True).flags == 0x08

    def test_via_mqtt_is_bit_4(self):
        assert PacketHeader(hop_limit=0, hop_start=0, via_mqtt=True).flags == 0x10

    def test_hop_start_occupies_the_top_three_bits(self):
        assert PacketHeader(hop_limit=0, hop_start=3).flags == 3 << 5

    def test_all_flags_combine_without_overlap(self):
        header = PacketHeader(
            hop_limit=3, hop_start=7, want_ack=True, via_mqtt=True
        )
        assert header.flags == 3 | 0x08 | 0x10 | (7 << 5)

    def test_hop_limit_above_max_is_clamped_not_wrapped(self):
        # Firmware ORs hop_limit in unmasked after clamping to HOP_MAX. Without
        # the clamp a hop_limit of 8 would set the want_ack bit instead.
        header = PacketHeader(hop_limit=9, hop_start=0, want_ack=False)
        assert header.flags & 0x07 == HOP_MAX
        assert not header.flags & 0x08


class TestRoundTrip:
    def test_unpack_recovers_every_field(self):
        original = PacketHeader(
            to=0x11223344,
            sender=0x55667788,
            packet_id=0x99AABBCC,
            hop_limit=3,
            hop_start=5,
            want_ack=True,
            via_mqtt=True,
            channel_hash=0x08,
            next_hop=0x42,
            relay_node=0x99,
        )
        assert PacketHeader.unpack(original.pack()) == original

    def test_zero_hop_start_invalidates_the_routing_bytes(self):
        # Firmware treats next_hop and relay_node as meaningless when hop_start
        # is 0, because the packet predates firmware 2.3.
        raw = PacketHeader(
            hop_limit=3, hop_start=0, next_hop=0x42, relay_node=0x99
        ).pack()
        decoded = PacketHeader.unpack(raw)
        assert decoded.next_hop == NO_NEXT_HOP_PREFERENCE
        assert decoded.relay_node == NO_RELAY_NODE

    def test_short_buffer_is_rejected(self):
        with pytest.raises(ValueError, match="16 bytes"):
            PacketHeader.unpack(b"\x00" * 15)

    def test_trailing_payload_is_ignored(self):
        raw = PacketHeader(sender=1, packet_id=2).pack() + b"payload"
        assert PacketHeader.unpack(raw).sender == 1


class TestRangeChecks:
    @pytest.mark.parametrize("field", ["to", "sender", "packet_id"])
    def test_u32_overflow_is_rejected(self, field):
        header = PacketHeader()
        setattr(header, field, 0x1_0000_0000)
        with pytest.raises(ValueError, match="u32"):
            header.pack()

    @pytest.mark.parametrize("field", ["channel_hash", "next_hop", "relay_node"])
    def test_u8_overflow_is_rejected(self, field):
        header = PacketHeader()
        setattr(header, field, 256)
        with pytest.raises(ValueError, match="u8"):
            header.pack()


class TestLastByteOfNodeNum:
    def test_takes_the_low_byte(self):
        assert last_byte_of_node_num(0xDEADBE42) == 0x42

    def test_a_zero_low_byte_becomes_0xff(self):
        # 0 is the "none" sentinel, so the firmware substitutes 0xFF.
        assert last_byte_of_node_num(0xDEADBE00) == 0xFF
