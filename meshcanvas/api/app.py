"""FastAPI surface: render, budget, transmit, abort, and a WebSocket log.

Binds to localhost by default. This service transmits on real spectrum in rf
mode, so it has no authentication and must not be exposed to a network.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from meshcanvas.api.models import (
    BudgetResponse,
    RadioSettings,
    RenderRequest,
    RenderResponse,
    TransmitRequest,
)
from meshcanvas.api.service import RunState, compute_budget, render_points, run_transmit

WEB_ROOT = Path(__file__).resolve().parent.parent.parent / "web"


class NoCacheStaticFiles(StaticFiles):
    """Serve the frontend without letting the browser cache it.

    These files are edited during a session. A browser that keeps a stale
    index.html while fetching a fresh app.js gets a page whose script queries
    elements the cached markup does not contain, and the resulting null
    dereference kills initialization: no map, no WebSocket, and an error only
    visible in the devtools console. Revalidating every request costs nothing
    on localhost.
    """

    def is_not_modified(self, *args, **kwargs) -> bool:
        return False

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response


class LogHub:
    """Fans run events out to every connected WebSocket."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()

    async def connect(self, socket: WebSocket) -> None:
        await socket.accept()
        self._clients.add(socket)

    def disconnect(self, socket: WebSocket) -> None:
        self._clients.discard(socket)

    async def emit(self, event: dict) -> None:
        for socket in list(self._clients):
            try:
                await socket.send_json(event)
            except (WebSocketDisconnect, RuntimeError):
                self.disconnect(socket)


def create_app(session_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="MeshCanvas", version="0.1.0")
    app.state.hub = LogHub()
    app.state.run = RunState()
    app.state.task: asyncio.Task | None = None
    app.state.session_dir = session_dir

    @app.post("/api/render", response_model=RenderResponse)
    async def render(request: RenderRequest) -> RenderResponse:
        try:
            points = render_points(request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return RenderResponse(
            points=points, node_count=len(points), seed=request.seed
        )

    @app.get("/api/budget", response_model=BudgetResponse)
    async def budget(
        region: str = "US",
        modem_preset: str = "LONG_FAST",
        channel_name: str = "LongFast",
        node_count: int = 50,
        tx_power_dbm: int = 0,
        channel_num: int | None = None,
        psk_base64: str | None = None,
        inter_packet_ms: int | None = None,
        airtime_target_percent: float | None = None,
    ) -> BudgetResponse:
        settings = RadioSettings(
            region=region,
            modem_preset=modem_preset,
            channel_name=channel_name,
            tx_power_dbm=tx_power_dbm,
            channel_num=channel_num,
            psk_base64=psk_base64,
        )
        try:
            return compute_budget(
                settings, node_count, inter_packet_ms, airtime_target_percent
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    @app.post("/api/transmit")
    async def transmit(request: TransmitRequest) -> dict:
        run: RunState = app.state.run
        if run.running:
            raise HTTPException(
                status_code=409, detail="a run is already in progress"
            )

        # Validate before announcing a start so a bad request fails as a 400
        # the caller can act on, rather than an error event after the fact.
        try:
            budget = compute_budget(
                request, request.node_count, request.inter_packet_ms,
                request.airtime_target_percent,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

        if not budget.within_duty_cycle and not request.duty_cycle_override:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"projected duty cycle {budget.duty_cycle_percent}% exceeds "
                    f"the {budget.region_duty_cycle_limit}% limit for "
                    f"{request.region}. Increase inter_packet_ms or set "
                    "duty_cycle_override."
                ),
            )

        app.state.task = asyncio.create_task(
            run_transmit(
                request,
                run,
                app.state.hub.emit,
                session_dir=app.state.session_dir,
            )
        )
        return {"started": True, "mode": request.mode}

    @app.post("/api/abort")
    async def abort() -> dict:
        run: RunState = app.state.run
        run.abort()
        await app.state.hub.emit(
            {"type": "log", "level": "warn", "message": "abort requested"}
        )
        return {"aborted": True, "sent": run.sent}

    @app.get("/api/status")
    async def status() -> dict:
        run: RunState = app.state.run
        return {
            "running": run.running,
            "sent": run.sent,
            "total": run.total,
            "session_csv": str(run.session_csv) if run.session_csv else None,
        }

    @app.websocket("/ws")
    async def websocket(socket: WebSocket) -> None:
        hub: LogHub = app.state.hub
        await hub.connect(socket)
        try:
            await hub.emit({"type": "log", "level": "info", "message": "connected"})
            while True:
                # The client never sends; this keeps the socket open and
                # notices a disconnect.
                await socket.receive_text()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            hub.disconnect(socket)

    if WEB_ROOT.is_dir():
        # Mounted at the root, and last, so the API routes above win. index.html
        # references style.css and app.js relatively, so they have to resolve at
        # "/" and not under a prefix. html=True serves index.html for "/".
        app.mount(
            "/", NoCacheStaticFiles(directory=str(WEB_ROOT), html=True), name="web"
        )

    @app.on_event("shutdown")
    async def shutdown() -> None:
        task: asyncio.Task | None = app.state.task
        if task and not task.done():
            app.state.run.abort()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    return app


app = create_app()
