"""Assembly of complete Meshtastic frames for synthetic nodes.

A frame is: 16-byte cleartext header, then the AES-CTR ciphertext of a
serialized Data protobuf. Nothing here talks to a radio.

Synthetic node numbers carry a fixed high-byte prefix so anything this tool
injects can be identified and removed from a NodeDB afterwards. The mapping from
shape point to node number is deterministic given a seed, and is written to a
session CSV by the caller.
"""

from __future__ import annotations

import csv
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

from meshtastic.protobuf import mesh_pb2, portnums_pb2

from meshcanvas.protocol import crypto
from meshcanvas.protocol.channel import channel_hash, expand_psk
from meshcanvas.protocol.frequency import MAX_ENCRYPTED_PAYLOAD
from meshcanvas.protocol.header import (
    BROADCAST_ADDR,
    HOP_RELIABLE,
    PacketHeader,
)

# High byte of every synthetic node number. Real hardware derives its node num
# from the low bytes of its MAC, so a fixed high byte is not a guarantee of
# non-collision, only a clear marker. Configurable for that reason.
DEFAULT_NODE_PREFIX = 0x7F

# Reserved node numbers that must never be produced.
_RESERVED_NODE_NUMS = {0x00000000, 0xFFFFFFFF}


@dataclass(frozen=True)
class SyntheticNode:
    index: int
    node_num: int
    long_name: str
    short_name: str
    latitude: float
    longitude: float
    altitude: int = 0

    @property
    def node_id(self) -> str:
        """The "!hex" form Meshtastic uses for User.id."""
        return f"!{self.node_num:08x}"


def node_num_for(index: int, seed: str, prefix: int = DEFAULT_NODE_PREFIX) -> int:
    """Deterministic synthetic node number.

    Uses BLAKE2b rather than Python's hash() because hash() is randomized per
    process, which would make the session CSV useless for cleanup.
    """
    if not 0 <= prefix <= 0xFF:
        raise ValueError(f"prefix must be a single byte, got {prefix}")

    digest = hashlib.blake2b(
        f"{seed}:{index}".encode("utf-8"), digest_size=4
    ).digest()
    low = int.from_bytes(digest, "little") & 0x00FFFFFF
    node_num = (prefix << 24) | low

    if node_num in _RESERVED_NODE_NUMS:
        node_num = (prefix << 24) | ((low + 1) & 0x00FFFFFF)
    return node_num


def generate_nodes(
    points: list[tuple[float, float]],
    seed: str,
    prefix: int = DEFAULT_NODE_PREFIX,
    name_prefix: str = "MCV",
    altitude: int = 0,
) -> list[SyntheticNode]:
    """Build one synthetic node per (latitude, longitude) point.

    Node numbers are de-duplicated by probing forward, because a BLAKE2b
    collision across a few thousand points is unlikely but not impossible, and
    two nodes sharing a number would silently merge in the receiver's NodeDB.
    """
    nodes: list[SyntheticNode] = []
    used: set[int] = set()

    for index, (latitude, longitude) in enumerate(points):
        node_num = node_num_for(index, seed, prefix)
        probe = 0
        while node_num in used:
            probe += 1
            node_num = node_num_for(index + probe * 1_000_003, seed, prefix)
        used.add(node_num)

        nodes.append(
            SyntheticNode(
                index=index,
                node_num=node_num,
                long_name=f"{name_prefix}-{index:04d}",
                # short_name is capped at 4 characters by the protobuf options.
                short_name=f"{node_num:04x}"[-4:],
                latitude=latitude,
                longitude=longitude,
                altitude=altitude,
            )
        )
    return nodes


def write_session_csv(nodes: list[SyntheticNode], path: Path) -> None:
    """Persist the node mapping so injected nodes can be identified later."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["index", "node_num", "node_id", "long_name", "short_name",
             "latitude", "longitude", "altitude"]
        )
        for node in nodes:
            writer.writerow(
                [node.index, node.node_num, node.node_id, node.long_name,
                 node.short_name, f"{node.latitude:.7f}",
                 f"{node.longitude:.7f}", node.altitude]
            )


def position_payload(
    node: SyntheticNode,
    timestamp: int | None = None,
    precision_bits: int = 32,
) -> bytes:
    """A Position protobuf serialized as a Data payload.

    Coordinates are degrees * 1e7 as sfixed32, and location_source is LOC_MANUAL
    which is what a node reports when its position was set by hand rather than
    by GPS.
    """
    position = mesh_pb2.Position()
    position.latitude_i = int(round(node.latitude * 1e7))
    position.longitude_i = int(round(node.longitude * 1e7))
    position.altitude = node.altitude
    position.time = int(timestamp if timestamp is not None else time.time())
    position.location_source = mesh_pb2.Position.LocSource.LOC_MANUAL
    position.precision_bits = precision_bits

    data = mesh_pb2.Data()
    data.portnum = portnums_pb2.PortNum.POSITION_APP
    data.payload = position.SerializeToString()
    return data.SerializeToString()


def nodeinfo_payload(
    node: SyntheticNode,
    hw_model: str = "PRIVATE_HW",
) -> bytes:
    """A User protobuf serialized as a Data payload.

    hw_model defaults to PRIVATE_HW rather than impersonating a specific vendor's
    board.
    """
    user = mesh_pb2.User()
    user.id = node.node_id
    user.long_name = node.long_name
    user.short_name = node.short_name
    user.hw_model = mesh_pb2.HardwareModel.Value(hw_model)

    data = mesh_pb2.Data()
    data.portnum = portnums_pb2.PortNum.NODEINFO_APP
    data.payload = user.SerializeToString()
    return data.SerializeToString()


def build_frame(
    payload: bytes,
    sender: int,
    packet_id: int,
    channel_name: str,
    psk: bytes,
    preset_name: str = "LongFast",
    to: int = BROADCAST_ADDR,
    hop_limit: int = HOP_RELIABLE,
    want_ack: bool = False,
) -> bytes:
    """Header plus encrypted payload, ready to hand to a radio backend."""
    key = expand_psk(psk)
    ciphertext = crypto.encrypt(payload, key, packet_id, sender)

    if len(ciphertext) > MAX_ENCRYPTED_PAYLOAD:
        raise ValueError(
            f"payload is {len(ciphertext)} bytes, over the "
            f"{MAX_ENCRYPTED_PAYLOAD}-byte on-air limit"
        )

    header = PacketHeader(
        to=to,
        sender=sender,
        packet_id=packet_id,
        hop_limit=hop_limit,
        want_ack=want_ack,
        # Firmware sets hop_start = hop_limit for packets it originates.
        hop_start=hop_limit,
        channel_hash=channel_hash(channel_name, psk, preset_name),
    )
    return header.pack() + ciphertext


def packet_id_for(node_num: int, sequence: int, seed: str) -> int:
    """Deterministic non-zero packet id.

    The id feeds the AES nonce, so reusing one for two different payloads under
    the same key reuses the keystream and leaks their XOR. Derive from the
    sequence number rather than randomly so a session is reproducible and ids
    within a run never repeat.
    """
    digest = hashlib.blake2b(
        f"{seed}:{node_num}:{sequence}".encode("utf-8"), digest_size=4
    ).digest()
    return int.from_bytes(digest, "little") or 1
