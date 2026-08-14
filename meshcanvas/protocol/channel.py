"""Channel name, PSK expansion and the two hashes Meshtastic uses.

There are two unrelated hashes here and mixing them up is the most expensive
mistake available in this codebase:

    channel_hash()  8-bit XOR of name and expanded PSK. Goes in the packet
                    header as a decode hint. Firmware: Channels::generateHash.
    djb2()          32-bit string hash. Selects the RF frequency slot.
                    Firmware: hash() in RadioInterface.cpp.

Using the header hash for slot selection puts the radio on a frequency nobody
listens to, and nothing reports an error.

Verified against meshtastic/firmware origin/master @ 6de6c3d, src/mesh/Channels.cpp
and src/mesh/RadioInterface.cpp.
"""

from __future__ import annotations

# src/mesh/Channels.h: the PSK of the public default channel.
DEFAULT_PSK = bytes(
    [0xD4, 0xF1, 0xBB, 0x3A, 0x20, 0x29, 0x07, 0x59,
     0xF0, 0xBC, 0xFF, 0xAB, 0xCF, 0x4E, 0x69, 0x01]
)

# src/mesh/Channels.h: the PSK used for large events.
EVENT_PSK = bytes(
    [0x38, 0x4B, 0xBC, 0xC0, 0x1D, 0xC0, 0x22, 0xD1, 0x81, 0xBF, 0x36,
     0xB8, 0x61, 0x21, 0xE1, 0xFB, 0x96, 0xB7, 0x2E, 0x55, 0xBF, 0x74,
     0x22, 0x7E, 0x9D, 0x6A, 0xFB, 0x48, 0xD6, 0x4C, 0xB1, 0xA1]
)


def xor_hash(data: bytes) -> int:
    """XOR-fold bytes into one byte. Firmware: xorHash()."""
    code = 0
    for byte in data:
        code ^= byte
    return code


def djb2(text: str) -> int:
    """djb2 by Dan Bernstein, as used for frequency slot selection.

    Firmware hashes the raw bytes of a C string, so encode as latin-1 to keep
    one byte per character for any name the firmware could hold.
    """
    value = 5381
    for byte in text.encode("latin-1", errors="replace"):
        value = ((value * 33) + byte) & 0xFFFFFFFF
    return value


def expand_psk(psk: bytes) -> bytes:
    """Expand a stored PSK to the key actually used for AES and for hashing.

    Firmware: Channels::getKey. Returns b"" when encryption is disabled.

        0 bytes     no encryption
        1 byte      shorthand index; 0 disables, N copies DEFAULT_PSK and adds
                    N-1 to its last byte, so index 1 is DEFAULT_PSK unchanged
        2..15       zero-padded to 16 (AES128)
        16          used as-is (AES128)
        17..31      zero-padded to 32 (AES256)
        32          used as-is (AES256)
    """
    if len(psk) == 0:
        return b""

    if len(psk) == 1:
        index = psk[0]
        if index == 0:
            return b""
        # The firmware does *last = *last + pskIndex - 1 on a uint8_t, so this
        # wraps rather than growing the key.
        return DEFAULT_PSK[:-1] + bytes([(DEFAULT_PSK[-1] + index - 1) & 0xFF])

    if len(psk) < 16:
        return psk + b"\x00" * (16 - len(psk))

    if len(psk) == 16 or len(psk) == 32:
        return psk

    if len(psk) < 32:
        return psk + b"\x00" * (32 - len(psk))

    raise ValueError(f"PSK longer than 32 bytes: {len(psk)}")


def resolve_channel_name(name: str, preset_name: str = "LongFast") -> str:
    """An empty channel name is displayed, hashed and slotted as the preset name.

    Firmware: Channels::getName substitutes the modem preset display name for
    the empty string, or "Custom" when the config is not preset-based.
    """
    return name if name else preset_name


def channel_hash(name: str, psk: bytes, preset_name: str = "LongFast") -> int:
    """The header channel byte. Firmware: Channels::generateHash.

    XOR over the resolved channel name, then over the *expanded* key. Passing
    the raw shorthand PSK here instead of the expanded key silently produces a
    hash no real node will match.
    """
    resolved = resolve_channel_name(name, preset_name)
    key = expand_psk(psk)
    return xor_hash(resolved.encode("utf-8")) ^ xor_hash(key)
