"""End-to-end frame assembly.

The decisive test here is test_a_receiver_can_decode_our_frame: it takes the
bytes we would put on air and decodes them the way a receiver does, using the
upstream protobuf definitions rather than our own encoder.
"""

from pathlib import Path

import pytest
from meshtastic.protobuf import mesh_pb2, portnums_pb2

from meshcanvas.protocol import crypto
from meshcanvas.protocol.channel import DEFAULT_PSK, channel_hash, expand_psk
from meshcanvas.protocol.header import BROADCAST_ADDR, HEADER_LENGTH, PacketHeader
from meshcanvas.protocol.packet import (
    DEFAULT_NODE_PREFIX,
    build_frame,
    generate_nodes,
    node_num_for,
    nodeinfo_payload,
    packet_id_for,
    position_payload,
    write_session_csv,
)

POINTS = [(19.4326, -99.1332), (19.4330, -99.1340), (19.4340, -99.1350)]


@pytest.fixture
def nodes():
    return generate_nodes(POINTS, seed="test-seed")


class TestNodeIdentity:
    def test_node_numbers_carry_the_prefix(self, nodes):
        for node in nodes:
            assert node.node_num >> 24 == DEFAULT_NODE_PREFIX

    def test_generation_is_deterministic_across_processes(self):
        # BLAKE2b rather than hash(), which is randomized per process and would
        # make the session CSV useless for cleanup.
        assert node_num_for(0, "seed-a") == node_num_for(0, "seed-a")

    def test_different_seeds_give_different_nodes(self):
        assert node_num_for(0, "seed-a") != node_num_for(0, "seed-b")

    def test_node_numbers_are_unique_across_many_points(self):
        many = generate_nodes([(0.0, 0.0)] * 2000, seed="collide")
        assert len({node.node_num for node in many}) == 2000

    def test_never_produces_a_reserved_node_num(self):
        for index in range(500):
            assert node_num_for(index, "s") not in (0, 0xFFFFFFFF)

    def test_node_id_is_the_bang_hex_form(self, nodes):
        assert nodes[0].node_id == f"!{nodes[0].node_num:08x}"
        assert len(nodes[0].node_id) == 9

    def test_short_name_fits_the_protobuf_limit(self, nodes):
        for node in nodes:
            assert len(node.short_name) <= 4

    def test_custom_prefix_is_honoured(self):
        assert node_num_for(0, "s", prefix=0xAB) >> 24 == 0xAB

    def test_prefix_out_of_byte_range_is_rejected(self):
        with pytest.raises(ValueError, match="single byte"):
            node_num_for(0, "s", prefix=256)


class TestSessionCsv:
    def test_writes_one_row_per_node_plus_a_header(self, nodes, tmp_path: Path):
        path = tmp_path / "session.csv"
        write_session_csv(nodes, path)
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == len(nodes) + 1
        assert lines[0].startswith("index,node_num,node_id")

    def test_records_coordinates_at_full_precision(self, nodes, tmp_path: Path):
        path = tmp_path / "session.csv"
        write_session_csv(nodes, path)
        assert "19.4326000" in path.read_text(encoding="utf-8")


class TestPayloads:
    def test_position_encodes_degrees_times_1e7(self, nodes):
        data = mesh_pb2.Data()
        data.ParseFromString(position_payload(nodes[0]))
        assert data.portnum == portnums_pb2.PortNum.POSITION_APP

        position = mesh_pb2.Position()
        position.ParseFromString(data.payload)
        assert position.latitude_i == 194326000
        assert position.longitude_i == -991332000

    def test_position_is_marked_manual_not_gps(self, nodes):
        data = mesh_pb2.Data()
        data.ParseFromString(position_payload(nodes[0]))
        position = mesh_pb2.Position()
        position.ParseFromString(data.payload)
        assert position.location_source == mesh_pb2.Position.LocSource.LOC_MANUAL

    def test_southern_and_western_coordinates_stay_negative(self):
        node = generate_nodes([(-33.8688, -151.2093)], seed="s")[0]
        data = mesh_pb2.Data()
        data.ParseFromString(position_payload(node))
        position = mesh_pb2.Position()
        position.ParseFromString(data.payload)
        assert position.latitude_i == -338688000
        assert position.longitude_i == -1512093000

    def test_nodeinfo_carries_the_user_record(self, nodes):
        data = mesh_pb2.Data()
        data.ParseFromString(nodeinfo_payload(nodes[0]))
        assert data.portnum == portnums_pb2.PortNum.NODEINFO_APP

        user = mesh_pb2.User()
        user.ParseFromString(data.payload)
        assert user.id == nodes[0].node_id
        assert user.long_name == nodes[0].long_name
        assert user.hw_model == mesh_pb2.HardwareModel.Value("PRIVATE_HW")


class TestFrameAssembly:
    def test_frame_is_header_plus_ciphertext(self, nodes):
        payload = position_payload(nodes[0])
        frame = build_frame(
            payload, sender=nodes[0].node_num, packet_id=1,
            channel_name="LongFast", psk=DEFAULT_PSK,
        )
        assert len(frame) == HEADER_LENGTH + len(payload)

    def test_header_channel_byte_matches_the_channel_hash(self, nodes):
        frame = build_frame(
            position_payload(nodes[0]), sender=nodes[0].node_num, packet_id=1,
            channel_name="LongFast", psk=DEFAULT_PSK,
        )
        assert PacketHeader.unpack(frame).channel_hash == 0x08

    def test_defaults_to_broadcast(self, nodes):
        frame = build_frame(
            position_payload(nodes[0]), sender=nodes[0].node_num, packet_id=1,
            channel_name="LongFast", psk=DEFAULT_PSK,
        )
        assert PacketHeader.unpack(frame).to == BROADCAST_ADDR

    def test_hop_start_equals_hop_limit_for_originated_packets(self, nodes):
        frame = build_frame(
            position_payload(nodes[0]), sender=nodes[0].node_num, packet_id=1,
            channel_name="LongFast", psk=DEFAULT_PSK, hop_limit=3,
        )
        header = PacketHeader.unpack(frame)
        assert header.hop_start == header.hop_limit == 3

    def test_oversized_payload_is_rejected(self, nodes):
        with pytest.raises(ValueError, match="on-air limit"):
            build_frame(
                b"x" * 300, sender=nodes[0].node_num, packet_id=1,
                channel_name="LongFast", psk=DEFAULT_PSK,
            )

    def test_a_receiver_can_decode_our_frame(self, nodes):
        """Reverse the whole pipeline the way a receiving node does."""
        node = nodes[0]
        packet_id = packet_id_for(node.node_num, 0, "seed")
        frame = build_frame(
            position_payload(node), sender=node.node_num, packet_id=packet_id,
            channel_name="LongFast", psk=DEFAULT_PSK,
        )

        # 1. Split the cleartext header from the ciphertext.
        header = PacketHeader.unpack(frame)
        ciphertext = frame[HEADER_LENGTH:]

        # 2. Match the channel by hash, then decrypt with that channel's key.
        assert header.channel_hash == channel_hash("LongFast", DEFAULT_PSK)
        plaintext = crypto.decrypt(
            ciphertext, expand_psk(DEFAULT_PSK), header.packet_id, header.sender
        )

        # 3. A successful protobuf decode is how the firmware validates the key.
        data = mesh_pb2.Data()
        data.ParseFromString(plaintext)
        assert data.portnum == portnums_pb2.PortNum.POSITION_APP

        position = mesh_pb2.Position()
        position.ParseFromString(data.payload)
        assert position.latitude_i == int(round(node.latitude * 1e7))
        assert position.longitude_i == int(round(node.longitude * 1e7))
        assert header.sender == node.node_num

    def test_a_real_position_frame_fits_the_128_byte_serial_cap(self, nodes):
        # The CatSniffer TX <hex> path buffers 128 bytes. Position and NodeInfo
        # must fit or the driver has to fall back to the unframed stream path.
        for payload in (position_payload(nodes[0]), nodeinfo_payload(nodes[0])):
            frame = build_frame(
                payload, sender=nodes[0].node_num, packet_id=1,
                channel_name="LongFast", psk=DEFAULT_PSK,
            )
            assert len(frame) <= 128


class TestPacketIds:
    def test_are_never_zero(self):
        assert all(packet_id_for(1, n, "s") != 0 for n in range(200))

    def test_are_deterministic(self):
        assert packet_id_for(7, 3, "s") == packet_id_for(7, 3, "s")

    def test_do_not_repeat_within_a_run(self):
        # A repeat under one key would reuse the AES-CTR keystream.
        ids = {packet_id_for(1, n, "s") for n in range(1000)}
        assert len(ids) == 1000

    def test_fit_in_u32(self):
        assert all(packet_id_for(1, n, "s") <= 0xFFFFFFFF for n in range(200))
