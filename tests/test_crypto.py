"""AES-CTR payload encryption and the nonce layout."""

import struct

import pytest

from meshcanvas.protocol.channel import DEFAULT_PSK, expand_psk
from meshcanvas.protocol.crypto import NONCE_LENGTH, build_nonce, decrypt, encrypt


class TestNonce:
    def test_is_16_bytes(self):
        assert len(build_nonce(1, 2)) == NONCE_LENGTH

    def test_layout_is_packet_id_u64_then_sender_u32_then_zeros(self):
        nonce = build_nonce(0xDEADBEEF, 0x12345678)
        assert nonce[0:8] == struct.pack("<Q", 0xDEADBEEF)
        assert nonce[8:12] == struct.pack("<I", 0x12345678)
        assert nonce[12:16] == b"\x00\x00\x00\x00"

    def test_bytes_4_to_7_stay_zero_on_the_psk_path(self):
        # The public-key path writes extraNonce at offset 4. With a 32-bit
        # packet id widened to u64 and no extraNonce, these must be zero.
        assert build_nonce(0xFFFFFFFF, 0xFFFFFFFF)[4:8] == b"\x00" * 4

    def test_rejects_out_of_range_values(self):
        with pytest.raises(ValueError, match="u32"):
            build_nonce(1, 0x1_0000_0000)
        with pytest.raises(ValueError, match="u64"):
            build_nonce(0x1_0000_0000_0000_0000, 1)


class TestEncryption:
    def test_ctr_round_trips(self):
        key = expand_psk(DEFAULT_PSK)
        plaintext = b"the quick brown fox jumps over the lazy dog"
        ciphertext = encrypt(plaintext, key, packet_id=42, sender=0x7F000001)
        assert ciphertext != plaintext
        assert decrypt(ciphertext, key, packet_id=42, sender=0x7F000001) == plaintext

    def test_decrypt_is_the_same_operation_as_encrypt(self):
        # The firmware literally defines decrypt as a call to encryptPacket.
        assert decrypt is encrypt

    def test_ciphertext_is_the_same_length_as_plaintext(self):
        key = expand_psk(DEFAULT_PSK)
        for length in (1, 15, 16, 17, 200):
            assert len(encrypt(b"x" * length, key, 1, 1)) == length

    def test_a_different_packet_id_gives_a_different_keystream(self):
        key = expand_psk(DEFAULT_PSK)
        plaintext = b"same plaintext"
        assert encrypt(plaintext, key, 1, 99) != encrypt(plaintext, key, 2, 99)

    def test_a_different_sender_gives_a_different_keystream(self):
        key = expand_psk(DEFAULT_PSK)
        plaintext = b"same plaintext"
        assert encrypt(plaintext, key, 7, 1) != encrypt(plaintext, key, 7, 2)

    def test_wrong_key_does_not_recover_the_plaintext(self):
        plaintext = b"the quick brown fox"
        ciphertext = encrypt(plaintext, expand_psk(DEFAULT_PSK), 1, 1)
        assert decrypt(ciphertext, expand_psk(b"\x02"), 1, 1) != plaintext

    def test_empty_key_means_cleartext(self):
        # An unencrypted channel sends the payload as-is rather than failing.
        assert encrypt(b"hello", b"", 1, 1) == b"hello"

    def test_aes256_key_is_accepted(self):
        key = bytes(range(32))
        assert decrypt(encrypt(b"hello", key, 1, 1), key, 1, 1) == b"hello"

    def test_unexpanded_key_length_is_rejected(self):
        with pytest.raises(ValueError, match="16 or 32"):
            encrypt(b"hello", b"\x01" * 20, 1, 1)


def test_keystream_reuse_is_visible_when_ids_collide():
    """Documents why packet ids must not repeat under one key.

    CTR with a repeated nonce leaks the XOR of the two plaintexts. This is a
    property of the protocol, not a defect in this implementation, and it is the
    reason packet_id_for() derives from a sequence number.
    """
    key = expand_psk(DEFAULT_PSK)
    a, b = b"attack at dawn!!", b"retreat at dusk!"
    ca = encrypt(a, key, packet_id=5, sender=1)
    cb = encrypt(b, key, packet_id=5, sender=1)

    leaked = bytes(x ^ y for x, y in zip(ca, cb))
    assert leaked == bytes(x ^ y for x, y in zip(a, b))
