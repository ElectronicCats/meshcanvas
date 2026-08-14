"""The 16-byte Meshtastic on-air header.

Verified against meshtastic/firmware origin/master @ 6de6c3d,
src/mesh/RadioInterface.h PacketHeader and RadioInterface::beginSending.

    offset  size  field
    0       4     to          u32 LE, 0xFFFFFFFF = broadcast
    4       4     from        u32 LE
    8       4     id          u32 LE
    12      1     flags
    13      1     channel     channel hash
    14      1     next_hop    last byte of the next hop's node num
    15      1     relay_node  last byte of the relaying node's node num

The firmware memcpy's the struct straight to the radio with no byte swapping, so
the wire order is host order, and every supported target is little-endian.

The header travels in the clear and is not authenticated. Only the payload is
encrypted, and AES-CTR carries no MAC, so nothing here is tamper-evident.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

BROADCAST_ADDR = 0xFFFFFFFF

HEADER_LENGTH = 16
_HEADER_STRUCT = struct.Struct("<IIIBBBB")

# src/mesh/RadioInterface.h
PACKET_FLAGS_HOP_LIMIT_MASK = 0x07
PACKET_FLAGS_WANT_ACK_MASK = 0x08
PACKET_FLAGS_VIA_MQTT_MASK = 0x10
PACKET_FLAGS_HOP_START_MASK = 0xE0
PACKET_FLAGS_HOP_START_SHIFT = 5

# src/mesh/MeshTypes.h
HOP_MAX = 7
HOP_RELIABLE = 3
NO_NEXT_HOP_PREFERENCE = 0
NO_RELAY_NODE = 0


def last_byte_of_node_num(node_num: int) -> int:
    """Firmware: NodeDB::getLastByteOfNodeNum.

    A low byte of 0x00 maps to 0xFF because 0 is the "none" sentinel.
    """
    low = node_num & 0xFF
    return low if low else 0xFF


@dataclass
class PacketHeader:
    to: int = BROADCAST_ADDR
    sender: int = 0
    packet_id: int = 0
    hop_limit: int = HOP_RELIABLE
    want_ack: bool = False
    via_mqtt: bool = False
    hop_start: int = HOP_RELIABLE
    channel_hash: int = 0
    next_hop: int = NO_NEXT_HOP_PREFERENCE
    relay_node: int = NO_RELAY_NODE

    @property
    def flags(self) -> int:
        """Firmware: RadioInterface::beginSending.

        hop_limit is OR'd in unmasked after being clamped to HOP_MAX, so a value
        above 7 would corrupt want_ack and via_mqtt. We clamp for the same reason.
        """
        hop_limit = min(self.hop_limit, HOP_MAX)
        value = hop_limit
        if self.want_ack:
            value |= PACKET_FLAGS_WANT_ACK_MASK
        if self.via_mqtt:
            value |= PACKET_FLAGS_VIA_MQTT_MASK
        value |= (self.hop_start << PACKET_FLAGS_HOP_START_SHIFT) & PACKET_FLAGS_HOP_START_MASK
        return value & 0xFF

    def pack(self) -> bytes:
        for name, value in (
            ("to", self.to),
            ("sender", self.sender),
            ("packet_id", self.packet_id),
        ):
            if not 0 <= value <= 0xFFFFFFFF:
                raise ValueError(f"{name} out of u32 range: {value}")
        for name, value in (
            ("channel_hash", self.channel_hash),
            ("next_hop", self.next_hop),
            ("relay_node", self.relay_node),
        ):
            if not 0 <= value <= 0xFF:
                raise ValueError(f"{name} out of u8 range: {value}")

        return _HEADER_STRUCT.pack(
            self.to,
            self.sender,
            self.packet_id,
            self.flags,
            self.channel_hash,
            self.next_hop,
            self.relay_node,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "PacketHeader":
        if len(data) < HEADER_LENGTH:
            raise ValueError(f"header needs {HEADER_LENGTH} bytes, got {len(data)}")

        to, sender, packet_id, flags, channel, next_hop, relay_node = _HEADER_STRUCT.unpack(
            data[:HEADER_LENGTH]
        )
        hop_start = (flags & PACKET_FLAGS_HOP_START_MASK) >> PACKET_FLAGS_HOP_START_SHIFT

        # Firmware: when hop_start is 0 the packet predates 2.3 and the two
        # routing bytes are meaningless.
        return cls(
            to=to,
            sender=sender,
            packet_id=packet_id,
            hop_limit=flags & PACKET_FLAGS_HOP_LIMIT_MASK,
            want_ack=bool(flags & PACKET_FLAGS_WANT_ACK_MASK),
            via_mqtt=bool(flags & PACKET_FLAGS_VIA_MQTT_MASK),
            hop_start=hop_start,
            channel_hash=channel,
            next_hop=next_hop if hop_start else NO_NEXT_HOP_PREFERENCE,
            relay_node=relay_node if hop_start else NO_RELAY_NODE,
        )
