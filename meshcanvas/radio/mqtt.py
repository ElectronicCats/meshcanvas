"""Publish frames to an MQTT broker as Meshtastic ServiceEnvelopes.

No RF is involved: this is the path a gateway node uses to forward traffic, so
it reaches every client subscribed to the mesh's topic without occupying any
spectrum. Duty cycle does not apply and reported airtime is zero.

Our frames are already assembled bytes, so the 16-byte header is parsed back out
to fill the MeshPacket fields and the remainder goes into `packet.encrypted`
untouched. The broker never sees the plaintext: the payload stays encrypted
under the channel PSK exactly as it would be on the air.
"""

from __future__ import annotations

from typing import Callable, Protocol

from meshtastic.protobuf import mesh_pb2, mqtt_pb2

from meshcanvas.protocol.header import HEADER_LENGTH, PacketHeader
from meshcanvas.radio.base import RadioParams, TransmitError, TransmitResult

DEFAULT_PORT = 1883
DEFAULT_TLS_PORT = 8883
DEFAULT_TOPIC_ROOT = "msh"
DEFAULT_KEEPALIVE_S = 60

# The "2" in msh/<region>/2/e/<channel>/<node>: the topic schema version, not the
# protocol version. "e" is the encrypted subtree; "c"/"json" carry cleartext,
# which we never publish.
TOPIC_SCHEMA_VERSION = "2"
TOPIC_ENCRYPTED = "e"

_TOPIC_FORBIDDEN = ("/", "+", "#", "\x00")


class MqttClient(Protocol):
    """The slice of paho-mqtt this backend uses."""

    def username_pw_set(self, username: str, password: str | None = None) -> None: ...

    def connect(self, host: str, port: int, keepalive: int): ...

    def publish(self, topic: str, payload: bytes, qos: int = 0): ...

    def loop_start(self) -> None: ...

    def loop_stop(self) -> None: ...

    def disconnect(self) -> None: ...


def _default_client_factory() -> MqttClient:
    import paho.mqtt.client as paho

    # paho 2.x refuses to build a client without an explicit callback API
    # version; VERSION2 is the only one that is not deprecated.
    return paho.Client(paho.CallbackAPIVersion.VERSION2)


def _check_topic_element(name: str, value: str) -> str:
    if not value:
        raise ValueError(f"{name} must not be empty; it is a topic level")
    for bad in _TOPIC_FORBIDDEN:
        if bad in value:
            raise ValueError(
                f"{name} contains {bad!r}, which would split the topic or make it "
                "a wildcard the broker rejects on publish"
            )
    return value


def topic_for(
    region: str,
    channel: str,
    node_id: str,
    root: str = DEFAULT_TOPIC_ROOT,
) -> str:
    """msh/<region>/2/e/<channel>/<node_id>."""
    return "/".join(
        [
            _check_topic_element("root", root),
            _check_topic_element("region", region),
            TOPIC_SCHEMA_VERSION,
            TOPIC_ENCRYPTED,
            _check_topic_element("channel", channel),
            _check_topic_element("node_id", node_id),
        ]
    )


def node_id_for(node_num: int) -> str:
    """The "!hex" form Meshtastic uses in topics and gateway_id."""
    return f"!{node_num:08x}"


def envelope_for(frame: bytes, channel: str, gateway_id: str) -> mqtt_pb2.ServiceEnvelope:
    """Rebuild the MeshPacket that produced these bytes.

    The header is the only structure available: a gateway forwarding someone
    else's traffic cannot decrypt the payload either, so it copies the ciphertext
    across verbatim.
    """
    header = PacketHeader.unpack(frame)

    packet = mesh_pb2.MeshPacket()
    # "from" is a Python keyword, so the generated field needs setattr.
    setattr(packet, "from", header.sender)
    packet.to = header.to
    packet.id = header.packet_id
    packet.channel = header.channel_hash
    packet.hop_limit = header.hop_limit
    packet.hop_start = header.hop_start
    packet.want_ack = header.want_ack
    packet.via_mqtt = header.via_mqtt
    packet.next_hop = header.next_hop
    packet.relay_node = header.relay_node
    packet.encrypted = frame[HEADER_LENGTH:]

    envelope = mqtt_pb2.ServiceEnvelope()
    envelope.packet.CopyFrom(packet)
    envelope.channel_id = channel
    envelope.gateway_id = gateway_id
    return envelope


class MqttBackend:
    """Publishes frames to a broker instead of transmitting them."""

    name = "mqtt"

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        region: str = "US",
        channel: str = "LongFast",
        gateway_id: str | None = None,
        username: str | None = None,
        password: str | None = None,
        topic_root: str = DEFAULT_TOPIC_ROOT,
        keepalive_s: int = DEFAULT_KEEPALIVE_S,
        qos: int = 0,
        client_factory: Callable[[], MqttClient] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.region = region
        self.channel = channel
        # None means every frame is published under its own sender, so each
        # synthetic node appears to gateway itself rather than sharing one id.
        self.gateway_id = gateway_id
        self.username = username
        self.password = password
        self.topic_root = topic_root
        self.keepalive_s = keepalive_s
        self.qos = qos
        self.params: RadioParams | None = None
        self.published_count = 0

        self._client_factory = client_factory or _default_client_factory
        self._client: MqttClient | None = None

    def __enter__(self) -> "MqttBackend":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def configure(self, params: RadioParams) -> None:
        """Connect to the broker. The radio params are kept for reporting only:
        nothing here touches a PHY."""
        client = self._client_factory()
        if self.username is not None:
            client.username_pw_set(self.username, self.password)

        try:
            result = client.connect(self.host, self.port, self.keepalive_s)
        except (OSError, ValueError) as exc:
            raise TransmitError(
                f"MQTT connect to {self.host}:{self.port} failed: {exc}"
            ) from exc
        if result not in (None, 0):
            raise TransmitError(
                f"MQTT connect to {self.host}:{self.port} refused with rc={result}"
            )

        client.loop_start()
        self._client = client
        self.params = params

    def transmit(self, frame: bytes) -> TransmitResult:
        if self.params is None or self._client is None:
            raise TransmitError("mqtt backend was not configured; call configure() first")

        gateway = self.gateway_id or node_id_for(PacketHeader.unpack(frame).sender)
        topic = topic_for(self.region, self.channel, gateway, self.topic_root)
        payload = envelope_for(frame, self.channel, gateway).SerializeToString()

        try:
            info = self._client.publish(topic, payload, qos=self.qos)
        except (OSError, ValueError) as exc:
            raise TransmitError(f"MQTT publish to {topic} failed: {exc}") from exc

        code = getattr(info, "rc", 0)
        if code:
            raise TransmitError(f"MQTT publish to {topic} failed with rc={code}")

        self.published_count += 1
        # Airtime is zero on purpose. A duty cycle budget that counted MQTT
        # frames would throttle a path that uses no spectrum.
        return TransmitResult(
            frame=bytes(frame),
            airtime_ms=0,
            detail=f"published {len(payload)} bytes to {topic}",
        )

    def close(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        for call in (client.loop_stop, client.disconnect):
            try:
                call()
            except OSError:
                # A broker that vanished still leaves the socket to release, and
                # close() must stay safe to call twice.
                pass
