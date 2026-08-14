"""CatSniffer v3 serial driver, written against the shipping Zephyr firmware.

Verified against the firmware sources in this session: main.c, shell_commands.c
and overlay.dts. Line numbers below refer to those files.

The board exposes three CDC ACM interfaces, named in overlay.dts in this fixed
order: cdc_acm_uart0 "Cat-Bridge" (CC1352 passthrough), cdc_acm_uart1 "Cat-LoRa"
(SX1262 data and commands), cdc_acm_uart2 "Cat-Shell" (configuration).

Cat-Bridge is the CC1352 path and is unused here. Cat-LoRa is the binary data
path and carries frames. Cat-Shell takes configuration.

Two firmware behaviours drive the shape of this driver:

- Cat-Shell echoes every byte it receives (main.c:275-280), so the first line
  after a command is our own text and has to be skipped.
- The board boots in stream mode, and process_lora_command() only runs in
  command mode (main.c:1336). Any text command sent to Cat-LoRa while in stream
  mode is transmitted as radio payload instead of being parsed.

Transmission writes raw frame bytes to Cat-LoRa in stream mode, which is what
that endpoint is for. The alternative, command mode `TX <hex>`, was measured
against this firmware and rejected:

- The LoRa port assembles commands in `char command_buffer[128]` bounded by
  `cmd_len < sizeof(command_buffer) - 1` (main.c:1233, 1355). After the "TX "
  prefix only 124 hex characters fit, so the real cap is 62 payload bytes, not
  the 128 that `uint8_t tx_data[128]` suggests.
- Characters past that bound are dropped silently. If what survives has even
  length it decodes cleanly and the firmware transmits a truncated frame with
  "TX Result: 0 (Success)". A corrupt packet is reported as a success.

A 62-byte cap does not fit this tool: a NodeInfo frame measures 50 bytes and a
Position frame 42, so a slightly longer node name or an added protobuf field
crosses the line into silent corruption. Stream mode carries the full 255 bytes
the Meshtastic PHY allows. The vendor's own lora_test.py uses stream mode for
binary traffic for the same reason.

What stream mode gives up is the result code, so a transmit is fire and forget.
It also has no framing: the firmware sends whatever is in the ring buffer when
the LoRa thread polls (main.c:1363-1372). Each frame is written in a single
write() call, and frames at or under 64 bytes fit one USB full-speed bulk packet
and so reach the ring buffer in one piece. Larger frames span several USB
packets and could in principle be split across two transmissions.

The configuration path was verified against a live board (firmware v3.1.0.0,
git 8eaa84c): every parameter was read back with lora_config. The transmit path
is unverified on air.
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Iterable, Protocol

import serial
import serial.tools.list_ports

from meshcanvas.protocol.frequency import time_on_air_us
from meshcanvas.radio.base import RadioParams, TransmitError, TransmitResult

# USB identity of the CatSniffer v3 composite device.
VENDOR_ID = 0x1209
PRODUCT_ID = 0xBABB

# Index into the ordered list of the board's three CDC ACM ports.
CDC_BRIDGE = 0
CDC_LORA = 1
CDC_SHELL = 2
CDC_PORT_COUNT = 3

DEFAULT_BAUDRATE = 115_200
DEFAULT_TIMEOUT_S = 2.0

# Stream mode reads into uint8_t tx_buffer[255] (main.c:1363), matching the
# Meshtastic PHY limit of 255 bytes for header plus encrypted payload.
MAX_TX_BYTES = 255

# What command mode would have allowed, kept for the error message. "TX " plus
# 124 hex characters is all that fits in command_buffer[128].
COMMAND_MODE_MAX_TX_BYTES = 62

# Frames at or under one USB full-speed bulk packet reach the firmware's ring
# buffer in a single transaction, so they cannot be split across two LoRa
# transmissions. Larger frames are still written in one call but span several
# USB packets.
USB_ATOMIC_WRITE_BYTES = 64

# shell_commands.c:483-521. lora_bw parses an enum: these three and nothing else.
BANDWIDTHS_KHZ = (125, 250, 500)
# shell_commands.c:440, 468, 539, 580, 634. The firmware's own bounds.
FREQ_MIN_HZ = 137_000_000
FREQ_MAX_HZ = 1_020_000_000
SF_MIN, SF_MAX = 7, 12
CR_MIN, CR_MAX = 5, 8
POWER_MIN_DBM, POWER_MAX_DBM = -9, 22
PREAMBLE_MIN, PREAMBLE_MAX = 6, 65535

# Meshtastic's sync word is 0x2B, which the firmware expands to register 0x24B4.
# The lora_syncword help text (shell_commands.c:660) says "0x2D for Meshtastic
# (reg 0x24D4)", which is wrong: 0x2D is the LoRaWAN public-network value. A
# board configured from that help text hears and is heard by nothing, and the
# failure is silent. Do not "correct" this back to the help text.
SYNC_WORD_NOTE = (
    "Meshtastic uses sync word 0x2B (reg 0x24B4). The CatSniffer firmware help "
    "text claims 0x2D; that is the LoRaWAN public value and is wrong here."
)

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# No prompt is printed by this firmware, but a stock Zephyr shell prints
# "uart:~$ ". Stripping it costs nothing and no firmware reply starts this way.
_SHELL_PROMPT = re.compile(r"^\s*[\w.:~-]*[:~]?[$#>]\s*")

_POLL_INTERVAL_S = 0.005
# The board can be mid-sentence when we open the port; bound the flush.
_MAX_DRAIN_READS = 64
_TRANSCRIPT_LINES = 64


class SerialPort(Protocol):
    """The slice of pyserial this driver uses."""

    is_open: bool
    in_waiting: int

    def write(self, data: bytes) -> int | None: ...

    def read(self, size: int) -> bytes: ...

    def close(self) -> None: ...


SerialFactory = Callable[[str, int, float], SerialPort]


@dataclass(frozen=True)
class CatSnifferPorts:
    lora: str
    shell: str


def open_serial(device: str, baudrate: int, timeout_s: float) -> SerialPort:
    """Default factory. Reads are timeout bounded so a wedged board cannot hang
    the caller."""
    return serial.Serial(
        device,
        baudrate=baudrate,
        timeout=timeout_s,
        write_timeout=timeout_s,
    )


def _natural(device: str) -> str:
    """Zero-pad digit runs so device names sort numerically.

    Without this /dev/ttyACM10 sorts before /dev/ttyACM2, and COM10 before COM2.
    """
    return re.sub(r"\d+", lambda m: m.group().zfill(12), device.lower())


def _interface_number(port) -> int | None:
    """The USB interface number this port belongs to, if the OS exposes it.

    The three CDC functions enumerate as interfaces 0, 2, 4 in Cat-Bridge,
    Cat-LoRa, Cat-Shell order, so this integer sorts them correctly regardless
    of how the OS named the device. Two platforms expose it, in different places:

    - Windows puts it in the hardware id as MI_04 for a composite device.
    - Linux puts it in the libusb location as bus-port:config.interface, e.g.
      2-1:1.4.

    macOS exposes neither (every port shares location 2-1 with no interface
    segment), so this returns None there and the caller falls back to the device
    name, whose trailing number increments with the interface on macOS.
    """
    hwid = getattr(port, "hwid", "") or ""
    match = re.search(r"MI_(\d+)", hwid)
    if match:
        return int(match.group(1))
    location = getattr(port, "location", "") or ""
    match = re.search(r":\d+\.(\d+)$", location)
    if match:
        return int(match.group(1))
    return None


def _port_order_key(port) -> tuple:
    """Sort the board's three ports into Cat-Bridge, Cat-LoRa, Cat-Shell order.

    Prefer the USB interface number (correct on Linux and Windows, and immune to
    the OS's device-naming), and fall back to a numeric-aware device-name sort on
    macOS, where the interface number is not exposed but the device suffix
    increments with the interface.
    """
    interface = _interface_number(port)
    if interface is not None:
        return (0, interface, _natural(str(port.device)))
    return (1, 0, _natural(str(port.device)))


def _serial_number(port) -> str:
    """A stable id for the physical board a port belongs to.

    Used to group the three ports of one board and to keep two attached boards
    from being mixed. Mirrors CatSniffer-Tools/catnip: the serial is in the hwid
    as SER=, with serial_number and location as fallbacks.
    """
    hwid = getattr(port, "hwid", "") or ""
    match = re.search(r"SER=([A-Za-z0-9]+)", hwid)
    if match:
        return match.group(1)
    if getattr(port, "serial_number", None):
        return str(port.serial_number)
    location = getattr(port, "location", None)
    if location:
        # Strip the :config.interface suffix so a board's ports share a key:
        # Linux gives 2-1:1.0 / 2-1:1.2 / 2-1:1.4, which must group as 2-1.
        return f"loc-{str(location).split(':')[0]}"
    return "unknown"


def _role_from_text(port) -> str | None:
    """The port's function from the label the firmware gives its interface.

    The most reliable signal when the OS surfaces it: Linux puts the interface
    string ("Cat-LoRa", "Cat-Shell") in the description, and pyusb exposes it as
    the interface attribute. macOS reports a generic "Catsniffer" for all three,
    so this returns None there and the caller falls back to ordering.
    """
    text = " ".join(
        str(getattr(port, attr, "") or "") for attr in ("description", "interface")
    ).lower()
    if "shell" in text:
        return "shell"
    if "lora" in text:
        return "lora"
    if "bridge" in text:
        return "bridge"
    return None


def _map_device_ports(ports) -> dict:
    """Map one board's three ports to roles, most reliable signal first.

    1. The firmware's own interface label, when the OS surfaces it.
    2. Positional order by USB interface number (Linux, Windows), which is
       correct regardless of what the OS named the device.
    3. Numeric-aware device-name order (macOS), where the suffix increments with
       the interface.
    """
    roles: dict[str, str] = {}
    for port in ports:
        role = _role_from_text(port)
        if role and role not in roles:
            roles[role] = port.device

    if "lora" not in roles or "shell" not in roles:
        ordered = sorted(ports, key=_port_order_key)
        positional = {0: "bridge", 1: "lora", 2: "shell"}
        for index, port in enumerate(ordered[:CDC_PORT_COUNT]):
            role = positional.get(index)
            if role and role not in roles:
                roles[role] = port.device
    return roles


def find_ports(
    port_lister: Callable[[], Iterable] | None = None,
    vendor_id: int = VENDOR_ID,
    product_id: int = PRODUCT_ID,
    device_serial: str | None = None,
) -> CatSnifferPorts:
    """Locate Cat-LoRa and Cat-Shell for a single attached board.

    Ports are grouped by serial number so two boards do not get mixed. With one
    board attached this just works; with several, pass device_serial to pick one,
    or name the ports explicitly on the backend.
    """
    lister = port_lister or serial.tools.list_ports.comports
    seen = list(lister())
    matches = [p for p in seen if p.vid == vendor_id and p.pid == product_id]

    boards: dict[str, list] = {}
    for port in matches:
        boards.setdefault(_serial_number(port), []).append(port)
    complete = {s: ps for s, ps in boards.items() if len(ps) >= CDC_PORT_COUNT}

    if not complete:
        raise TransmitError(
            f"CatSniffer not found: expected {CDC_PORT_COUNT} CDC ACM ports with "
            f"VID 0x{vendor_id:04X} PID 0x{product_id:04X}, found {len(matches)}. "
            f"Ports on this machine: {_describe(seen)}. "
            "Pass lora_port and shell_port to name them yourself."
        )

    if device_serial is not None:
        if device_serial not in complete:
            raise TransmitError(
                f"no CatSniffer with serial {device_serial}; "
                f"attached: {', '.join(sorted(complete))}"
            )
        chosen = complete[device_serial]
    elif len(complete) > 1:
        raise TransmitError(
            f"{len(complete)} CatSniffers attached (serials "
            f"{', '.join(sorted(complete))}). Pass device_serial to pick one, or "
            "name lora_port and shell_port explicitly."
        )
    else:
        chosen = next(iter(complete.values()))

    roles = _map_device_ports(chosen)
    if "lora" not in roles or "shell" not in roles:
        raise TransmitError(
            "could not tell Cat-LoRa from Cat-Shell for this board; "
            f"ports: {_describe(chosen)}. Name lora_port and shell_port yourself."
        )
    return CatSnifferPorts(lora=roles["lora"], shell=roles["shell"])


def _describe(ports: Iterable) -> str:
    entries = [
        "{} (VID {} PID {} {})".format(
            p.device,
            f"0x{p.vid:04X}" if p.vid is not None else "none",
            f"0x{p.pid:04X}" if p.pid is not None else "none",
            getattr(p, "description", "") or "no description",
        )
        for p in ports
    ]
    return ", ".join(entries) if entries else "none"


class _LineReader:
    """Assembles lines from a port without ever blocking.

    Replies for one command can arrive on either port, so neither may be read
    with a blocking readline(): waiting on the silent one would burn the whole
    timeout before the talkative one is even looked at.
    """

    def __init__(self, port: SerialPort) -> None:
        self.port = port
        self._buffer = bytearray()

    def poll(self) -> str | None:
        """The next complete line, or None if one has not arrived yet."""
        waiting = getattr(self.port, "in_waiting", 0) or 0
        if waiting:
            self._buffer += self.port.read(waiting)

        end = self._buffer.find(b"\n")
        if end < 0:
            return None
        raw = bytes(self._buffer[: end + 1])
        del self._buffer[: end + 1]
        return _clean(raw)

    def discard(self) -> None:
        """Drop anything queued so a reply cannot be an older line."""
        self._buffer.clear()
        for _ in range(_MAX_DRAIN_READS):
            waiting = getattr(self.port, "in_waiting", 0) or 0
            if waiting <= 0:
                return
            self.port.read(waiting)


class CatSnifferBackend:
    """Transmits real frames through a CatSniffer v3's SX1262."""

    name = "catsniffer"

    def __init__(
        self,
        lora_port: str | None = None,
        shell_port: str | None = None,
        baudrate: int = DEFAULT_BAUDRATE,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        serial_factory: SerialFactory | None = None,
        port_lister: Callable[[], Iterable] | None = None,
        device_serial: str | None = None,
    ) -> None:
        if (lora_port is None) != (shell_port is None):
            raise ValueError(
                "name both lora_port and shell_port or neither; naming one and "
                "discovering the other would pair ports from different boards"
            )
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")

        if lora_port is None:
            found = find_ports(port_lister, device_serial=device_serial)
            lora_port, shell_port = found.lora, found.shell

        self.lora_device = lora_port
        self.shell_device = shell_port
        self.timeout_s = timeout_s
        self.params: RadioParams | None = None
        self.commands: list[str] = []
        self.transcript: deque[str] = deque(maxlen=_TRANSCRIPT_LINES)

        factory = serial_factory or open_serial
        self._lora: SerialPort | None = factory(lora_port, baudrate, timeout_s)
        self._shell: SerialPort | None = factory(shell_port, baudrate, timeout_s)
        self._shell_reader = _LineReader(self._shell)

    def __enter__(self) -> "CatSnifferBackend":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def configure(self, params: RadioParams) -> None:
        """Bring the radio up and apply the physical layer settings.

        No explicit init command is needed: main() calls initialize_lora() at
        boot (main.c:1534) and a live board reports "LoRa: initialized" before
        anything is sent. `radio TEST` is not that init step. It only prints a
        status line, and only in command mode, so on a freshly booted board it
        returns nothing at all.
        """
        shell = self._require_port(self._shell, "Cat-Shell")
        commands = build_config_commands(params)

        for command in commands:
            self._exchange(shell, command, accept=_ACCEPT.get(command))

        self.params = params

    def transmit(self, frame: bytes) -> TransmitResult:
        if self.params is None:
            raise TransmitError(
                "catsniffer backend was not configured; call configure() first"
            )
        lora = self._require_port(self._lora, "Cat-LoRa")

        if not frame:
            raise ValueError("refusing to transmit an empty frame")
        if len(frame) > MAX_TX_BYTES:
            raise ValueError(
                f"frame is {len(frame)} bytes; the firmware reads at most "
                f"{MAX_TX_BYTES} per transmission (uint8_t tx_buffer[255], "
                "main.c:1363). Splitting it would put two malformed packets "
                "on the air."
            )

        # One write() per frame. The firmware transmits whatever the ring buffer
        # holds when its LoRa thread polls, so a frame split across two write()
        # calls could go out as two packets.
        written = lora.write(bytes(frame))
        if written is not None and written != len(frame):
            raise TransmitError(
                f"serial port accepted {written} of {len(frame)} bytes"
            )
        flush = getattr(lora, "flush", None)
        if flush is not None:
            flush()

        detail = f"stream mode, {len(frame)} bytes"
        if len(frame) > USB_ATOMIC_WRITE_BYTES:
            detail += (
                f" (over {USB_ATOMIC_WRITE_BYTES}, spans multiple USB packets)"
            )

        airtime_us = time_on_air_us(
            len(frame),
            spreading_factor=self.params.spreading_factor,
            bandwidth_khz=self.params.bandwidth_khz,
            coding_rate=self.params.coding_rate,
            preamble_symbols=self.params.preamble_symbols,
        )
        return TransmitResult(
            frame=bytes(frame), airtime_ms=airtime_us // 1000, detail=detail
        )

    def close(self) -> None:
        for attribute in ("_lora", "_shell"):
            port = getattr(self, attribute, None)
            setattr(self, attribute, None)
            if port is None:
                continue
            if not getattr(port, "is_open", True):
                continue
            try:
                port.close()
            except OSError:
                # A board unplugged mid-session errors on close. Nothing left to
                # release either way, and close() must stay safe to call twice.
                pass

    def _require_port(self, port: SerialPort | None, label: str) -> SerialPort:
        if port is None or not getattr(port, "is_open", True):
            raise TransmitError(f"{label} port is closed")
        return port

    def _exchange(
        self, port: SerialPort, command: str, accept=None, required: bool = True
    ) -> str:
        """Send one line and return the reply that answers it.

        The reply is read from Cat-Shell whatever port the command went out on,
        because that is where the firmware routes every response.

        With no acceptance test the first substantive line is the reply, and
        silence is tolerated: several commands are terse and a missing line is
        not proof of failure. Where a specific line has to arrive, accept() says
        which, and silence raises rather than letting the next command run
        against a radio that is not ready.
        """
        self._shell_reader.discard()
        self.commands.append(command)
        port.write((command + "\n").encode("ascii"))
        flush = getattr(port, "flush", None)
        if callable(flush):
            flush()

        reply = self._await(self._shell_reader, command, accept)
        if required and accept is not None and not reply:
            raise TransmitError(
                f"{command!r} was not confirmed within {self.timeout_s} s; "
                f"saw {list(self.transcript)}"
            )
        return reply

    def _await(self, reader: _LineReader, echoed: str, accept) -> str:
        """Read lines until accept() matches, the firmware reports an error, or
        the timeout expires."""
        deadline = time.monotonic() + self.timeout_s

        while True:
            line = reader.poll()
            if line is None:
                if time.monotonic() >= deadline:
                    return ""
                time.sleep(_POLL_INTERVAL_S)
                continue
            if not line or line == echoed:
                continue

            self.transcript.append(line)
            if "error" in line.lower():
                raise TransmitError(f"{echoed!r} failed: {line}")
            if accept is None or accept(line):
                return line

def _is_applied(line: str) -> bool:
    """apply_lora_config() prints "Applying LoRa configuration..." first
    (main.c:588) and the success line only after lora_config() returns
    (main.c:623), so the first line back is not the verdict."""
    return "success" in line.lower()


# Commands whose reply must arrive before the next one is sent. lora_apply
# prints "Applying LoRa configuration..." before it knows the outcome
# (main.c:588), so the first line back is not the verdict.
_ACCEPT = {
    "lora_apply": _is_applied,
}


def build_config_commands(params: RadioParams) -> list[str]:
    """The bring-up sequence, validated before a byte leaves the host.

    Every value is range checked against the firmware's own bounds so a bad
    setting fails as a ValueError naming the number, instead of leaving the
    radio half configured after one line in the middle is rejected.
    """
    frequency = int(params.frequency_hz)
    if not FREQ_MIN_HZ <= frequency <= FREQ_MAX_HZ:
        raise ValueError(
            f"frequency {frequency} Hz is outside the firmware's "
            f"{FREQ_MIN_HZ // 1_000_000}-{FREQ_MAX_HZ // 1_000_000} MHz range"
        )
    if not SF_MIN <= params.spreading_factor <= SF_MAX:
        raise ValueError(
            f"spreading factor must be {SF_MIN}-{SF_MAX}, got {params.spreading_factor}"
        )
    bandwidth = _bandwidth_arg(params.bandwidth_khz)
    if not CR_MIN <= params.coding_rate <= CR_MAX:
        raise ValueError(
            f"coding rate must be {CR_MIN}-{CR_MAX} (the 4/n denominator), "
            f"got {params.coding_rate}"
        )
    if not POWER_MIN_DBM <= params.tx_power_dbm <= POWER_MAX_DBM:
        raise ValueError(
            f"tx power must be {POWER_MIN_DBM} to {POWER_MAX_DBM} dBm, "
            f"got {params.tx_power_dbm}"
        )
    if not PREAMBLE_MIN <= params.preamble_symbols <= PREAMBLE_MAX:
        raise ValueError(
            f"preamble must be {PREAMBLE_MIN}-{PREAMBLE_MAX} symbols, "
            f"got {params.preamble_symbols}"
        )
    if not 0 <= params.sync_word <= 0xFF:
        raise ValueError(f"sync word is one byte, got {params.sync_word}")

    return [
        "band3",
        f"lora_freq {frequency}",
        f"lora_sf {params.spreading_factor}",
        f"lora_bw {bandwidth}",
        f"lora_cr {params.coding_rate}",
        # 0x2B, not the 0x2D the firmware's help text suggests. See SYNC_WORD_NOTE.
        f"lora_syncword 0x{params.sync_word:02X}",
        f"lora_preamble {params.preamble_symbols}",
        f"lora_power {params.tx_power_dbm}",
        # Cat-LoRa carries raw frame bytes, so it must stay in stream mode. In
        # command mode the same bytes would be parsed as text (main.c:1336).
        "lora_mode stream",
        "lora_apply",
    ]


def _bandwidth_arg(bandwidth_khz: float) -> int:
    """lora_bw takes 125, 250 or 500 and nothing else."""
    rounded = int(bandwidth_khz)
    if rounded != bandwidth_khz or rounded not in BANDWIDTHS_KHZ:
        allowed = ", ".join(str(b) for b in BANDWIDTHS_KHZ)
        raise ValueError(
            f"bandwidth {bandwidth_khz} kHz is not one the firmware accepts "
            f"({allowed}). The wide-LoRa presets cannot run on an SX1262."
        )
    return rounded


def _clean(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    text = _ANSI.sub("", text)
    text = text.replace("\r", "").strip()
    return _SHELL_PROMPT.sub("", text).strip()
