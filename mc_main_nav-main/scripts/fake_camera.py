#!/usr/bin/env python3
"""Stand-in for the camera team's HTTP service, so the search tree can be tested.

  ./scripts/fake_camera.py [--port 8099] [--find-after 8] [--x 1.2 --y 0.9]

Mirrors the real VLM server's API:
  POST /api/query   {"text": "the white paper cup"}  -> start looking
  GET  /api/status                                   -> {"recent": [{"status": "FOUND",
                                                        "client_stamp": ...}], ...}

`--find-after N` makes it report the object N seconds after the search started, publish
the pose on /object_goal_pose, and -- like the real system would -- make the object known
to the rest of ROS by calling the /visualize_object action, so /get_object_pose starts
answering and the onboard camera pipeline can see it. Without a sourced ROS environment it
still answers HTTP and simply skips both.
"""
import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

state = {"object": None, "started_at": None, "find_after": 8.0, "x": 1.2, "y": 0.9,
         "pub": None, "drift": 0.0, "found_at": None, "findable": ""}


def found():
    """Has the 'camera' seen the current query yet?"""
    if state["started_at"] is None:
        return False
    # --findable models objects that are simply not there: anything whose text does not
    # match is never found, so a tree can be tested against a hopeless search.
    if state["findable"] and state["findable"].lower() not in (state["object"] or "").lower():
        return False
    return (time.time() - state["started_at"]) >= state["find_after"]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        if urlparse(self.path).path != "/api/query":
            return self._send(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n) or b"{}")
        state["object"] = req.get("text")
        state["started_at"] = time.time()
        state["found_at"] = None
        print(f"camera: looking for {state['object']!r}, will find it in {state['find_after']}s")
        self._send(200, {"query_version": 2, "text": state["object"]})

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/query":
            return self._send(200, {"query_version": 2, "text": state["object"]})
        if u.path != "/api/status":
            return self._send(404, {"error": "not found"})
        # Same shape as the real server: a list of recent attempts, newest last.
        recent = []
        if state["started_at"] is not None:
            now = time.time()
            for i in range(5):
                stamp = now - (4 - i) * 0.4
                seen = found() and state["started_at"] + state["find_after"] <= stamp
                recent.append({
                    "client_stamp": stamp,
                    "request_id": i,
                    "server_ms": 1884.1,
                    "status": "FOUND" if seen else "NOT_FOUND",
                })
        self._send(200, {
            "model_ready": True,
            "model_error": None,
            "query": {"query_version": 2, "text": state["object"]},
            "recent": recent,
        })


def start_pose_publisher(x, y, topic):
    try:
        import rclpy
        from geometry_msgs.msg import PoseStamped
        from rclpy.action import ActionClient
        from rclpy.node import Node
        from robot_interfaces.action import VisualizeObject
    except Exception as exc:                      # no ROS on this shell: HTTP only
        print(f"camera: no ROS ({exc}); not publishing a pose")
        return None
    rclpy.init()
    node = Node("fake_camera")
    pub = node.create_publisher(PoseStamped, topic, 1)
    visualize = ActionClient(node, VisualizeObject, "visualize_object")
    threading.Thread(target=lambda: rclpy.spin(node), daemon=True).start()
    announced = set()

    def publish():
        if state["found_at"] is None:
            state["found_at"] = time.time()
        # The object drifts, so the tree sees the pose move while it is approaching.
        moved = state["drift"] * (time.time() - state["found_at"])
        msg = PoseStamped()
        msg.header.frame_id = "map"
        msg.header.stamp = node.get_clock().now().to_msg()
        msg.pose.position.x = float(x) + moved
        msg.pose.position.y = float(y)
        msg.pose.orientation.w = 1.0
        pub.publish(msg)
        # The real camera system also makes the object known to ROS, so that
        # /get_object_pose answers and the onboard camera pipeline can see it.
        name = state["object"]
        if name and name not in announced and visualize.server_is_ready():
            announced.add(name)
            visualize.send_goal_async(VisualizeObject.Goal(object_name=name))
            print(f"camera: announced {name!r} to /visualize_object")

    # The real camera publishes continuously, not only when someone asks over HTTP.
    # Without this the pose freezes the moment the tree stops polling.
    def keep_publishing():
        while True:
            if found():
                publish()
            time.sleep(0.2)
    threading.Thread(target=keep_publishing, daemon=True).start()
    return publish


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--find-after", type=float, default=8.0)
    ap.add_argument("--x", type=float, default=1.2)
    ap.add_argument("--y", type=float, default=0.9)
    ap.add_argument("--topic", default="/object_goal_pose")
    ap.add_argument(
        "--findable", default="",
        help="only find objects whose text contains this; anything else is never found")
    ap.add_argument(
        "--drift", type=float, default=0.0,
        help="metres per second the object drifts in +X once found, to test pose updates")
    args = ap.parse_args()
    state["find_after"] = args.find_after
    state["drift"] = args.drift
    state["findable"] = args.findable
    state["pub"] = start_pose_publisher(args.x, args.y, args.topic)
    print(f"fake camera on :{args.port}, object appears at ({args.x}, {args.y}), drift {args.drift} m/s")
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
