"""Table map + obstacle editor (runs on the mini PC).

Publishes /map (nav_msgs/OccupancyGrid, latched) for Nav2's static layer:
  base   = the global camera's map on `base_map_topic` if one has arrived,
           otherwise an empty table of `width` x `height`
  + edge = a band of `edge_margin` around the border marked occupied (don't drive off)
  + obstacles drawn in the web UI (rectangles / circles), saved to `obstacles_file`
Re-rendered and re-published whenever any of these change. Grid resolution is
always `resolution` (the camera map is resampled onto it).

Web UI + JSON API on http://<host>:<http_port>/ (see web/index.html).
"""
import base64
import json
import math
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np
import rclpy
from ament_index_python.packages import get_package_share_directory
from geometry_msgs.msg import PoseStamped
from nav_msgs.msg import OccupancyGrid, Path
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy

LATCHED = QoSProfile(
    depth=1, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL
)
MAX_OBSTACLES = 200


def _yaw(q):
    return math.atan2(2.0 * (q.w * q.z + q.x * q.y), 1.0 - 2.0 * (q.y * q.y + q.z * q.z))


def validate_obstacles(items):
    """Returns a clean list or raises ValueError."""
    if not isinstance(items, list) or len(items) > MAX_OBSTACLES:
        raise ValueError(f"expected a list of at most {MAX_OBSTACLES} obstacles")
    out = []
    for n, o in enumerate(items):
        if not isinstance(o, dict):
            raise ValueError(f"obstacle {n}: not an object")
        kind = o.get("type")
        try:
            if kind == "rect":
                clean = {k: float(o[k]) for k in ("x", "y", "w", "h")}
                if clean["w"] <= 0 or clean["h"] <= 0:
                    raise ValueError
            elif kind == "circle":
                clean = {k: float(o[k]) for k in ("x", "y", "r")}
                if clean["r"] <= 0:
                    raise ValueError
            else:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ValueError(
                f"obstacle {n}: need type 'rect' (x, y, w, h > 0) or 'circle' (x, y, r > 0)"
            ) from None
        if not all(math.isfinite(v) for v in clean.values()):
            raise ValueError(f"obstacle {n}: numbers must be finite")
        clean["type"] = kind
        clean["id"] = str(o.get("id", n))[:40]
        out.append(clean)
    return out


def render(width_cells, height_cells, resolution, origin, base, edge_margin, obstacles):
    """Returns an int8 grid [row=y, col=x]: 0 free, 100 occupied, -1 unknown."""
    grid = np.zeros((height_cells, width_cells), dtype=np.int8) if base is None else base.copy()
    m = int(round(edge_margin / resolution))
    if m > 0:
        grid[:m, :] = 100
        grid[-m:, :] = 100
        grid[:, :m] = 100
        grid[:, -m:] = 100
    # Cell centres in map coordinates.
    xs = origin[0] + (np.arange(width_cells) + 0.5) * resolution
    ys = origin[1] + (np.arange(height_cells) + 0.5) * resolution
    for o in obstacles:
        if o["type"] == "rect":
            cols = (xs >= o["x"]) & (xs <= o["x"] + o["w"])
            rows = (ys >= o["y"]) & (ys <= o["y"] + o["h"])
            grid[np.ix_(rows, cols)] = 100
        else:
            inside = (xs[None, :] - o["x"]) ** 2 + (ys[:, None] - o["y"]) ** 2 <= o["r"] ** 2
            grid[inside] = 100
    return grid


def resample(msg, width_cells, height_cells, resolution, origin):
    """Nearest-neighbour resample of an OccupancyGrid onto our grid (occupied >= 50 -> 100)."""
    info = msg.info
    src = np.asarray(msg.data, dtype=np.int16).reshape(info.height, info.width)
    xs = origin[0] + (np.arange(width_cells) + 0.5) * resolution
    ys = origin[1] + (np.arange(height_cells) + 0.5) * resolution
    ci = np.floor((xs - info.origin.position.x) / info.resolution).astype(int)
    rj = np.floor((ys - info.origin.position.y) / info.resolution).astype(int)
    out = np.full((height_cells, width_cells), -1, dtype=np.int8)
    col_ok = (ci >= 0) & (ci < info.width)
    row_ok = (rj >= 0) & (rj < info.height)
    sub = src[np.ix_(rj[row_ok], ci[col_ok])]
    vals = np.where(sub < 0, -1, np.where(sub >= 50, 100, 0)).astype(np.int8)
    out[np.ix_(row_ok, col_ok)] = vals
    return out


class TableMap(Node):
    def __init__(self):
        super().__init__("table_map")
        dp = self.declare_parameter
        self.frame = dp("frame_id", "map").value
        self.resolution = dp("resolution", 0.005).value
        self.default_size = (dp("width", 1.8).value, dp("height", 1.2).value)
        self.default_origin = (dp("origin_x", 0.0).value, dp("origin_y", 0.0).value)
        self.edge_margin = dp("edge_margin", 0.01).value
        self.obstacles_file = os.path.expanduser(
            dp("obstacles_file", "~/mc_main_nav/data/obstacles.json").value
        )
        host = dp("http_host", "0.0.0.0").value
        port = dp("http_port", 8081).value
        # Set when the UI is reachable from the internet; /api/* then needs the token
        # (header X-Map-Token or ?token=). The page itself stays open so it can ask for it.
        self.token = dp("auth_token", os.environ.get("TABLE_MAP_TOKEN", "")).value

        self._lock = threading.Lock()
        self._base_msg = None
        self._obstacles = self._load_obstacles()
        self._robot = None   # (x, y, yaw, monotonic time)
        self._plan = []
        self._grid = None    # last rendered grid
        self._geometry = None

        self.map_pub = self.create_publisher(OccupancyGrid, dp("map_topic", "/map").value, LATCHED)
        self.create_subscription(
            OccupancyGrid, dp("base_map_topic", "/table_map").value, self._on_base, LATCHED
        )
        self.create_subscription(
            PoseStamped, dp("robot_pose_topic", "/robot_pose").value, self._on_pose, 10
        )
        self.create_subscription(Path, dp("plan_topic", "/plan").value, self._on_plan, 10)
        self._publish()

        web_dir = os.path.join(get_package_share_directory("table_map"), "web")
        handler = make_handler(self, web_dir)
        self.httpd = ThreadingHTTPServer((host, port), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.get_logger().info(f"table_map UI on http://{host}:{port}/ ; obstacles in {self.obstacles_file}")

    # ------------------------------------------------------------------ state
    def _load_obstacles(self):
        try:
            with open(self.obstacles_file) as f:
                return validate_obstacles(json.load(f))
        except FileNotFoundError:
            return []
        except (ValueError, OSError) as e:
            self.get_logger().error(f"ignoring bad obstacles file: {e}")
            return []

    def set_obstacles(self, items):
        clean = validate_obstacles(items)
        os.makedirs(os.path.dirname(self.obstacles_file) or ".", exist_ok=True)
        tmp = self.obstacles_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(clean, f, indent=1)
        os.replace(tmp, self.obstacles_file)
        with self._lock:
            self._obstacles = clean
        self._publish()
        return clean

    def _on_base(self, msg):
        with self._lock:
            self._base_msg = msg
        self.get_logger().info(
            f"base map from camera: {msg.info.width}x{msg.info.height} @ {msg.info.resolution} m"
        )
        self._publish()

    def _on_pose(self, msg):
        p = msg.pose
        with self._lock:
            self._robot = (p.position.x, p.position.y, _yaw(p.orientation), time.monotonic())

    def _on_plan(self, msg):
        pts = [[round(s.pose.position.x, 4), round(s.pose.position.y, 4)] for s in msg.poses]
        with self._lock:
            self._plan = pts[:: max(1, len(pts) // 300)]

    def _publish(self):
        with self._lock:
            base_msg = self._base_msg
            obstacles = list(self._obstacles)
        res = self.resolution
        if base_msg is not None:
            info = base_msg.info
            origin = (info.origin.position.x, info.origin.position.y)
            size = (info.width * info.resolution, info.height * info.resolution)
        else:
            origin, size = self.default_origin, self.default_size
        w_cells = max(1, int(round(size[0] / res)))
        h_cells = max(1, int(round(size[1] / res)))
        base = resample(base_msg, w_cells, h_cells, res, origin) if base_msg is not None else None
        grid = render(w_cells, h_cells, res, origin, base, self.edge_margin, obstacles)

        msg = OccupancyGrid()
        msg.header.frame_id = self.frame
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.info.map_load_time = msg.header.stamp
        msg.info.resolution = res
        msg.info.width = w_cells
        msg.info.height = h_cells
        msg.info.origin.position.x = origin[0]
        msg.info.origin.position.y = origin[1]
        msg.info.origin.orientation.w = 1.0
        msg.data = grid.flatten().tolist()
        self.map_pub.publish(msg)
        with self._lock:
            self._grid = grid
            self._geometry = {
                "width": w_cells, "height": h_cells, "resolution": res,
                "origin": list(origin), "source": "camera" if base_msg is not None else "config",
                "edge_margin": self.edge_margin,
            }

    def snapshot(self, with_grid):
        with self._lock:
            out = {
                "map": dict(self._geometry),
                "obstacles": list(self._obstacles),
                "plan": list(self._plan),
                "robot": None,
            }
            if self._robot is not None:
                x, y, yaw, t = self._robot
                out["robot"] = {"x": x, "y": y, "yaw": yaw, "age": time.monotonic() - t}
            if with_grid:
                # 1 byte per cell, row 0 = bottom (y = origin); 0 free, 1 occupied, 2 unknown.
                g = self._grid
                cells = np.where(g < 0, 2, np.where(g >= 50, 1, 0)).astype(np.uint8)
                out["grid"] = base64.b64encode(cells.tobytes()).decode()
        return out


def make_handler(node, web_dir):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Map-Token")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self):
            if not node.token:
                return True
            query = parse_qs(urlparse(self.path).query)
            return (self.headers.get("X-Map-Token") == node.token
                    or query.get("token", [None])[0] == node.token)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path.startswith("/api/") and not self._authorized():
                self._send(401, {"ok": False, "error": "missing or wrong token"})
                return
            if path in ("/", "/index.html"):
                with open(os.path.join(web_dir, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif path == "/api/state":
                self._send(200, node.snapshot(with_grid="grid=1" in self.path))
            else:
                self._send(404, {"ok": False, "error": "not found"})

        def do_PUT(self):
            if not self._authorized():
                self._send(401, {"ok": False, "error": "missing or wrong token"})
                return
            if self.path.split("?")[0] != "/api/obstacles":
                self._send(404, {"ok": False, "error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length > 1_000_000:
                    raise ValueError("body too large")
                items = json.loads(self.rfile.read(length) or b"null")
                clean = node.set_obstacles(items)
            except (ValueError, json.JSONDecodeError) as e:
                self._send(400, {"ok": False, "error": str(e)})
                return
            self._send(200, {"ok": True, "obstacles": clean})

    return Handler


def main():
    rclpy.init()
    node = TableMap()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.httpd.shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
