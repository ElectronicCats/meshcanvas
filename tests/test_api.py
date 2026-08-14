"""API layer: rendering, budgeting and the transmit run loop."""

import asyncio
import base64
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from meshcanvas.api.app import create_app
from meshcanvas.api.models import RadioSettings, RenderRequest, Shape, TransmitRequest
from meshcanvas.api.service import (
    build_bitmap,
    compute_budget,
    decode_psk,
    render_points,
    RunState,
    run_transmit,
)
from meshcanvas.protocol.channel import DEFAULT_PSK
from meshcanvas.radio.null import NullBackend

SQUARE = [(0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8)]
# A short freehand stroke rasterizes to a thin line of a few hundred pixels, so
# a node count in the thousands is genuinely impossible for it.
TINY_STROKE = [(19.4326, -99.1332), (19.4327, -99.1331)]


def render_request(**overrides) -> RenderRequest:
    fields = dict(
        shape=Shape(type="polygon", vertices=SQUARE),
        center=(19.4326, -99.1332),
        scale_m=1000.0,
        node_count=20,
    )
    fields.update(overrides)
    return RenderRequest(**fields)


def transmit_request(**overrides) -> TransmitRequest:
    fields = dict(
        shape=Shape(type="polygon", vertices=SQUARE),
        center=(19.4326, -99.1332),
        scale_m=1000.0,
        node_count=3,
        mode="dry-run",
        # 1 ms pacing keeps the suite fast but breaches every region's duty
        # cycle, so these runs opt out deliberately. The guard itself is
        # covered by test_duty_cycle_breach_is_refused.
        inter_packet_ms=1,
        duty_cycle_override=True,
    )
    fields.update(overrides)
    return TransmitRequest(**fields)


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(session_dir=tmp_path)) as test_client:
        yield test_client


class TestPskDecoding:
    def test_unset_psk_is_the_default_channel_shorthand(self):
        # Matches a stock node, whose stored PSK is the single byte 0x01.
        assert decode_psk(None) == b"\x01"

    def test_empty_string_disables_encryption(self):
        assert decode_psk("") == b""

    def test_base64_round_trips(self):
        assert decode_psk(base64.b64encode(DEFAULT_PSK).decode()) == DEFAULT_PSK

    def test_invalid_base64_is_rejected(self):
        with pytest.raises(ValueError, match="base64"):
            decode_psk("not base64!!")


class TestRendering:
    def test_returns_exactly_the_requested_point_count(self):
        assert len(render_points(render_request(node_count=37))) == 37

    def test_is_deterministic_for_a_seed(self):
        assert render_points(render_request()) == render_points(render_request())

    def test_points_are_near_the_requested_centre(self):
        points = render_points(render_request())
        lats = [lat for lat, _ in points]
        lons = [lon for _, lon in points]
        assert abs(sum(lats) / len(lats) - 19.4326) < 0.02
        assert abs(sum(lons) / len(lons) + 99.1332) < 0.02

    def test_more_nodes_than_pixels_is_rejected(self):
        with pytest.raises(ValueError, match="active pixels"):
            render_points(render_request(
                shape=Shape(type="freehand", paths=[TINY_STROKE]),
                node_count=2000,
            ))

    def test_text_shape_renders(self):
        assert len(render_points(render_request(
            shape=Shape(type="text", text="MESH"), node_count=25
        ))) == 25

    def test_image_shape_renders(self):
        buffer = io.BytesIO()
        Image.new("L", (32, 32), color=0).save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode()
        bitmap = build_bitmap(
            Shape(type="image", image={"data_base64": encoded})
        )
        assert bitmap.any()

    def test_primitives_render(self):
        for shape in (
            Shape(type="circle"), Shape(type="grid"), Shape(type="star")
        ):
            assert build_bitmap(shape).any()

    def test_polygon_with_two_vertices_is_rejected(self):
        with pytest.raises(ValueError, match="3 points"):
            build_bitmap(
                Shape(type="polygon", vertices=[(19.4326, -99.1332), (19.4336, -99.1322)])
            )

    def test_empty_text_is_rejected(self):
        with pytest.raises(ValueError, match="non-empty"):
            build_bitmap(Shape(type="text", text="   "))


class TestBudget:
    def test_reports_the_us_longfast_frequency(self):
        assert compute_budget(RadioSettings(), 10).frequency_hz == 906_875_000

    def test_two_frames_per_node(self):
        assert compute_budget(RadioSettings(), 25).packet_count == 50

    def test_default_pacing_stays_within_the_duty_cycle(self):
        for region in ("US", "EU_868", "ANZ"):
            budget = compute_budget(RadioSettings(region=region), 20)
            assert budget.within_duty_cycle
            assert budget.duty_cycle_percent <= budget.region_duty_cycle_limit

    def test_eu868_pacing_reflects_its_ten_percent_limit(self):
        eu = compute_budget(RadioSettings(region="EU_868"), 10)
        assert eu.airtime_target_percent == 10.0
        assert eu.inter_packet_ms >= eu.toa_ms_per_packet * 10

    def test_an_unrestricted_region_still_paces_to_the_default_target(self):
        # US permits 100 percent, which would mean keying the transmitter
        # continuously. The default target halves that.
        us = compute_budget(RadioSettings(region="US"), 10)
        assert us.region_duty_cycle_limit == 100.0
        assert us.airtime_target_percent == 50.0
        assert us.duty_cycle_percent <= 51.0

    def test_the_region_limit_is_a_ceiling_the_target_cannot_raise(self):
        eu = compute_budget(
            RadioSettings(region="EU_868"), 10, airtime_target_percent=90.0
        )
        assert eu.airtime_target_percent == 10.0

    def test_a_lower_target_slows_the_run_down(self):
        fast = compute_budget(RadioSettings(region="US"), 10)
        slow = compute_budget(
            RadioSettings(region="US"), 10, airtime_target_percent=5.0
        )
        assert slow.inter_packet_ms > fast.inter_packet_ms
        assert slow.eta_seconds > fast.eta_seconds

    def test_forcing_a_short_gap_breaches_the_duty_cycle(self):
        budget = compute_budget(
            RadioSettings(region="EU_868"), 10, inter_packet_ms=1
        )
        assert not budget.within_duty_cycle

    def test_tx_power_is_clamped_to_the_region_limit(self):
        assert compute_budget(
            RadioSettings(region="EU_868", tx_power_dbm=30), 5
        ).tx_power_dbm == 27

    def test_reports_the_channel_hash(self):
        assert compute_budget(RadioSettings(), 5).channel_hash == 0x08

    def test_short_turbo_is_far_cheaper_than_longfast(self):
        fast = compute_budget(RadioSettings(modem_preset="LONG_FAST"), 10)
        turbo = compute_budget(
            RadioSettings(modem_preset="SHORT_TURBO", channel_name="ShortTurbo"), 10
        )
        assert turbo.total_airtime_ms < fast.total_airtime_ms / 10

    def test_unknown_region_is_rejected(self):
        with pytest.raises(ValueError, match="unknown region"):
            compute_budget(RadioSettings(region="ATLANTIS"), 5)

    def test_unknown_preset_is_rejected(self):
        with pytest.raises(ValueError, match="unknown modem preset"):
            compute_budget(RadioSettings(modem_preset="ULTRA_FAST"), 5)


class TestTransmitRun:
    async def _run(self, request, backend, tmp_path):
        events = []
        state = RunState()

        async def emit(event):
            events.append(event)

        await run_transmit(
            request, state, emit, backend=backend, session_dir=tmp_path
        )
        return events, state

    def test_sends_nodeinfo_and_position_per_node(self, tmp_path):
        backend = NullBackend()
        events, state = asyncio.run(
            self._run(transmit_request(node_count=3), backend, tmp_path)
        )
        assert state.sent == 6
        assert len(backend.sent) == 6
        assert events[-1]["type"] == "done"

    def test_nodeinfo_precedes_position_for_each_node(self, tmp_path):
        events, _ = asyncio.run(
            self._run(transmit_request(node_count=2), NullBackend(), tmp_path)
        )
        kinds = [e["kind"] for e in events if e["type"] == "progress"]
        assert kinds == ["nodeinfo", "position", "nodeinfo", "position"]

    def test_writes_a_session_csv(self, tmp_path):
        _, state = asyncio.run(
            self._run(transmit_request(node_count=3), NullBackend(), tmp_path)
        )
        assert state.session_csv.exists()
        assert len(state.session_csv.read_text().strip().splitlines()) == 4

    def test_position_only_mode_halves_the_frames(self, tmp_path):
        backend = NullBackend()
        asyncio.run(
            self._run(
                transmit_request(node_count=4, send_nodeinfo=False),
                backend, tmp_path,
            )
        )
        assert len(backend.sent) == 4

    def test_duty_cycle_breach_is_refused(self, tmp_path):
        backend = NullBackend()
        events, _ = asyncio.run(self._run(
            transmit_request(
                region="EU_868", node_count=5, inter_packet_ms=1,
                duty_cycle_override=False,
            ),
            backend, tmp_path,
        ))
        assert events[-1]["type"] == "error"
        assert "duty cycle" in events[-1]["message"]
        assert backend.sent == []

    def test_duty_cycle_breach_can_be_overridden_deliberately(self, tmp_path):
        backend = NullBackend()
        events, _ = asyncio.run(self._run(
            transmit_request(
                region="EU_868", node_count=2, inter_packet_ms=1,
                duty_cycle_override=True,
            ),
            backend, tmp_path,
        ))
        assert events[-1]["type"] == "done"
        assert len(backend.sent) == 4

    def test_abort_stops_mid_run(self, tmp_path):
        backend = NullBackend()
        state = RunState()
        events = []

        async def emit(event):
            events.append(event)
            if event.get("type") == "progress" and event["sent"] == 2:
                state.abort()

        asyncio.run(run_transmit(
            transmit_request(node_count=10), state, emit,
            backend=backend, session_dir=tmp_path,
        ))
        assert state.sent == 2
        assert events[-1]["type"] == "done"
        assert "abort" in events[-1]["message"].lower()

    def test_frames_are_decodable_meshtastic_packets(self, tmp_path):
        from meshtastic.protobuf import mesh_pb2, portnums_pb2

        from meshcanvas.protocol import crypto
        from meshcanvas.protocol.channel import expand_psk
        from meshcanvas.protocol.header import HEADER_LENGTH, PacketHeader

        backend = NullBackend()
        asyncio.run(self._run(
            transmit_request(node_count=1, psk_base64=base64.b64encode(
                DEFAULT_PSK).decode()),
            backend, tmp_path,
        ))

        header = PacketHeader.unpack(backend.sent[1])
        plaintext = crypto.decrypt(
            backend.sent[1][HEADER_LENGTH:], expand_psk(DEFAULT_PSK),
            header.packet_id, header.sender,
        )
        data = mesh_pb2.Data()
        data.ParseFromString(plaintext)
        assert data.portnum == portnums_pb2.PortNum.POSITION_APP

    def test_precision_bits_reaches_the_position_payload(self, tmp_path):
        from meshtastic.protobuf import mesh_pb2

        from meshcanvas.protocol import crypto
        from meshcanvas.protocol.channel import expand_psk
        from meshcanvas.protocol.header import HEADER_LENGTH, PacketHeader

        backend = NullBackend()
        asyncio.run(self._run(
            transmit_request(node_count=1, precision_bits=14), backend, tmp_path
        ))
        header = PacketHeader.unpack(backend.sent[1])
        plaintext = crypto.decrypt(
            backend.sent[1][HEADER_LENGTH:], expand_psk(b"\x01"),
            header.packet_id, header.sender,
        )
        data = mesh_pb2.Data()
        data.ParseFromString(plaintext)
        position = mesh_pb2.Position()
        position.ParseFromString(data.payload)
        assert position.precision_bits == 14

    def test_synthetic_nodes_carry_the_configured_prefix(self, tmp_path):
        from meshcanvas.protocol.header import PacketHeader

        backend = NullBackend()
        asyncio.run(self._run(
            transmit_request(node_count=3, node_prefix=0xAB), backend, tmp_path
        ))
        for frame in backend.sent:
            assert PacketHeader.unpack(frame).sender >> 24 == 0xAB


class TestHttpEndpoints:
    def test_render_returns_points(self, client):
        response = client.post("/api/render", json={
            "shape": {"type": "polygon", "vertices": SQUARE},
            "center": [19.4326, -99.1332], "scale_m": 1000, "node_count": 12,
        })
        assert response.status_code == 200
        assert len(response.json()["points"]) == 12

    def test_render_rejects_an_impossible_request(self, client):
        response = client.post("/api/render", json={
            "shape": {"type": "freehand", "paths": [TINY_STROKE]},
            "center": [19.4326, -99.1332], "scale_m": 1000, "node_count": 2000,
        })
        assert response.status_code == 400

    def test_budget_endpoint_reports_frequency_and_duty_cycle(self, client):
        body = client.get("/api/budget", params={
            "region": "US", "modem_preset": "LONG_FAST", "node_count": 10,
        }).json()
        assert body["frequency_hz"] == 906_875_000
        assert body["region_duty_cycle_limit"] == 100
        assert body["region_power_limit_dbm"] == 30

    def test_budget_rejects_an_unknown_region(self, client):
        assert client.get(
            "/api/budget", params={"region": "ATLANTIS"}
        ).status_code == 400

    def test_transmit_starts_and_status_reports_progress(self, client):
        response = client.post("/api/transmit", json={
            "shape": {"type": "polygon", "vertices": SQUARE},
            "center": [19.4326, -99.1332], "scale_m": 1000,
            "node_count": 2, "mode": "dry-run", "inter_packet_ms": 1,
            "duty_cycle_override": True,
        })
        assert response.status_code == 200
        assert response.json()["started"] is True

    def test_transmit_at_default_pacing_needs_no_override(self, client):
        response = client.post("/api/transmit", json={
            "shape": {"type": "polygon", "vertices": SQUARE},
            "center": [19.4326, -99.1332], "scale_m": 1000,
            "node_count": 2, "mode": "dry-run",
        })
        assert response.status_code == 200

    def test_transmit_rejects_a_duty_cycle_breach_as_400(self, client):
        response = client.post("/api/transmit", json={
            "shape": {"type": "polygon", "vertices": SQUARE},
            "center": [19.4326, -99.1332], "scale_m": 1000,
            "node_count": 5, "mode": "dry-run",
            "region": "EU_868", "inter_packet_ms": 1,
        })
        assert response.status_code == 400
        assert "duty cycle" in response.json()["detail"]

    def test_abort_is_always_answerable(self, client):
        assert client.post("/api/abort").json()["aborted"] is True

    def test_websocket_receives_events(self, client):
        with client.websocket_connect("/ws") as socket:
            assert socket.receive_json()["type"] == "log"

    def test_index_is_served(self, client):
        assert client.get("/").status_code == 200

    @pytest.mark.parametrize(
        "path,content_type",
        [("/style.css", "text/css"), ("/app.js", "javascript")],
    )
    def test_assets_resolve_at_the_paths_index_html_asks_for(
        self, client, path, content_type
    ):
        # index.html references these relatively, so they must resolve at the
        # root. Serving them only under a prefix renders the page as unstyled
        # HTML with no map, and nothing in the server logs says why.
        response = client.get(path)
        assert response.status_code == 200
        assert content_type in response.headers["content-type"]
        assert len(response.content) > 0

    def test_every_asset_index_html_references_locally_is_reachable(self, client):
        import re

        html = client.get("/").text
        local = [
            ref
            for ref in re.findall(r'(?:href|src)="([^"]+)"', html)
            if not ref.startswith(("http://", "https://", "//", "data:", "#"))
        ]
        assert local, "index.html references no local assets, which is suspicious"
        for ref in local:
            assert client.get("/" + ref.lstrip("/")).status_code == 200, ref

    def test_the_static_mount_does_not_shadow_the_api(self, client):
        # The mount sits at "/", so a route ordering mistake would turn every
        # API call into a 404 from StaticFiles.
        assert client.get("/api/status").status_code == 200
        assert client.get("/api/budget", params={"node_count": 5}).status_code == 200


class TestMultiStrokeFreehand:
    """A freehand drawing is usually several strokes, not one.

    The frontend used to overwrite its vertex list on every new stroke, so only
    the last one survived. Strokes now travel as `paths`.
    """

    A = [(19.4326, -99.1332), (19.4336, -99.1332), (19.4346, -99.1332)]
    B = [(19.4326, -99.1322), (19.4336, -99.1322), (19.4346, -99.1322)]

    def test_two_strokes_cover_more_than_one(self):
        one = build_bitmap(Shape(type="freehand", paths=[self.A]), 19.43)
        two = build_bitmap(Shape(type="freehand", paths=[self.A, self.B]), 19.43)
        assert two.sum() > one.sum() * 1.8

    def test_strokes_share_one_bounding_box(self):
        # Normalizing each stroke separately would stack them, so a second
        # stroke to the east must widen the bitmap.
        one = build_bitmap(Shape(type="freehand", paths=[self.A]), 19.43)
        two = build_bitmap(Shape(type="freehand", paths=[self.A, self.B]), 19.43)
        assert two.shape[1] > one.shape[1]

    def test_strokes_stay_separated(self):
        # Two parallel vertical strokes must leave a gap between them.
        bitmap = build_bitmap(Shape(type="freehand", paths=[self.A, self.B]), 19.43)
        columns = bitmap.any(axis=0)
        assert not columns.all(), "the strokes merged into one solid band"

    def test_a_single_vertices_list_still_works(self):
        assert build_bitmap(
            Shape(type="freehand", vertices=self.A), 19.43
        ).any()

    def test_paths_take_precedence_over_vertices(self):
        both = build_bitmap(
            Shape(type="freehand", paths=[self.A, self.B], vertices=self.A), 19.43
        )
        only = build_bitmap(Shape(type="freehand", paths=[self.A, self.B]), 19.43)
        assert both.shape == only.shape

    def test_a_two_point_stroke_is_enough_for_freehand(self):
        assert build_bitmap(
            Shape(type="freehand", paths=[[(19.4326, -99.1332), (19.4336, -99.1322)]]),
            19.43,
        ).any()

    def test_a_polygon_still_needs_three_points(self):
        with pytest.raises(ValueError, match="3 points"):
            build_bitmap(
                Shape(type="polygon", vertices=[(19.4326, -99.1332), (19.4336, -99.1322)]),
                19.43,
            )

    def test_empty_paths_are_rejected(self):
        with pytest.raises(ValueError, match="at least one path"):
            build_bitmap(Shape(type="freehand", paths=[]), 19.43)

    def test_renders_end_to_end_through_the_api(self, client):
        response = client.post("/api/render", json={
            "shape": {"type": "freehand", "paths": [self.A, self.B]},
            "center": [19.4336, -99.1327], "scale_m": 1000, "node_count": 40,
        })
        assert response.status_code == 200
        assert len(response.json()["points"]) == 40


class TestManualChannelNum:
    """A pinned frequency slot must win over the name-derived one.

    Meshtastic derives the slot from djb2 of the channel name unless
    loraConfig.channel_num pins it. A config that pins slot 31 lands on 917.25
    MHz; deriving from the channel name instead gives a different frequency, and
    nothing reports the miss: the receiving node simply never hears anything.
    """

    def settings(self, **kw):
        base = dict(
            region="US", modem_preset="SHORT_TURBO", channel_name="labmesh"
        )
        base.update(kw)
        return RadioSettings(**base)

    def test_pinned_slot_sets_the_frequency(self):
        assert compute_budget(
            self.settings(channel_num=31), 10
        ).frequency_hz == 917_250_000

    def test_without_a_pin_the_name_decides(self):
        assert compute_budget(self.settings(), 10).frequency_hz == 902_750_000

    def test_the_pin_is_one_based(self):
        # Slot 1 is the bottom of the band plus half a bandwidth.
        assert compute_budget(
            self.settings(channel_num=1), 10
        ).frequency_hz == 902_250_000

    def test_slot_index_reported_is_zero_based(self):
        assert compute_budget(self.settings(channel_num=31), 10).channel_slot == 30

    def test_the_pin_survives_a_channel_rename(self):
        a = compute_budget(self.settings(channel_num=31), 10).frequency_hz
        b = compute_budget(
            self.settings(channel_name="otherlab", channel_num=31), 10
        ).frequency_hz
        assert a == b == 917_250_000

    def test_a_zero_slot_is_rejected(self):
        with pytest.raises(Exception):
            RadioSettings(channel_num=0)

    def test_the_budget_endpoint_accepts_it(self, client):
        body = client.get("/api/budget", params={
            "region": "US", "modem_preset": "SHORT_TURBO",
            "channel_name": "labmesh", "channel_num": 31, "node_count": 10,
        }).json()
        assert body["frequency_hz"] == 917_250_000
        assert body["spreading_factor"] == 7
        assert body["bandwidth_khz"] == 500.0


class TestPskReachesTheFrameOverJson:
    """The PSK must survive the JSON -> model -> frame path.

    The object-based tests set psk_base64 directly and so never caught that the
    frontend sent the key under the field name `psk`, which Pydantic dropped:
    the default key went on air with a different channel hash than the intended
    one, silently. These tests exercise the real field name over HTTP/JSON.

    Vectors for channel "meshcanvas": the example key hashes to 0x04, the default
    key to 0x19; these differ, which is what makes the regression detectable.
    """

    CHANNEL = "meshcanvas"
    KEY = "bWVzaGNhbnZhcy1sYWItaw=="
    KEY_HASH = 0x04
    DEFAULT_HASH = 0x19

    def test_budget_hash_reflects_the_psk(self, client):
        with_key = client.get("/api/budget", params={
            "region": "US", "modem_preset": "LONG_FAST",
            "channel_name": self.CHANNEL,
            "psk_base64": self.KEY, "node_count": 10,
        }).json()
        assert with_key["channel_hash"] == self.KEY_HASH

    def test_budget_without_a_psk_uses_the_default_key(self, client):
        without = client.get("/api/budget", params={
            "region": "US", "modem_preset": "LONG_FAST",
            "channel_name": self.CHANNEL, "node_count": 10,
        }).json()
        assert without["channel_hash"] == self.DEFAULT_HASH

    def test_the_wrong_field_name_is_ignored_and_is_a_regression(self, client):
        # This is the exact bug: `psk` instead of `psk_base64`. The key is
        # dropped and the default hash results. The test documents that the
        # field name is load-bearing.
        wrong = client.get("/api/budget", params={
            "region": "US", "modem_preset": "LONG_FAST",
            "channel_name": self.CHANNEL,
            "psk": self.KEY, "node_count": 10,
        }).json()
        # The default hash, not the key's: proves the field name matters.
        assert wrong["channel_hash"] == self.DEFAULT_HASH

    def test_transmitted_frame_carries_the_psk_hash(self, tmp_path):
        # Build the request the way the HTTP layer does: from a JSON dict with
        # the documented field names, then run it and read the real frame.
        from meshcanvas.protocol.header import PacketHeader

        request = TransmitRequest.model_validate({
            "shape": {"type": "polygon", "vertices": SQUARE},
            "center": [36.13472, -115.16172], "scale_m": 1000,
            "node_count": 1, "mode": "dry-run",
            "region": "US", "modem_preset": "LONG_FAST",
            "channel_name": self.CHANNEL,
            "psk_base64": self.KEY,
            "inter_packet_ms": 1, "duty_cycle_override": True,
        })
        backend = NullBackend()
        state = RunState()

        async def emit(event):
            pass

        asyncio.run(run_transmit(request, state, emit, backend=backend,
                                 session_dir=tmp_path))
        assert backend.sent, "nothing was transmitted"
        for frame in backend.sent:
            assert PacketHeader.unpack(frame).channel_hash == self.KEY_HASH
