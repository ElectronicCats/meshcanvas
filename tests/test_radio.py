"""Radio backends, driven by fakes rather than hardware.

The serial double replays the CatSniffer firmware's own behaviour, taken from
main.c and shell_commands.c:

- Cat-Shell echoes every byte it receives (main.c:275-280).
- `lora_apply` answers with more than one line, printing "Applying LoRa
  configuration..." before it knows the outcome (main.c:588). A driver that took
  the first line as the verdict would call a pending configuration a success.
- Cat-LoRa in stream mode is a binary pipe. Frames are written to it raw and it
  never answers, so the fake for that port records writes without parsing them.

Every string below is quoted from that source. The configuration sequence was
additionally replayed against a live board running firmware v3.1.0.0.
"""

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from meshtastic.protobuf import mqtt_pb2

from meshcanvas.protocol.frequency import time_on_air_ms
from meshcanvas.protocol.header import HEADER_LENGTH, PacketHeader
from meshcanvas.radio import catsniffer as cs
from meshcanvas.radio import mqtt as mq
from meshcanvas.radio.base import RadioBackend, RadioParams, TransmitError
from meshcanvas.radio.catsniffer import CatSnifferBackend, find_ports
from meshcanvas.radio.mqtt import MqttBackend, envelope_for, topic_for
from meshcanvas.radio.null import NullBackend

LONGFAST = RadioParams(
    frequency_hz=906_875_000,
    bandwidth_khz=250.0,
    spreading_factor=11,
    coding_rate=5,
    tx_power_dbm=20,
)


def params(**overrides) -> RadioParams:
    fields = dict(
        frequency_hz=906_875_000,
        bandwidth_khz=250.0,
        spreading_factor=11,
        coding_rate=5,
        tx_power_dbm=20,
    )
    fields.update(overrides)
    return RadioParams(**fields)


def frame(payload_len: int = 40, sender: int = 0x7F001122) -> bytes:
    header = PacketHeader(sender=sender, packet_id=0x1234ABCD, channel_hash=0x08)
    return header.pack() + bytes(range(payload_len))


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


def firmware_lines(command: str) -> list[str]:
    """What the board sends back, in order, for one command."""
    if command == "band3":
        return ["LoRa Band"]
    if command == "radio TEST":
        return [
            "LoRa: Starting initialization...",
            "LoRa: Device ready",
            "LoRa: Initialization completed (RX mode)!",
            "TEST: LoRa ready!",
        ]
    if command.startswith("lora_freq "):
        return [f"Frequency set to {command.split()[1]} Hz (pending)"]
    if command.startswith("lora_sf "):
        return [f"Spreading Factor set to SF{command.split()[1]} (pending)"]
    if command.startswith("lora_bw "):
        return [f"Bandwidth set to {command.split()[1]} kHz (pending)"]
    if command.startswith("lora_cr "):
        return [f"Coding Rate set to 4/{command.split()[1]} (pending)"]
    if command.startswith("lora_syncword "):
        return [f"Sync word: {command.split()[1]} (reg 0x24B4) (pending)"]
    if command.startswith("lora_preamble "):
        return [f"Preamble length set to {command.split()[1]} (pending)"]
    if command.startswith("lora_power "):
        return [f"TX Power set to {command.split()[1]} dBm (pending)"]
    if command.startswith("lora_mode "):
        return [f"LoRa mode set to {command.split()[1].upper()} (fast blink)"]
    if command == "lora_apply":
        return [
            "Applying LoRa configuration...",
            "LoRa configuration applied successfully (RX mode)",
            "LoRa configuration applied successfully",
        ]
    if command.startswith("TX "):
        return ["TX Result: 0 (Success)"]
    return ["Unknown command. Type 'help'"]


class FakeSerialPort:
    """The slice of pyserial the driver uses.

    Replies are queued at write time. `reply_to` is the port they leave by,
    which for Cat-LoRa is a different port than the one written to. An empty
    read() means nothing has arrived, which is what a timed-out pyserial port
    looks like to the caller.
    """

    def __init__(
        self,
        responder=firmware_lines,
        echo: bool = False,
        banner: bytes = b"",
        binary: bool = False,
        short_write: int | None = None,
    ):
        self.writes: list[bytes] = []
        self.commands: list[str] = []
        self.close_calls = 0
        self.is_open = True
        self.reply_to: "FakeSerialPort | None" = None
        self._responder = responder
        self._echo = echo
        self._out = bytearray(banner)
        self._partial = bytearray()
        # A binary port carries raw frames, so writes are never parsed as text.
        # Cat-LoRa in stream mode is one, and frames legitimately contain 0x0A.
        self._binary = binary
        self._short_write = short_write

    def write(self, data: bytes) -> int:
        self.writes.append(bytes(data))
        if self._short_write is not None:
            return self._short_write
        if self._binary:
            return len(data)
        self._partial += data
        while b"\n" in self._partial:
            raw, _, rest = bytes(self._partial).partition(b"\n")
            self._partial = bytearray(rest)
            command = raw.decode("ascii").strip()
            self.commands.append(command)
            if self._echo:
                self._out += command.encode("ascii") + b"\n"
            sink = self.reply_to or self
            for line in self._responder(command) or []:
                sink._out += line.encode("ascii") + b"\r\n"
        return len(data)

    @property
    def in_waiting(self) -> int:
        return len(self._out)

    def read(self, size: int) -> bytes:
        chunk = bytes(self._out[:size])
        del self._out[:size]
        return chunk

    def readline(self) -> bytes:
        end = self._out.find(b"\n")
        if end < 0:
            return b""
        line = bytes(self._out[: end + 1])
        del self._out[: end + 1]
        return line

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.close_calls += 1
        self.is_open = False


def shell_port(**kwargs) -> FakeSerialPort:
    kwargs.setdefault("echo", True)
    return FakeSerialPort(**kwargs)


def lora_port(**kwargs) -> FakeSerialPort:
    kwargs.setdefault("binary", True)
    return FakeSerialPort(**kwargs)


@dataclass
class FakeBoard:
    """A CatSniffer's two ports. Cat-LoRa is binary, Cat-Shell is text."""

    lora: FakeSerialPort = field(default_factory=lora_port)
    shell: FakeSerialPort = field(default_factory=shell_port)

    def factory(self, device: str, baudrate: int, timeout_s: float) -> FakeSerialPort:
        return {"lora": self.lora, "shell": self.shell}[device]

    def backend(self, timeout_s: float = 0.25, **kwargs) -> CatSnifferBackend:
        return CatSnifferBackend(
            lora_port="lora",
            shell_port="shell",
            serial_factory=self.factory,
            timeout_s=timeout_s,
            **kwargs,
        )

    def configured(self, **kwargs) -> CatSnifferBackend:
        backend = self.backend(**kwargs)
        backend.configure(LONGFAST)
        return backend


@dataclass
class FakeUsbPort:
    device: str
    vid: int | None
    pid: int | None
    location: str | None = None
    description: str = ""
    hwid: str = ""
    serial_number: str | None = None


class FakeMqttClient:
    def __init__(self, connect_error=None, connect_rc=0, publish_rc=0):
        self.connect_error = connect_error
        self.connect_rc = connect_rc
        self.publish_rc = publish_rc
        self.connected: list[tuple[str, int, int]] = []
        self.published: list[tuple[str, bytes, int]] = []
        self.credentials = None
        self.loop_started = 0
        self.loop_stopped = 0
        self.disconnects = 0

    def username_pw_set(self, username, password=None):
        self.credentials = (username, password)

    def connect(self, host, port, keepalive):
        if self.connect_error is not None:
            raise self.connect_error
        self.connected.append((host, port, keepalive))
        return self.connect_rc

    def publish(self, topic, payload, qos=0):
        self.published.append((topic, payload, qos))
        return SimpleNamespace(rc=self.publish_rc)

    def loop_start(self):
        self.loop_started += 1

    def loop_stop(self):
        self.loop_stopped += 1

    def disconnect(self):
        self.disconnects += 1


def mqtt_backend(client: FakeMqttClient, **kwargs) -> MqttBackend:
    return MqttBackend(host="broker.invalid", client_factory=lambda: client, **kwargs)


# --------------------------------------------------------------------------
# null backend
# --------------------------------------------------------------------------


class TestNullBackend:
    def test_records_every_frame(self):
        backend = NullBackend()
        backend.configure(LONGFAST)
        for _ in range(3):
            backend.transmit(frame(40))

        assert backend.frame_count == 3
        assert backend.sent == [frame(40)] * 3

    def test_airtime_matches_time_on_air_for_the_frame_length(self):
        backend = NullBackend()
        backend.configure(LONGFAST)
        payload = frame(40)

        result = backend.transmit(payload)

        assert result.airtime_ms == time_on_air_ms(
            len(payload),
            spreading_factor=LONGFAST.spreading_factor,
            bandwidth_khz=LONGFAST.bandwidth_khz,
            coding_rate=LONGFAST.coding_rate,
            preamble_symbols=LONGFAST.preamble_symbols,
        )

    def test_airtime_follows_the_configured_preset(self):
        slow = NullBackend()
        slow.configure(params(spreading_factor=12, bandwidth_khz=125.0, coding_rate=8))
        fast = NullBackend()
        fast.configure(params(spreading_factor=7, bandwidth_khz=500.0))

        assert slow.transmit(frame()).airtime_ms > fast.transmit(frame()).airtime_ms

    def test_total_airtime_accumulates(self):
        backend = NullBackend()
        backend.configure(LONGFAST)
        one = backend.transmit(frame(40)).airtime_ms
        backend.transmit(frame(40))

        # Totals sum microseconds and truncate once, so the total is never below
        # the sum of the per-frame values.
        assert backend.total_airtime_ms >= 2 * one
        assert backend.total_airtime_ms < 2 * one + 2

    def test_transmit_before_configure_raises(self):
        with pytest.raises(TransmitError, match="not configured"):
            NullBackend().transmit(frame())

    def test_nothing_is_transmitted(self):
        backend = NullBackend()
        backend.configure(LONGFAST)
        assert "not transmitted" in backend.transmit(frame()).detail

    def test_reset_clears_the_record(self):
        backend = NullBackend()
        backend.configure(LONGFAST)
        backend.transmit(frame())
        backend.reset()
        assert (backend.frame_count, backend.total_airtime_ms) == (0, 0)


# --------------------------------------------------------------------------
# protocol conformance
# --------------------------------------------------------------------------


class TestBackendProtocol:
    def test_null_backend_satisfies_the_protocol(self):
        assert isinstance(NullBackend(), RadioBackend)

    def test_catsniffer_backend_satisfies_the_protocol(self):
        assert isinstance(FakeBoard().backend(), RadioBackend)

    def test_mqtt_backend_satisfies_the_protocol(self):
        assert isinstance(mqtt_backend(FakeMqttClient()), RadioBackend)


# --------------------------------------------------------------------------
# catsniffer: port discovery
# --------------------------------------------------------------------------


class TestPortDiscovery:
    def usb_ports(self):
        return [
            FakeUsbPort("/dev/cu.usbmodem12341", 0x1209, 0xBABB, "20-2:1.2"),
            FakeUsbPort("/dev/cu.Bluetooth", None, None, None, "Bluetooth"),
            FakeUsbPort("/dev/cu.usbmodem12343", 0x1209, 0xBABB, "20-2:1.4"),
            FakeUsbPort("/dev/cu.usbmodem12340", 0x1209, 0xBABB, "20-2:1.0"),
        ]

    def test_lora_is_the_second_interface_and_shell_the_third(self):
        ports = find_ports(port_lister=self.usb_ports)
        assert ports.lora == "/dev/cu.usbmodem12341"
        assert ports.shell == "/dev/cu.usbmodem12343"

    def test_linux_orders_by_interface_number_not_device_name(self):
        # /dev/ttyACM10 sorts before /dev/ttyACM2 by name, which would pick the
        # wrong interface. Linux exposes the interface in the location string.
        ports = [
            FakeUsbPort("/dev/ttyACM10", 0x1209, 0xBABB, "1-1:1.2"),
            FakeUsbPort("/dev/ttyACM11", 0x1209, 0xBABB, "1-1:1.4"),
            FakeUsbPort("/dev/ttyACM9", 0x1209, 0xBABB, "1-1:1.0"),
        ]
        found = find_ports(port_lister=lambda: ports)
        assert (found.lora, found.shell) == ("/dev/ttyACM10", "/dev/ttyACM11")

    def test_windows_orders_by_interface_number_not_com_number(self):
        # Windows assigns COM numbers by enumeration history, so COM order is
        # unrelated to interface order. The interface number lives in the hwid
        # as MI_xx. Here the LoRa function (MI_02) is COM7 and the bridge
        # (MI_00) is COM11: a name sort would pick COM11 as first, wrongly.
        ports = [
            FakeUsbPort("COM7", 0x1209, 0xBABB, "", "Catsniffer",
                        r"USB VID:PID=1209:BABB SER=E661 LOCATION=1-2:x.2 MI_02"),
            FakeUsbPort("COM11", 0x1209, 0xBABB, "", "Catsniffer",
                        r"USB VID:PID=1209:BABB SER=E661 LOCATION=1-2:x.0 MI_00"),
            FakeUsbPort("COM3", 0x1209, 0xBABB, "", "Catsniffer",
                        r"USB VID:PID=1209:BABB SER=E661 LOCATION=1-2:x.4 MI_04"),
        ]
        found = find_ports(port_lister=lambda: ports)
        assert (found.lora, found.shell) == ("COM7", "COM3")

    def test_macos_falls_back_to_device_name_order(self):
        # macOS gives every port the same location and no interface number, but
        # the device suffix increments with the interface (2101/2103/2105 =
        # Bridge/LoRa/Shell). Returned out of order to prove the sort fixes it.
        ports = [
            FakeUsbPort("/dev/cu.usbmodem2105", 0x1209, 0xBABB, "2-1"),
            FakeUsbPort("/dev/cu.usbmodem2101", 0x1209, 0xBABB, "2-1"),
            FakeUsbPort("/dev/cu.usbmodem2103", 0x1209, 0xBABB, "2-1"),
        ]
        found = find_ports(port_lister=lambda: ports)
        assert found.lora == "/dev/cu.usbmodem2103"
        assert found.shell == "/dev/cu.usbmodem2105"

    def test_maps_by_interface_label_when_the_os_exposes_it(self):
        # Linux surfaces the firmware's interface label in the description. This
        # is the most reliable signal, so it should win even if the device-name
        # order disagrees. Here shell has the lowest device name.
        ports = [
            FakeUsbPort("/dev/ttyACM0", 0x1209, 0xBABB, "1-1:1.4", "Cat-Shell",
                        serial_number="E661"),
            FakeUsbPort("/dev/ttyACM1", 0x1209, 0xBABB, "1-1:1.2", "Cat-LoRa",
                        serial_number="E661"),
            FakeUsbPort("/dev/ttyACM2", 0x1209, 0xBABB, "1-1:1.0", "Cat-Bridge",
                        serial_number="E661"),
        ]
        found = find_ports(port_lister=lambda: ports)
        assert found.lora == "/dev/ttyACM1"
        assert found.shell == "/dev/ttyACM0"

    def test_two_boards_are_not_mixed(self):
        # Two CatSniffers attached: the three ports of each must be grouped by
        # serial, and auto-discovery must refuse rather than pair ports across
        # boards.
        def two_boards():
            out = []
            for serial in ("AAAA", "BBBB"):
                for iface in (0, 2, 4):
                    out.append(FakeUsbPort(
                        f"/dev/ttyACM_{serial}_{iface}", 0x1209, 0xBABB,
                        f"1-1:1.{iface}", "Catsniffer", serial_number=serial))
            return out

        with pytest.raises(TransmitError, match="2 CatSniffers"):
            find_ports(port_lister=two_boards)

        # Naming the serial resolves it.
        found = find_ports(port_lister=two_boards, device_serial="BBBB")
        assert found.lora == "/dev/ttyACM_BBBB_2"
        assert found.shell == "/dev/ttyACM_BBBB_4"

    def test_unknown_device_serial_is_reported(self):
        def one_board():
            return [FakeUsbPort(f"/dev/ttyACM{i}", 0x1209, 0xBABB,
                                f"1-1:1.{i*2}", serial_number="AAAA")
                    for i in range(3)]
        with pytest.raises(TransmitError, match="ZZZZ"):
            find_ports(port_lister=one_board, device_serial="ZZZZ")

    def test_other_vendors_are_ignored(self):
        ports = [FakeUsbPort("/dev/ttyUSB0", 0x10C4, 0xEA60, "1-1:1.0", "CP2102")]
        with pytest.raises(TransmitError) as excinfo:
            find_ports(port_lister=lambda: ports)
        assert "found 0" in str(excinfo.value)

    def test_failure_lists_what_was_actually_found(self):
        ports = [FakeUsbPort("/dev/ttyACM0", 0x1209, 0xBABB, "1-1:1.0", "Cat-Bridge")]
        with pytest.raises(TransmitError) as excinfo:
            find_ports(port_lister=lambda: ports)
        message = str(excinfo.value)
        assert "/dev/ttyACM0" in message
        assert "0x1209" in message
        assert "lora_port" in message

    def test_naming_only_one_port_is_refused(self):
        with pytest.raises(ValueError, match="both"):
            CatSnifferBackend(lora_port="lora", serial_factory=FakeBoard().factory)


# --------------------------------------------------------------------------
# catsniffer: configuration
# --------------------------------------------------------------------------


class TestCatSnifferConfigure:
    def test_full_sequence_in_firmware_order(self):
        board = FakeBoard()
        board.backend().configure(LONGFAST)

        assert board.shell.commands == [
            "band3",
            "lora_freq 906875000",
            "lora_sf 11",
            "lora_bw 250",
            "lora_cr 5",
            "lora_syncword 0x2B",
            "lora_preamble 16",
            "lora_power 20",
            "lora_mode stream",
            "lora_apply",
        ]

    def test_no_init_command_is_sent(self):
        # main() calls initialize_lora() at boot (main.c:1534), so the radio is
        # already initialized. "radio TEST" is not an init step: it only prints a
        # status line, and only in command mode, so on a freshly booted board in
        # stream mode it returns nothing and would stall the sequence. Confirmed
        # against firmware v3.1.0.0.
        board = FakeBoard()
        board.backend().configure(LONGFAST)
        assert "radio TEST" not in board.shell.commands

    def test_band3_comes_first_and_apply_last(self):
        board = FakeBoard()
        board.backend().configure(LONGFAST)
        assert board.shell.commands[0] == "band3"
        assert board.shell.commands[-1] == "lora_apply"

    def test_stream_mode_is_selected_before_apply(self):
        # Cat-LoRa carries raw frame bytes. In command mode the firmware would
        # parse those bytes as text instead of transmitting them.
        board = FakeBoard()
        board.backend().configure(LONGFAST)
        sent = board.shell.commands
        assert "lora_mode stream" in sent
        assert "lora_mode command" not in sent
        assert sent.index("lora_mode stream") < sent.index("lora_apply")

    def test_sync_word_is_2b_not_2d(self):
        board = FakeBoard()
        board.backend().configure(LONGFAST)
        assert "lora_syncword 0x2B" in board.shell.commands
        assert "lora_syncword 0x2D" not in board.shell.commands

    def test_configuration_does_not_touch_the_lora_port(self):
        board = FakeBoard()
        board.backend().configure(LONGFAST)
        assert board.lora.writes == []

    def test_multi_line_replies_do_not_desynchronize_the_sequence(self):
        # lora_apply prints "Applying LoRa configuration..." before it knows the
        # outcome (main.c:588), so more than one line comes back for it.
        board = FakeBoard()
        backend = board.backend()
        backend.configure(LONGFAST)
        assert "LoRa configuration applied successfully (RX mode)" in list(
            backend.transcript
        )

    def test_apply_is_not_confirmed_by_the_pending_line_alone(self):
        def stalls(command: str) -> list[str]:
            if command == "lora_apply":
                return ["Applying LoRa configuration..."]
            return firmware_lines(command)

        board = FakeBoard(shell=shell_port(responder=stalls))
        with pytest.raises(TransmitError, match="lora_apply"):
            board.backend().configure(LONGFAST)

    def test_unacknowledged_apply_stops_the_sequence(self):
        def no_ack(command: str) -> list[str]:
            if command == "lora_apply":
                return ["Applying LoRa configuration..."]
            return firmware_lines(command)

        board = FakeBoard(shell=shell_port(responder=no_ack))
        with pytest.raises(TransmitError, match="lora_apply"):
            board.backend().configure(LONGFAST)
        assert board.shell.commands[-1] == "lora_apply"

    def test_invalid_bandwidth_is_rejected_before_any_write(self):
        board = FakeBoard()
        backend = board.backend()
        with pytest.raises(ValueError, match="200"):
            backend.configure(params(bandwidth_khz=200.0))
        assert board.shell.writes == []

    def test_out_of_range_spreading_factor_is_rejected_before_any_write(self):
        board = FakeBoard()
        backend = board.backend()
        with pytest.raises(ValueError, match="7-12"):
            backend.configure(params(spreading_factor=6))
        assert board.shell.writes == []

    def test_out_of_band_frequency_is_rejected_before_any_write(self):
        board = FakeBoard()
        backend = board.backend()
        with pytest.raises(ValueError, match="137-1020"):
            backend.configure(params(frequency_hz=2_400_000_000))
        assert board.shell.writes == []

    def test_out_of_range_power_is_rejected_before_any_write(self):
        board = FakeBoard()
        backend = board.backend()
        with pytest.raises(ValueError, match="-9 to 22"):
            backend.configure(params(tx_power_dbm=30))
        assert board.shell.writes == []

    def test_short_preamble_is_rejected_before_any_write(self):
        board = FakeBoard()
        backend = board.backend()
        with pytest.raises(ValueError, match="6-65535"):
            backend.configure(params(preamble_symbols=4))
        assert board.shell.writes == []

    def test_firmware_error_carries_the_firmware_text(self):
        def rejects_frequency(command: str) -> list[str]:
            if command.startswith("lora_freq"):
                return ["Error: Frequency must be 137-1020 MHz"]
            return firmware_lines(command)

        board = FakeBoard(shell=shell_port(responder=rejects_frequency))
        with pytest.raises(TransmitError) as excinfo:
            board.backend().configure(LONGFAST)
        assert "Error: Frequency must be 137-1020 MHz" in str(excinfo.value)

    def test_lora_not_initialized_on_apply_is_a_failure(self):
        def apply_fails(command: str) -> list[str]:
            if command == "lora_apply":
                return [
                    "Error: LoRa not initialized. Use TEST command first.",
                    "Error applying configuration: -19",
                ]
            return firmware_lines(command)

        board = FakeBoard(shell=shell_port(responder=apply_fails))
        with pytest.raises(TransmitError, match="LoRa not initialized"):
            board.backend().configure(LONGFAST)

    def test_a_failed_configure_leaves_the_backend_unconfigured(self):
        def apply_fails(command: str) -> list[str]:
            if command == "lora_apply":
                return ["Error applying configuration: -5"]
            return firmware_lines(command)

        board = FakeBoard(shell=shell_port(responder=apply_fails))
        backend = board.backend()
        with pytest.raises(TransmitError):
            backend.configure(LONGFAST)
        with pytest.raises(TransmitError, match="not configured"):
            backend.transmit(frame())

    def test_shell_echo_is_not_mistaken_for_a_reply(self):
        board = FakeBoard()
        backend = board.backend()
        backend.configure(LONGFAST)
        assert "band3" not in list(backend.transcript)
        assert "LoRa Band" in list(backend.transcript)

    def test_boot_banner_is_drained(self):
        banner = b"*** Booting Zephyr OS build v3.6.0 ***\r\nCatSniffer v3 ready\r\n"
        board = FakeBoard(shell=shell_port(banner=banner))
        board.backend().configure(LONGFAST)
        assert board.shell.commands[-1] == "lora_apply"


# --------------------------------------------------------------------------
# catsniffer: transmit
# --------------------------------------------------------------------------


class TestCatSnifferTransmit:
    def test_frame_bytes_go_to_the_lora_port_verbatim(self):
        board = FakeBoard()
        payload = frame(40)
        result = board.configured().transmit(payload)

        # Stream mode: the raw frame, with no hex encoding and no framing bytes.
        assert b"".join(board.lora.writes) == payload
        assert result.airtime_ms > 0

    def test_arbitrary_byte_values_survive_including_newlines(self):
        # Stream mode is binary transparent. A 0x0A inside a frame would have
        # terminated a command-mode line.
        board = FakeBoard()
        payload = bytes([0x00, 0x0A, 0x0D, 0xFF, 0x2B, 0x1B])
        board.configured().transmit(payload)
        assert b"".join(board.lora.writes) == payload

    def test_each_frame_is_written_in_a_single_call(self):
        # The firmware transmits whatever the ring buffer holds when it polls
        # (main.c:1363-1372), so a frame split across two write() calls could go
        # out as two packets.
        board = FakeBoard()
        board.configured().transmit(frame(50))
        assert len(board.lora.writes) == 1

    def test_configuration_goes_to_the_shell_not_the_lora_port(self):
        board = FakeBoard()
        backend = board.configured()
        shell_writes = len(board.shell.writes)
        backend.transmit(frame())
        assert len(board.shell.writes) == shell_writes

    def test_256_bytes_is_rejected_before_anything_is_written(self):
        board = FakeBoard()
        backend = board.configured()
        with pytest.raises(ValueError, match="256"):
            backend.transmit(bytes(256))
        assert board.lora.writes == []

    def test_255_bytes_is_accepted(self):
        # The full Meshtastic PHY limit, which command mode could not carry.
        board = FakeBoard()
        result = board.configured().transmit(bytes(255))
        assert b"".join(board.lora.writes) == bytes(255)
        assert result.airtime_ms > 0

    def test_a_frame_too_large_for_command_mode_still_transmits(self):
        # 62 bytes was the real command-mode cap: "TX " plus 124 hex characters
        # in command_buffer[128]. Anything past that was silently truncated and
        # still answered "TX Result: 0 (Success)".
        board = FakeBoard()
        payload = frame(100)
        assert len(payload) > cs.COMMAND_MODE_MAX_TX_BYTES
        board.configured().transmit(payload)
        assert b"".join(board.lora.writes) == payload

    def test_large_frames_are_flagged_as_spanning_usb_packets(self):
        board = FakeBoard()
        small = board.configured().transmit(bytes(64))
        assert "USB" not in small.detail

        board2 = FakeBoard()
        large = board2.configured().transmit(bytes(65))
        assert "USB" in large.detail

    def test_a_short_write_raises_rather_than_sending_a_partial_frame(self):
        board = FakeBoard(lora=lora_port(short_write=10))
        with pytest.raises(TransmitError, match="10 of"):
            board.configured().transmit(frame(40))

    def test_empty_frame_is_rejected(self):
        board = FakeBoard()
        backend = board.configured()
        with pytest.raises(ValueError, match="empty"):
            backend.transmit(b"")
        assert board.lora.writes == []

    def test_transmit_before_configure_raises(self):
        board = FakeBoard()
        with pytest.raises(TransmitError, match="not configured"):
            board.backend().transmit(frame())
        assert board.lora.writes == []

    def test_airtime_matches_the_configured_params(self):
        board = FakeBoard()
        payload = frame(40)
        result = board.configured().transmit(payload)
        assert result.airtime_ms == time_on_air_ms(
            len(payload),
            spreading_factor=LONGFAST.spreading_factor,
            bandwidth_khz=LONGFAST.bandwidth_khz,
            coding_rate=LONGFAST.coding_rate,
            preamble_symbols=LONGFAST.preamble_symbols,
        )

    def test_inbound_mesh_traffic_does_not_disturb_transmit(self):
        # Cat-LoRa carries received packets outbound (main.c:98-112). Stream mode
        # never reads that port for a reply, so inbound traffic cannot interfere.
        board = FakeBoard()
        backend = board.configured()
        board.lora._out += b"\x01\x02 inbound packet bytes \xff\r\n"

        payload = frame(40)
        backend.transmit(payload)
        assert b"".join(board.lora.writes) == payload


class TestCatSnifferClose:
    def test_close_twice_does_not_raise(self):
        board = FakeBoard()
        backend = board.backend()
        backend.close()
        backend.close()
        assert board.lora.close_calls == 1
        assert board.shell.close_calls == 1

    def test_transmit_after_close_raises(self):
        board = FakeBoard()
        backend = board.configured()
        backend.close()
        with pytest.raises(TransmitError, match="closed"):
            backend.transmit(frame())

    def test_close_survives_a_port_that_errors(self):
        board = FakeBoard()
        backend = board.backend()

        def explode():
            raise OSError("device disconnected")

        board.lora.close = explode
        backend.close()
        backend.close()

    def test_context_manager_closes(self):
        board = FakeBoard()
        with board.backend():
            pass
        assert board.shell.close_calls == 1


# --------------------------------------------------------------------------
# mqtt
# --------------------------------------------------------------------------


class TestMqttTopic:
    def test_topic_shape(self):
        assert topic_for("US", "LongFast", "!7f001122") == "msh/US/2/e/LongFast/!7f001122"

    def test_backend_publishes_to_that_topic(self):
        client = FakeMqttClient()
        backend = mqtt_backend(client, region="EU_868", channel="LongFast")
        backend.configure(LONGFAST)
        backend.transmit(frame())

        topic, _, _ = client.published[0]
        assert topic == "msh/EU_868/2/e/LongFast/!7f001122"

    def test_gateway_id_override_is_used_for_every_frame(self):
        client = FakeMqttClient()
        backend = mqtt_backend(client, gateway_id="!deadbeef")
        backend.configure(LONGFAST)
        backend.transmit(frame(sender=0x7F00AAAA))

        topic, payload, _ = client.published[0]
        assert topic.endswith("/!deadbeef")
        envelope = mqtt_pb2.ServiceEnvelope.FromString(payload)
        assert envelope.gateway_id == "!deadbeef"

    def test_a_slash_in_a_topic_element_is_refused(self):
        with pytest.raises(ValueError, match="region"):
            topic_for("US/west", "LongFast", "!7f001122")

    def test_a_wildcard_in_a_topic_element_is_refused(self):
        with pytest.raises(ValueError, match="channel"):
            topic_for("US", "Long#Fast", "!7f001122")


class TestMqttEnvelope:
    def header(self) -> PacketHeader:
        return PacketHeader(
            to=0xFFFFFFFF,
            sender=0x7F001122,
            packet_id=0x1234ABCD,
            hop_limit=3,
            hop_start=3,
            want_ack=True,
            channel_hash=0x08,
            next_hop=0x11,
            relay_node=0x22,
        )

    def test_header_fields_round_trip_through_the_envelope(self):
        header = self.header()
        ciphertext = bytes(range(32))
        client = FakeMqttClient()
        backend = mqtt_backend(client, channel="LongFast")
        backend.configure(LONGFAST)

        backend.transmit(header.pack() + ciphertext)

        envelope = mqtt_pb2.ServiceEnvelope.FromString(client.published[0][1])
        packet = envelope.packet
        assert getattr(packet, "from") == header.sender
        assert packet.to == header.to
        assert packet.id == header.packet_id
        assert packet.channel == header.channel_hash
        assert packet.hop_limit == header.hop_limit
        assert packet.hop_start == header.hop_start
        assert packet.want_ack is True
        assert packet.next_hop == header.next_hop
        assert packet.relay_node == header.relay_node
        assert envelope.channel_id == "LongFast"

    def test_encrypted_payload_is_the_frame_minus_the_header(self):
        ciphertext = bytes(range(64))
        envelope = envelope_for(self.header().pack() + ciphertext, "LongFast", "!1")
        assert envelope.packet.encrypted == ciphertext
        assert len(envelope.packet.encrypted) == 64

    def test_the_plaintext_never_leaves_the_encrypted_field(self):
        envelope = envelope_for(self.header().pack() + b"ciphertext", "LongFast", "!1")
        assert not envelope.packet.HasField("decoded")

    def test_a_truncated_frame_is_rejected(self):
        with pytest.raises(ValueError):
            envelope_for(bytes(HEADER_LENGTH - 1), "LongFast", "!1")


class TestMqttLifecycle:
    def test_connection_failure_raises_transmit_error(self):
        client = FakeMqttClient(connect_error=ConnectionRefusedError("refused"))
        with pytest.raises(TransmitError, match="connect"):
            mqtt_backend(client).configure(LONGFAST)

    def test_a_refusing_broker_raises_transmit_error(self):
        client = FakeMqttClient(connect_rc=5)
        with pytest.raises(TransmitError, match="rc=5"):
            mqtt_backend(client).configure(LONGFAST)

    def test_publish_failure_raises_transmit_error(self):
        client = FakeMqttClient(publish_rc=4)
        backend = mqtt_backend(client)
        backend.configure(LONGFAST)
        with pytest.raises(TransmitError, match="rc=4"):
            backend.transmit(frame())

    def test_transmit_before_configure_raises(self):
        with pytest.raises(TransmitError, match="not configured"):
            mqtt_backend(FakeMqttClient()).transmit(frame())

    def test_credentials_are_set_before_connect(self):
        client = FakeMqttClient()
        backend = mqtt_backend(client, username="meshdev", password="large4cats")
        backend.configure(LONGFAST)
        assert client.credentials == ("meshdev", "large4cats")

    def test_close_twice_does_not_raise(self):
        client = FakeMqttClient()
        backend = mqtt_backend(client)
        backend.configure(LONGFAST)
        backend.close()
        backend.close()
        assert client.disconnects == 1
        assert client.loop_stopped == 1

    def test_no_airtime_is_charged(self):
        client = FakeMqttClient()
        backend = mqtt_backend(client)
        backend.configure(LONGFAST)
        # MQTT uses no spectrum, so it must not consume the duty cycle budget.
        assert backend.transmit(frame()).airtime_ms == 0


class TestModuleConstants:
    def test_usb_identity(self):
        assert (cs.VENDOR_ID, cs.PRODUCT_ID) == (0x1209, 0xBABB)

    def test_lora_is_interface_one_and_shell_interface_two(self):
        assert (cs.CDC_LORA, cs.CDC_SHELL) == (1, 2)

    def test_stream_mode_carries_the_full_phy_frame(self):
        assert cs.MAX_TX_BYTES == 255

    def test_the_command_mode_cap_is_recorded_as_62_not_128(self):
        # "TX " plus 124 hex characters is all that fits in command_buffer[128]
        # (main.c:1233, 1355). The uint8_t tx_data[128] buffer is not the binding
        # limit, and mistaking it for one is what made command mode look viable.
        assert cs.COMMAND_MODE_MAX_TX_BYTES == 62
        assert cs.COMMAND_MODE_MAX_TX_BYTES < cs.MAX_TX_BYTES

    def test_the_sync_word_note_names_2b(self):
        assert "0x2B" in cs.SYNC_WORD_NOTE

    def test_topic_schema_version_is_two(self):
        assert mq.TOPIC_SCHEMA_VERSION == "2"
