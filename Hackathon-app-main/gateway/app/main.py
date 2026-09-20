"""App Gateway: the only backend the web app talks to."""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Path as PathParam, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import calib
from .bt import BtClient, BtEngineError
from .cloud import ChatRequest, CloudError, FeedbackRequest, ResetRequest, make_cloud_client
from .config import Settings, get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.bt = BtClient(settings)
        app.state.cloud = make_cloud_client(settings, app.state.bt)
        # Calibration problems must not take the task/feedback features down with them.
        app.state.calib_error = None
        app.state.calib = calib.make_calib_backend(settings)
        try:
            calib.ensure_field_file(settings)
            app.state.calib.start()
        except Exception as e:
            app.state.calib_error = f"{e.__class__.__name__}: {e}"
            log.error("calibration disabled: %s", app.state.calib_error)
        yield
        app.state.calib.stop()
        await app.state.cloud.aclose()
        await app.state.bt.aclose()

    app = FastAPI(title="Hackathon App Gateway", lifespan=lifespan)

    @app.exception_handler(BtEngineError)
    async def _bt_down(_: Request, e: BtEngineError):
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)

    @app.exception_handler(CloudError)
    async def _cloud_down(_: Request, e: CloudError):
        return JSONResponse({"ok": False, "error": str(e)}, status_code=e.status_code)

    # ------------------------------------------------------------ cloud (Manta)

    MissionId = Annotated[str, PathParam(pattern=r"^[A-Za-z0-9_-]{1,100}$")]
    RunId = Annotated[str, PathParam(pattern=r"^[A-Za-z0-9_.-]{1,100}$")]

    @app.get("/api/cloud/health")
    async def cloud_health(request: Request):
        return await request.app.state.cloud.health()

    @app.post("/api/chat")
    async def chat(req: ChatRequest, request: Request):
        return await request.app.state.cloud.chat(req)

    @app.post("/api/sessions/reset")
    async def reset_session(req: ResetRequest, request: Request):
        return await request.app.state.cloud.reset(req)

    @app.post("/api/missions/{mission_id}/cancel")
    async def cancel_mission(mission_id: MissionId, request: Request):
        try:
            return await request.app.state.cloud.cancel(mission_id)
        except CloudError as e:
            # Stopping the robot must not depend on the cloud: fall back to bt_engine directly.
            log.warning("cloud cancel failed (%s), cancelling on bt_engine", e)
            code, body = await request.app.state.bt.cancel()
            return JSONResponse({**body, "fallback": "bt_engine"}, status_code=code)

    @app.post("/api/missions/{mission_id}/feedback")
    async def mission_feedback(mission_id: MissionId, fb: FeedbackRequest, request: Request):
        return await request.app.state.cloud.feedback(mission_id, fb)

    @app.get("/api/runs/{run_id}/mission")
    async def run_mission(run_id: RunId, request: Request):
        rec = request.app.state.cloud.runs.get(run_id)
        if rec is None:
            raise HTTPException(404, "this run was not started from the app")
        return rec

    # ------------------------------------------------------------ BT engine

    def _passthrough(result: tuple[int, object]) -> JSONResponse:
        code, body = result
        return JSONResponse(body, status_code=code)

    @app.get("/api/bt/status")
    async def bt_status_latest(request: Request, trace: str | None = None):
        return _passthrough(await request.app.state.bt.status(full_trace=trace == "full"))

    @app.get("/api/bt/status/{run_id}")
    async def bt_status(run_id: str, request: Request, trace: str | None = None):
        return _passthrough(await request.app.state.bt.status(run_id, full_trace=trace == "full"))

    @app.get("/api/bt/runs")
    async def bt_runs(request: Request):
        return _passthrough(await request.app.state.bt.runs())

    @app.post("/api/bt/cancel")
    async def bt_cancel(request: Request):
        return _passthrough(await request.app.state.bt.cancel())

    @app.get("/api/bt/health")
    async def bt_health(request: Request):
        return _passthrough(await request.app.state.bt.health())

    # ------------------------------------------------------------ calibration

    @app.get("/api/calib/field")
    async def get_field():
        return calib.read_field(settings)

    @app.put("/api/calib/field")
    async def put_field(cfg: calib.FieldConfig):
        try:
            return calib.write_field(settings, cfg)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        except OSError as e:
            raise HTTPException(503, f"cannot write {settings.field_file}: {e.strerror}") from e

    @app.get("/api/calib/state")
    async def calib_state(request: Request):
        backend = request.app.state.calib
        run = calib.latest_run(settings)
        return {
            "mode": backend.mode,
            "error": request.app.state.calib_error,
            "connected": request.app.state.calib_error is None and backend.connected(),
            "running_since": backend.running_since,
            "latest_run": run.name if run else None,
            "streams": {s: (f[1] if (f := backend.frames.get(s)) else None) for s in calib.STREAMS},
            "frame_counts": {s: (f[0] if (f := backend.frames.get(s)) else 0) for s in calib.STREAMS},
            "sources": {s: backend.frames.source(s) for s in calib.STREAMS},
        }

    @app.post("/api/calib/run")
    async def calib_run(request: Request):
        if request.app.state.calib_error:
            raise HTTPException(503, f"calibration disabled: {request.app.state.calib_error}")
        try:
            return await request.app.state.calib.run()
        except calib.CalibBusy as e:
            raise HTTPException(409, str(e)) from e
        except calib.CalibUnavailable as e:
            raise HTTPException(503, str(e)) from e
        except TimeoutError as e:
            raise HTTPException(504, str(e)) from e

    @app.get("/api/calib/runs/latest")
    async def calib_latest_run():
        """Newest run, passed or not: lets a reloaded page pick up a calibration it started."""
        run = calib.latest_run(settings)
        if run is None:
            raise HTTPException(404, "no calibration run yet")
        res = calib.load_result(run / "cam_tf.yaml")
        return {"run": run.name, "success": bool(res and res.get("passed")), "message": "", "result": res}

    @app.get("/api/calib/result")
    async def calib_result():
        res = calib.current_result(settings)
        if res is None:
            raise HTTPException(404, "no applied calibration yet")
        return res

    @app.get("/api/calib/image/{name}")
    async def calib_image(name: str, run: str | None = None):
        if name not in calib.DEBUG_IMAGES:
            raise HTTPException(404, "unknown image")
        if run is not None and not calib.RUN_DIR_RE.match(run):
            raise HTTPException(400, "bad run id")
        run_dir = Path(settings.calib_output_dir) / run if run else calib.latest_run(settings)
        path = run_dir / f"{name}.png" if run_dir else None
        if not path or not path.is_file():
            raise HTTPException(404, "image not found")
        return FileResponse(path, media_type="image/png")

    @app.get("/api/calib/snapshot/{stream}")
    async def calib_snapshot(stream: Literal["live", "calib", "camera"], request: Request):
        frame = request.app.state.calib.frames.get(stream)
        if not frame:
            raise HTTPException(503, f"no {stream} frame yet")
        return Response(frame[2], media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/calib/stream/{stream}")
    async def calib_stream(stream: Literal["live", "calib", "camera"], request: Request):
        frames = request.app.state.calib.frames
        if not frames.get(stream):
            raise HTTPException(503, f"no {stream} frame yet")

        async def gen():
            last = -1
            while not await request.is_disconnected():
                frame = frames.get(stream)
                if frame and frame[0] != last:
                    last = frame[0]
                    yield (
                        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + str(len(frame[2])).encode()
                        + b"\r\n\r\n"
                        + frame[2]
                        + b"\r\n"
                    )
                await asyncio.sleep(0.03)

        return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")

    @app.get("/api/health")
    async def health():
        return {
            "ok": True,
            "cloud_mode": settings.cloud_mode,
            "cloud_url": settings.cloud_url if settings.cloud_mode == "http" else None,
            "mock_execute": settings.mock_execute,
        }

    # ------------------------------------------------------------ web app (SPA)

    static = Path(settings.static_dir)
    if static.is_dir():
        app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            if path.startswith("api/"):
                raise HTTPException(404, "no such endpoint")
            f = (static / path).resolve()
            if path and f.is_file() and f.is_relative_to(static.resolve()):
                return FileResponse(f)
            return FileResponse(static / "index.html")

    return app


app = create_app()
