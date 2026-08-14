"""Second-CatSniffer receive probe: prove what MeshCanvas actually puts on air.

This is a control test for the transmit side. Point one CatSniffer at MeshCanvas
in rf mode and a second one at this script, both on the same radio settings. Each
frame the second board hears is printed with its decoded header, so you can
confirm independently of any Meshtastic node that:

  - a frame is on the air at all (proves TX and frequency),
  - its channel-hash byte matches the channel you meant to use (proves the PSK),
  - its sender carries the synthetic node prefix (proves it is ours).

Why only the header: the board's firmware (v3.1.0.0) formats received packets as
a text line and truncates the hex to the first 40 bytes (main.c lora_rx_cb). The
16-byte Meshtastic header is fully inside that window; the encrypted payload is
not, so the full protobuf cannot be recovered this way. The header is what
carries the channel hash, which is the byte that decides whether a real node
would have accepted the frame, so it is the evidence that matters.

Run the self-test first, with no hardware attached:

    python -m tools.sniff_rx --self-test

Then, with the second board attached:

    python -m tools.sniff_rx --freq 906875000 --sf 11 --bw 250 --cr 5 \
        --channel meshcanvas --psk-b64 <your channel key>
"""

from __future__ import annotations

import argparse
import re
import sys
import time

from meshcanvas.protocol.channel import channel_hash, expand_psk
from meshcanvas.protocol.header import BROADCAST_ADDR, HEADER_LENGTH, PacketHeader

_RX_LINE = re.compile(
    r"LORA RX:\s*([0-9A-Fa-f]+)(\.\.\.)?\s*\|\s*RSSI:\s*(-?\d+)\s*\|\s*SNR:\s*(-?\d+)"
)


class RxFrame:
    def __init__(self, raw: bytes, truncated: bool, rssi: int, snr: int):
        self.raw = raw
        self.truncated = truncated
        self.rssi = rssi
        self.snr = snr
        self.header = PacketHeader.unpack(raw) if len(raw) >= HEADER_LENGTH else None


def parse_rx_line(line: str) -> RxFrame | None:
    """Turn one firmware RX line into a frame, or None if it is not one."""
    match = _RX_LINE.search(line)
    if not match:
        return None
    hex_text, ellipsis, rssi, snr = match.groups()
    # An odd nibble count means the 40-byte cap split a byte; drop the stray one.
    if len(hex_text) % 2:
        hex_text = hex_text[:-1]
    return RxFrame(bytes.fromhex(hex_text), bool(ellipsis), int(rssi), int(snr))


def describe(frame: RxFrame, expected_hash: int | None, node_prefix: int) -> str:
    if frame.header is None:
        return f"  short frame, {len(frame.raw)} bytes, not a full header"

    h = frame.header
    is_broadcast = h.to == BROADCAST_ADDR
    prefix_matches = (h.sender >> 24) == node_prefix
    hash_matches = expected_hash is not None and h.channel_hash == expected_hash

    # The prefix is only a weak hint: a real node's number can start with any
    # byte, including ours. What actually identifies our traffic is a broadcast
    # on the channel hash we configured. On a busy channel a real node's number
    # may happen to start with 0x7f, and it is not ours.
    if prefix_matches:
        sender_note = "  prefix matches ours (weak hint; real nodes can share it)"
    else:
        sender_note = "  not our prefix"

    lines = [
        f"  RSSI {frame.rssi} dBm  SNR {frame.snr}"
        + ("  (payload truncated by firmware)" if frame.truncated else ""),
        f"  dest       0x{h.to:08x}" + ("  broadcast" if is_broadcast else "  direct"),
        f"  sender     0x{h.sender:08x}" + sender_note,
        f"  packet id  0x{h.packet_id:08x}",
        f"  hop_limit  {h.hop_limit}   hop_start {h.hop_start}   want_ack {h.want_ack}",
        f"  channel    0x{h.channel_hash:02x}",
    ]

    if expected_hash is not None:
        if hash_matches and is_broadcast and prefix_matches:
            lines.append(
                f"  -> OURS: broadcast on the configured channel (hash "
                f"0x{expected_hash:02x}) from our node prefix. Transmit path "
                "confirmed on air."
            )
        elif hash_matches:
            lines.append(
                f"  -> channel hash matches 0x{expected_hash:02x}: a node on this "
                "channel would accept this frame. (Not clearly ours: "
                + ("not a broadcast" if not is_broadcast else "prefix differs")
                + ", so it may be a real node on the same channel.)"
            )
        else:
            lines.append(
                f"  -> channel hash 0x{h.channel_hash:02x} does NOT match the "
                f"expected 0x{expected_hash:02x}. A node on this channel drops it "
                "silently. Different channel or key - most frames here are real "
                "mesh traffic on other channels."
            )
    return "\n".join(lines)


def self_test() -> int:
    """Control-test the decoder against a frame built by our own protocol layer.

    Proves the parser reports the right channel hash and prefix BEFORE it is
    trusted against hardware. A green self-test with a red sniff means the radio
    or the config is wrong, not this tool.
    """
    from meshcanvas.protocol import crypto
    from meshcanvas.protocol.packet import (
        DEFAULT_NODE_PREFIX,
        build_frame,
        generate_nodes,
        position_payload,
    )

    import base64

    # A private test channel: a name and key only your own node holds.
    channel = "meshcanvas"
    psk_b64 = "bWVzaGNhbnZhcy1sYWItaw=="
    psk = base64.b64decode(psk_b64)
    node = generate_nodes([(36.13531, -115.16154)], seed="selftest")[0]
    frame = build_frame(
        position_payload(node, precision_bits=14),
        sender=node.node_num,
        packet_id=0x11223344,
        channel_name=channel,
        psk=psk,
    )

    # Format exactly as the firmware would: uppercase hex, capped at 40 bytes.
    on_air = frame
    shown = on_air[:40]
    truncated = len(on_air) > 40
    line = (
        f"LORA RX: {shown.hex().upper()}{'...' if truncated else ''} "
        f"| RSSI: -42 | SNR: 11"
    )

    parsed = parse_rx_line(line)
    assert parsed is not None, "decoder failed to recognize a firmware RX line"
    assert parsed.truncated == truncated
    assert parsed.header is not None
    assert parsed.header.to == BROADCAST_ADDR
    assert parsed.header.sender == node.node_num
    assert parsed.header.sender >> 24 == DEFAULT_NODE_PREFIX

    expected = channel_hash(channel, psk)
    assert parsed.header.channel_hash == expected, (
        f"decoder read 0x{parsed.header.channel_hash:02x}, expected 0x{expected:02x}"
    )

    # Negative control: encrypting with the wrong key produces a different hash,
    # which is how a mismatch shows up on air. Here the default key is the wrong
    # one for this channel.
    default_hash = channel_hash(channel, b"\x01")
    assert default_hash != expected
    wrong = build_frame(
        position_payload(node), sender=node.node_num, packet_id=1,
        channel_name=channel, psk=b"\x01",
    )
    wrong_line = f"LORA RX: {wrong[:40].hex().upper()}... | RSSI: -50 | SNR: 9"
    wrong_parsed = parse_rx_line(wrong_line)
    assert wrong_parsed.header.channel_hash == default_hash

    print("self-test passed:")
    print(f"  {channel} + your key    -> channel hash 0x{expected:02x}")
    print(f"  {channel} + wrong key   -> channel hash 0x{default_hash:02x} (dropped by a receiver)")
    print("  decoder reads the header correctly from a truncated firmware line")
    print()
    print("sample decode of the good frame:")
    print(describe(parsed, expected, DEFAULT_NODE_PREFIX))
    return 0


def sniff(args) -> int:
    import serial

    from meshcanvas.radio.catsniffer import find_ports

    psk = b""
    expected_hash = None
    if args.psk_b64:
        import base64

        psk = base64.b64decode(args.psk_b64)
    if args.channel:
        expected_hash = channel_hash(args.channel, psk if psk else b"\x01")
        print(
            f"expecting channel '{args.channel}' -> hash 0x{expected_hash:02x} "
            f"(key: {'event/user PSK' if psk else 'default'})"
        )

    # With two boards attached (one transmitting, this one sniffing), name the
    # sniffer's ports explicitly or pick it by serial, since auto-discovery
    # cannot know which board is which.
    if args.lora_port and args.shell_port:
        shell_dev, lora_dev = args.shell_port, args.lora_port
    else:
        ports = find_ports(device_serial=args.device_serial)
        shell_dev, lora_dev = ports.shell, ports.lora
    print(f"Cat-Shell {shell_dev}, Cat-LoRa {lora_dev}")

    shell = serial.Serial(shell_dev, 115200, timeout=0.3)
    lora = serial.Serial(lora_dev, 115200, timeout=0.3)
    time.sleep(0.3)
    shell.reset_input_buffer()

    # Same bring-up as the transmit driver, staying in stream mode so the board
    # keeps its async RX armed and emits the LORA RX lines.
    for command in [
        "band3",
        f"lora_freq {args.freq}",
        f"lora_sf {args.sf}",
        f"lora_bw {args.bw}",
        f"lora_cr {args.cr}",
        f"lora_syncword 0x{args.sync:02X}",
        f"lora_preamble {args.preamble}",
        "lora_mode stream",
        "lora_apply",
    ]:
        shell.write((command + "\r\n").encode())
        shell.flush()
        time.sleep(0.3)
    print("configured, listening. Transmit from the other board. Ctrl-C to stop.\n")

    buffer = ""
    try:
        while True:
            chunk = lora.read(lora.in_waiting or 1)
            if not chunk:
                continue
            buffer += chunk.decode("utf-8", "replace")
            while "\n" in buffer:
                line, _, buffer = buffer.partition("\n")
                frame = parse_rx_line(line)
                if frame is None:
                    continue
                print("frame:")
                print(describe(frame, expected_hash, args.node_prefix))
                print()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        shell.close()
        lora.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true",
                        help="control-test the decoder, no hardware needed")
    parser.add_argument("--freq", type=int, default=917_250_000)
    parser.add_argument("--sf", type=int, default=7)
    parser.add_argument("--bw", type=int, default=500)
    parser.add_argument("--cr", type=int, default=5)
    parser.add_argument("--sync", type=lambda s: int(s, 0), default=0x2B)
    parser.add_argument("--preamble", type=int, default=16)
    parser.add_argument("--channel", default=None,
                        help="channel name, to check the received hash against")
    parser.add_argument("--psk-b64", default=None,
                        help="base64 PSK for that channel")
    parser.add_argument("--node-prefix", type=lambda s: int(s, 0), default=0x7F)
    parser.add_argument("--device-serial", default=None,
                        help="pick a board by serial when several are attached")
    parser.add_argument("--lora-port", default=None,
                        help="name the Cat-LoRa port explicitly (with --shell-port)")
    parser.add_argument("--shell-port", default=None,
                        help="name the Cat-Shell port explicitly (with --lora-port)")
    args = parser.parse_args()

    if bool(args.lora_port) != bool(args.shell_port):
        parser.error("--lora-port and --shell-port must be given together")

    if args.self_test:
        return self_test()
    return sniff(args)


if __name__ == "__main__":
    sys.exit(main())
