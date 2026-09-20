"""Camera extrinsic calibration through field_calib_node (localization server).

- Table size: field_calib_node re-reads its field YAML on every calibration, so the gateway
  writes FIELD_FILE and field_calib must be launched with field_file:=<same file>.
- Trigger: std_srvs/Trigger on CALIB_SERVICE (blocks for several seconds).
- Images: field_calib's overlays, preferring its compressed topics (live.compact: camera-size JPEG,
  >= 10 Hz) and falling back to the raw Image topics when no compressed frame arrives; plus the
  camera's own compressed stream (30 Hz, throttled and shrunk) for framing the table. All MJPEG.
- Result: <output_dir>/<YYYYmmdd_HHMMSS>/cam_tf.yaml for every run, <output_dir>/cam_tf.yaml when it passed.
"""
import asyncio
import logging
import re
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from .config import Settings

log = logging.getLogger(__name__)

RUN_DIR_RE = re.compile(r"^\d{8}_\d{6}$")
SEGMENT_RE = re.compile(r"^(far|near|left_\d+|right_\d+|seam_\d+)$")
STREAMS = ("live", "calib", "camera")
DEBUG_IMAGES = ("final_overlay", "final_strips", "final_residuals")


# ---------------------------------------------------------------- field YAML

class FieldConfig(BaseModel):
    length: float = Field(gt=0.1, le=10.0, description="table length along x (m)")
    depth: float = Field(gt=0.1, le=10.0, description="table depth along y (m)")
    count: int = Field(ge=1, le=10, description="tables butted along y")
    disabled_segments: list[str] = []


def _load_yaml(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a YAML mapping")
    return data


def _field_source(settings: Settings) -> dict[str, Any]:
    for p in (settings.field_file, settings.field_template):
        if Path(p).is_file():
            return _load_yaml(Path(p))
    return {"table": {"length": 1.8, "depth": 0.6, "count": 2}, "disabled_segments": []}


def read_field(settings: Settings) -> FieldConfig:
    data = _field_source(settings)
    table = data.get("table") or {}
    return FieldConfig(
        length=table.get("length", 1.8),
        depth=table.get("depth", 0.6),
        count=table.get("count", 1),
        disabled_segments=list(data.get("disabled_segments") or []),
    )


def ensure_field_file(settings: Settings) -> None:
    """field_calib_node is launched with field_file:=FIELD_FILE, so it must exist before any edit."""
    if not Path(settings.field_file).is_file():
        write_field(settings, read_field(settings))
        log.info("seeded %s from %s", settings.field_file, settings.field_template)


def write_field(settings: Settings, cfg: FieldConfig) -> FieldConfig:
    bad = [s for s in cfg.disabled_segments if not SEGMENT_RE.match(s)]
    if bad:
        raise ValueError(f"unknown segment names: {', '.join(bad)}")
    # Keep the tuning keys (corner_exclusion, seam_weight, ...) from the current file.
    data = _field_source(settings)
    data["table"] = {"length": cfg.length, "depth": cfg.depth, "count": cfg.count}
    data["disabled_segments"] = cfg.disabled_segments
    path = Path(settings.field_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("# Written by Hackathon-app gateway. field_calib_node reads it on every calibration.\n")
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    tmp.replace(path)
    return cfg


# ---------------------------------------------------------------- results

def run_dirs(output_dir: Path) -> list[Path]:
    if not output_dir.is_dir():
        return []
    return sorted(p for p in output_dir.iterdir() if p.is_dir() and RUN_DIR_RE.match(p.name))


def load_result(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    data = _load_yaml(path)
    data.pop("debug_dir", None)  # container path, meaningless to the browser
    return data


def current_result(settings: Settings) -> dict[str, Any] | None:
    """The applied calibration (only written when a run passed)."""
    return load_result(Path(settings.calib_output_dir) / "cam_tf.yaml")


def latest_run(settings: Settings) -> Path | None:
    dirs = run_dirs(Path(settings.calib_output_dir))
    return dirs[-1] if dirs else None


# ---------------------------------------------------------------- backends

class CalibBusy(Exception):
    pass


class CalibUnavailable(Exception):
    pass


# How long a compressed frame keeps the raw topic of the same stream muted. Long enough to
# bridge one missed frame at ~1 Hz, short enough to fall back quickly when compact is turned off.
COMPRESSED_GRACE_S = 3.0


def use_raw(last_compressed_at: float | None, now: float, grace: float = COMPRESSED_GRACE_S) -> bool:
    """Raw overlays are only decoded while no compressed frame is coming in (field_calib without live.compact)."""
    return last_compressed_at is None or now - last_compressed_at > grace


class FrameBuffer:
    """Latest JPEG per stream, shared between the ROS thread and asyncio."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frames: dict[str, tuple[int, float, bytes]] = {}
        self._sources: dict[str, str] = {}

    def put(self, stream: str, jpeg: bytes, source: str = "raw") -> None:
        with self._lock:
            seq = self._frames.get(stream, (0, 0.0, b""))[0] + 1
            self._frames[stream] = (seq, time.time(), jpeg)
            self._sources[stream] = source

    def source(self, stream: str) -> str | None:
        with self._lock:
            return self._sources.get(stream)

    def get(self, stream: str) -> tuple[int, float, bytes] | None:
        with self._lock:
            return self._frames.get(stream)


class CalibBackend(ABC):
    mode = "base"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.frames = FrameBuffer()
        self._lock = asyncio.Lock()
        self.running_since: float | None = None

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    @abstractmethod
    async def _trigger(self) -> tuple[bool, str]: ...

    def connected(self) -> bool:
        return True

    async def run(self) -> dict[str, Any]:
        if self._lock.locked():
            raise CalibBusy("calibration already running")
        async with self._lock:
            before = latest_run(self.settings)
            self.running_since = time.time()
            try:
                success, message = await self._trigger()
            finally:
                self.running_since = None
            after = latest_run(self.settings)
            new_run = after if after and after != before else None
            result = load_result(new_run / "cam_tf.yaml") if new_run else None
            return {
                "success": success,
                "message": message,
                "run": new_run.name if new_run else None,
                "result": result,
            }


class MockCalibBackend(CalibBackend):
    """No ROS: pretends to calibrate and reports the newest result already on disk."""

    mode = "mock"

    async def _trigger(self) -> tuple[bool, str]:
        await asyncio.sleep(3.0)
        run = latest_run(self.settings)
        res = load_result(run / "cam_tf.yaml") if run else None
        if res is None:
            return False, "mock mode: no calibration result on disk"
        return bool(res.get("passed")), f"mock mode: replayed {run.name}"

    async def run(self) -> dict[str, Any]:
        out = await super().run()
        if out["run"] is None and (run := latest_run(self.settings)):
            # Nothing new is written in mock mode: show the replayed run.
            out.update(run=run.name, result=load_result(run / "cam_tf.yaml"))
        return out

    def connected(self) -> bool:
        return False


class RosCalibBackend(CalibBackend):
    mode = "ros"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self._thread: threading.Thread | None = None
        self._node = None
        self._executor = None
        self._client = None

    def start(self) -> None:
        import rclpy
        from rclpy.executors import MultiThreadedExecutor
        from rclpy.qos import qos_profile_sensor_data
        from sensor_msgs.msg import CompressedImage, Image
        from std_srvs.srv import Trigger

        rclpy.init()
        self._compressed_at: dict[str, float] = {}
        self._node = rclpy.create_node("hackathon_app_gateway")
        self._client = self._node.create_client(Trigger, self.settings.calib_service)
        topics = {"live": self.settings.calib_live_topic, "calib": self.settings.calib_calib_topic}
        for stream, topic in topics.items():
            # field_calib with live.compact publishes camera-size JPEG next to the raw overlay;
            # that one is passed through untouched, and mutes the (expensive) raw one.
            self._node.create_subscription(
                CompressedImage, f"{topic}/compressed", lambda msg, s=stream: self._on_overlay_jpeg(s, msg),
                qos_profile_sensor_data,
            )
            self._node.create_subscription(
                Image, topic, lambda msg, s=stream: self._on_image(s, msg), qos_profile_sensor_data
            )
        self._camera_period = 1.0 / max(0.5, self.settings.camera_stream_fps)
        self._camera_last = 0.0
        self._node.create_subscription(
            CompressedImage, self.settings.calib_camera_topic, self._on_camera, qos_profile_sensor_data
        )
        self._executor = MultiThreadedExecutor()
        self._executor.add_node(self._node)
        self._thread = threading.Thread(target=self._executor.spin, daemon=True, name="ros-spin")
        self._thread.start()
        log.info("ROS calibration backend up: service %s", self.settings.calib_service)

    def stop(self) -> None:
        import rclpy

        if self._executor:
            self._executor.shutdown()
        if self._node:
            self._node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()

    def _on_image(self, stream: str, msg) -> None:
        import cv2
        import numpy as np

        if not use_raw(self._compressed_at.get(stream), time.monotonic()):
            return
        try:
            channels = {"bgr8": 3, "rgb8": 3, "mono8": 1}.get(msg.encoding)
            if channels is None:
                return
            buf = np.frombuffer(bytes(msg.data), np.uint8).reshape(msg.height, msg.step)
            img = buf[:, : msg.width * channels].reshape(msg.height, msg.width, channels)
            if msg.encoding == "rgb8":
                img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
            ok, jpeg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, self.settings.stream_jpeg_quality])
            if ok:
                self.frames.put(stream, jpeg.tobytes())
        except Exception:  # never kill the executor thread over one bad frame
            log.exception("failed to encode %s frame", stream)

    def _on_overlay_jpeg(self, stream: str, msg) -> None:
        """Compact overlay from field_calib: already JPEG, so just hand it on."""
        self._compressed_at[stream] = time.monotonic()
        self.frames.put(stream, bytes(msg.data), "compressed")

    def _on_camera(self, msg) -> None:
        """Camera JPEG (30 Hz): keep camera_stream_fps of them, shrunk to camera_stream_max_width."""
        import cv2
        import numpy as np

        now = time.monotonic()
        # 10% slack: at 30 Hz input, 15 fps must keep every 2nd frame, not every 3rd.
        if now - self._camera_last < self._camera_period * 0.9:
            return
        self._camera_last = now
        try:
            data = bytes(msg.data)
            img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                return
            max_w = self.settings.camera_stream_max_width
            if img.shape[1] > max_w:
                img = cv2.resize(img, (max_w, round(img.shape[0] * max_w / img.shape[1])), interpolation=cv2.INTER_AREA)
            ok, jpeg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, self.settings.stream_jpeg_quality])
            if ok:
                self.frames.put("camera", jpeg.tobytes(), "recompressed")
        except Exception:
            log.exception("failed to re-encode camera frame")

    def connected(self) -> bool:
        return bool(self._client and self._client.service_is_ready())

    async def _trigger(self) -> tuple[bool, str]:
        from std_srvs.srv import Trigger

        if self._client is None or not self._client.wait_for_service(timeout_sec=2.0):
            raise CalibUnavailable(f"service {self.settings.calib_service} not available")
        done = threading.Event()
        future = self._client.call_async(Trigger.Request())
        future.add_done_callback(lambda _: done.set())
        finished = await asyncio.to_thread(done.wait, self.settings.calib_timeout_s)
        if not finished:
            raise TimeoutError(f"calibration did not finish in {self.settings.calib_timeout_s:.0f} s")
        resp = future.result()
        return bool(resp.success), resp.message


def make_calib_backend(settings: Settings) -> CalibBackend:
    mode = settings.calib_mode
    if mode == "auto":
        try:
            import rclpy  # noqa: F401

            mode = "ros"
        except ImportError:
            mode = "mock"
    return RosCalibBackend(settings) if mode == "ros" else MockCalibBackend(settings)
