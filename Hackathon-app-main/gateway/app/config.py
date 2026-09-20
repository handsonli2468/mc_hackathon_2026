"""Gateway settings, read from environment variables (or a .env file)."""
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(ROOT / ".env", ".env"), extra="ignore")

    # BT engine (mc_main_nav). The token never leaves the gateway.
    bt_engine_url: str = "http://localhost:8080"
    bt_engine_token: str = ""
    bt_timeout_s: float = 5.0

    # Cloud task server (Manta, APP_API.md). "http" = Manta at CLOUD_URL, "mock" = offline stand-in.
    cloud_mode: Literal["mock", "http"] = "mock"
    cloud_url: str = ""
    cloud_token: str = ""
    cloud_timeout_s: float = 20.0
    cloud_chat_timeout_s: float = 240.0  # planning takes ~40 s, sometimes much more
    cloud_pipeline_mode: Literal["hybrid", "direct", "compare"] = "hybrid"
    # Mock only: a planned mission is sent to bt_engine like Manta's auto-execution. Moves the real robot.
    mock_execute: bool = False
    mock_tree_file: str = ""

    # Calibration (field_calib_node in the localization server).
    # auto = ros if rclpy can be imported, otherwise mock.
    calib_mode: Literal["auto", "ros", "mock"] = "auto"
    field_file: Path = ROOT / "data" / "field_app.yaml"
    field_template: Path = ROOT / "gateway" / "tests" / "fixtures" / "field.yaml"
    calib_output_dir: Path = ROOT / "data" / "calib_out"
    calib_service: str = "/field_calib_node/calibrate"
    calib_live_topic: str = "/field_calib_node/live/overlay"
    calib_calib_topic: str = "/field_calib_node/calib/overlay"
    # Plain camera view for framing the table (field_calib overlays are only ~1 Hz).
    calib_camera_topic: str = "/camera/camera/color/image_raw/compressed"
    camera_stream_fps: float = 15.0
    camera_stream_max_width: int = 960
    calib_timeout_s: float = 120.0
    stream_jpeg_quality: int = 70

    data_dir: Path = ROOT / "data"
    static_dir: Path = ROOT / "web" / "dist"


@lru_cache
def get_settings() -> Settings:
    return Settings()
