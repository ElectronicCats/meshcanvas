"""AES-CTR payload encryption, matching Meshtastic's PSK path.

Verified against meshtastic/firmware origin/master @ 6de6c3d,
src/mesh/CryptoEngine.cpp initNonce / encryptPacket / encryptAESCtr.

Nonce layout, 16 bytes:

    [0:8]    packet id, as a u64 little-endian
    [8:12]   sender node num, u32 little-endian
    [12:16]  zero, the CTR block counter

The counter is 4 bytes wide, so bytes 12 to 15 are the counter and the first 12
bytes are fixed for a packet.

This is the PSK path only. The public-key path uses AES-CCM with a 13-byte nonce
and appends a tag plus an extra nonce, and it writes extraNonce at offset 4 of
this same buffer. None of that applies here: with no extraNonce the firmware
leaves bytes 4 to 7 zero.

Note that CTR provides confidentiality and nothing else. There is no
authentication tag, so a receiver cannot tell a forged payload from a real one.
That property is exactly what makes this research tool possible, and it is the
thing under test.
"""

from __future__ import annotations

import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

NONCE_LENGTH = 16


def build_nonce(packet_id: int, sender: int) -> bytes:
    """Firmware: CryptoEngine::initNonce with extraNonce left at 0."""
    if not 0 <= packet_id <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError(f"packet_id out of u64 range: {packet_id}")
    if not 0 <= sender <= 0xFFFFFFFF:
        raise ValueError(f"sender out of u32 range: {sender}")
    return struct.pack("<QI", packet_id, sender) + b"\x00" * 4


def encrypt(payload: bytes, key: bytes, packet_id: int, sender: int) -> bytes:
    """Encrypt a serialized Data protobuf.

    An empty key means the channel is unencrypted, and the firmware sends the
    payload in the clear rather than failing.
    """
    if not key:
        return payload
    if len(key) not in (16, 32):
        raise ValueError(
            f"key must be 16 or 32 bytes after expansion, got {len(key)}"
        )

    nonce = build_nonce(packet_id, sender)
    cipher = Cipher(algorithms.AES(key), modes.CTR(nonce))
    encryptor = cipher.encryptor()
    return encryptor.update(payload) + encryptor.finalize()


# CTR is its own inverse, and the firmware defines decrypt as a call to encrypt.
decrypt = encrypt
