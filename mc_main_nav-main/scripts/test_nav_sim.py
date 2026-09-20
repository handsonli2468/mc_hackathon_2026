#!/usr/bin/env python3
"""End-to-end nav tests: bt_engine -> diff_nav -> Nav2 -> simulated robot.

Needs (from the repo root):
  MOCK_DISABLE=navigate_to_object,rotate_in_place,is_at_object,navigate_to_point,track_object \\
    docker compose -f docker/compose.yaml --profile sim up -d engine mocks map nav-sim

  ./scripts/test_nav_sim.py            # all tests
  ./scripts/test_nav_sim.py detour     # tests whose name contains "detour"
"""
import json
import math
import subprocess
import sys
import threading
import time
import urllib.request

ENGINE = "http://localhost:8080"
MAP = "http://localhost:8081"
COMPOSE = ["docker", "compose", "-f", "docker/compose.yaml", "--profile", "sim"]
FRONT = 0.05          # robot_front_offset in nav.yaml
RADIUS = 0.05         # robot_radius in nav2_table.yaml
PRESETS = {"contact": 0.01, "near": 0.05, "far": 0.10}


# ---------------------------------------------------------------- plumbing
def http(method, url, body=None):
    data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def ros(cmd, container="nav-sim"):
    full = f"source /opt/ros/humble/setup.bash; source ws/install/setup.bash; {cmd}"
    return subprocess.run(
        COMPOSE + ["exec", "-T", container, "bash", "-c", full],
        capture_output=True, text=True, timeout=30,
    ).stdout


def param(node, name, value, container="nav-sim"):
    out = ros(f"ros2 param set {node} {name} {value}", container)
    assert "successful" in out, f"param set {node} {name} failed: {out}"


def tree(body):
    return f'<root BTCPP_format="4"><BehaviorTree ID="t">{body}</BehaviorTree></root>'


def start(xml):
    code, out = http("POST", f"{ENGINE}/execute", xml)
    assert code == 202, out
    return out["run_id"]


def wait(run_id, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, st = http("GET", f"{ENGINE}/status/{run_id}")
        if st["state"] != "running":
            return st
        time.sleep(0.3)
    raise AssertionError(f"{run_id} still running after {timeout}s")


def run(xml, timeout=120):
    return wait(start(xml), timeout)


def robot():
    _, s = http("GET", f"{MAP}/api/state")
    return s["robot"]


def robot_now():
    """Fresh pose (waits for the next camera message after it was re-enabled)."""
    time.sleep(0.3)
    r = robot()
    assert r and r["age"] < 0.3, f"no fresh pose: {r}"
    return r


def set_obstacles(items):
    code, out = http("PUT", f"{MAP}/api/obstacles", json.dumps(items))
    assert code == 200, out
    time.sleep(1.5)  # global costmap updates at 2 Hz


def set_cup(x, y):
    param("/mock_robot", "object_positions", f"\"['cup:{x}:{y}']\"", container="mocks")


def teleport(x, y, yaw):
    param("/sim_base", "teleport", f"'{x},{y},{yaw}'")
    time.sleep(1.0)


def gap(r, cup):
    return math.hypot(cup[0] - r["x"], cup[1] - r["y"]) - FRONT


class Recorder:
    """Samples the robot pose while a tree runs."""

    def __init__(self):
        self.poses = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)

    def _loop(self):
        while not self._stop.is_set():
            r = robot()
            if r:
                self.poses.append((r["x"], r["y"]))
            time.sleep(0.1)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join()


def rect_distance(px, py, o):
    dx = max(o["x"] - px, 0.0, px - (o["x"] + o["w"]))
    dy = max(o["y"] - py, 0.0, py - (o["y"] + o["h"]))
    return math.hypot(dx, dy)


FIND_AND_GO = (
    '<Sequence><VisualizeObject object_name="cup"/>'
    '<NavigateToObject name="go" object_name="cup" distance="{d}"/>'
    '<IsAtObject name="check" object_name="cup"/></Sequence>'
)


# ---------------------------------------------------------------- tests
def test_presets():
    cup = (0.40, 0.30)
    set_cup(*cup)
    for preset in ("far", "near", "contact"):
        teleport(0.10, 0.10, 0.0)
        st = run(tree(FIND_AND_GO.format(d=preset)))
        assert st["state"] == "success", st
        r = robot()
        g = gap(r, cup)
        facing = math.atan2(cup[1] - r["y"], cup[0] - r["x"]) - r["yaw"]
        facing = abs(math.atan2(math.sin(facing), math.cos(facing)))
        assert abs(g - PRESETS[preset]) <= 0.008, f"{preset}: gap {g:.3f}"
        assert facing <= 0.15, f"{preset}: facing error {facing:.2f} rad"
        print(f"    {preset}: gap {g * 100:.1f} cm, facing error {facing:.2f} rad")


def test_detour_around_wall():
    cup = (0.70, 0.15)
    wall = {"type": "rect", "x": 0.35, "y": 0.0, "w": 0.04, "h": 0.45}
    set_cup(*cup)
    set_obstacles([wall])
    teleport(0.12, 0.15, 0.0)
    with Recorder() as rec:
        st = run(tree(FIND_AND_GO.format(d="near")))
    assert st["state"] == "success", st
    clearance = min(rect_distance(x, y, wall) for x, y in rec.poses)
    top = max(y for _, y in rec.poses)
    assert clearance >= RADIUS - 0.005, f"came within {clearance:.3f} m of the wall"
    assert top > wall["y"] + wall["h"], "never went around the wall"
    print(f"    min clearance {clearance * 100:.1f} cm, went up to y={top:.2f}")


def test_live_obstacle_blocks_approach_side():
    # Obstacle right where the direct approach would stop -> diff_nav picks another side.
    cup = (0.60, 0.40)
    set_cup(*cup)
    set_obstacles([{"type": "rect", "x": 0.45, "y": 0.30, "w": 0.06, "h": 0.20}])
    teleport(0.15, 0.40, 0.0)
    st = run(tree(FIND_AND_GO.format(d="near")))
    assert st["state"] == "success", st
    r = robot()
    assert r["x"] > 0.51 or abs(r["y"] - cup[1]) > 0.10, f"stopped on the blocked side: {r}"
    print(f"    stopped at ({r['x']:.2f}, {r['y']:.2f})")


def test_surrounded_object_fails_with_reason():
    cup = (0.60, 0.40)
    set_cup(*cup)
    set_obstacles([{"type": "circle", "x": cup[0], "y": cup[1], "r": 0.14}])
    st = run(tree(FIND_AND_GO.format(d="near")))
    assert st["state"] == "failure", st
    assert any("no free spot" in n["message"] for n in st["notes"]), st["notes"]


def test_unknown_object_reason():
    st = run(tree('<NavigateToObject object_name="bottle"/>'))
    assert st["state"] == "failure"
    assert any("bottle" in n["message"] for n in st["notes"]), st["notes"]


def test_rotate():
    teleport(0.40, 0.40, 0.0)
    st = run(tree('<RotateInPlace angle_deg="90"/>'))
    assert st["state"] == "success", st
    yaw = robot()["yaw"]
    assert abs(yaw - math.pi / 2) <= 0.15, f"yaw {yaw:.2f}"
    print(f"    yaw after +90 deg: {math.degrees(yaw):.0f} deg")


def test_cancel_stops_robot():
    set_cup(1.05, 0.65)
    teleport(0.12, 0.12, 0.6)
    param("/sim_base", "cmd_timeout", "60.0")  # only an explicit stop can halt it now
    try:
        run_id = start(tree(FIND_AND_GO.format(d="near")))
        time.sleep(4.0)
        a = robot()
        time.sleep(0.5)
        b = robot()
        moving = math.hypot(b["x"] - a["x"], b["y"] - a["y"])
        assert moving > 0.01, f"robot wasn't moving before cancel ({moving:.3f} m / 0.5 s)"
        http("POST", f"{ENGINE}/cancel")
        assert wait(run_id)["state"] == "canceled"
        time.sleep(1.0)
        a = robot()
        time.sleep(1.0)
        b = robot()
        drift = math.hypot(b["x"] - a["x"], b["y"] - a["y"])
        assert drift < 0.002, f"still moving after cancel ({drift:.3f} m / s)"
        print(f"    before cancel {moving * 200:.1f} cm/s, after {drift * 100:.2f} cm/s")
    finally:
        param("/sim_base", "cmd_timeout", "0.5")


def test_camera_loss_fails_and_stops():
    set_cup(1.05, 0.65)
    teleport(0.12, 0.12, 0.6)
    param("/sim_base", "cmd_timeout", "60.0")  # only an explicit stop can halt it now
    run_id = start(tree(FIND_AND_GO.format(d="near")))
    try:
        time.sleep(4.0)
        param("/sim_base", "camera_enabled", "false")
        t0 = time.time()
        st = wait(run_id, timeout=30)
        took = time.time() - t0
        assert st["state"] == "failure", st
        assert any("lost the robot pose" in n["message"] for n in st["notes"]), st["notes"]
        assert took < 10, f"took {took:.0f}s to notice"
        # Pose topic is off, so read the true position from the simulator itself.
        param("/sim_base", "camera_enabled", "true")
        a = robot_now()
        time.sleep(1.0)
        b = robot_now()
        drift = math.hypot(b["x"] - a["x"], b["y"] - a["y"])
        assert drift < 0.002, f"still moving after camera loss ({drift:.3f} m / s)"
        print(f"    noticed after {took:.1f}s, then {drift * 100:.2f} cm/s")
    finally:
        param("/sim_base", "camera_enabled", "true")
        param("/sim_base", "cmd_timeout", "0.5")


def test_point_navigation():
    teleport(0.15, 0.15, 0.0)
    target = (0.70, 0.55)
    st = run(tree(f'<NavigateToPoint name="go" x="{target[0]}" y="{target[1]}"/>'))
    assert st["state"] == "success", st
    r = robot()
    off = math.hypot(target[0] - r["x"], target[1] - r["y"])
    assert off <= 0.02, f"ended {off:.3f} m from the point"
    print(f"    reached ({r['x']:.2f}, {r['y']:.2f}), {off * 100:.1f} cm off")


def test_point_navigation_with_heading():
    teleport(0.15, 0.15, 0.0)
    st = run(tree('<NavigateToPoint name="go" x="0.60" y="0.30" yaw_deg="90"/>'))
    assert st["state"] == "success", st
    yaw = robot()["yaw"]
    assert abs(yaw - math.pi / 2) <= 0.2, f"final yaw {math.degrees(yaw):.0f} deg"
    print(f"    final heading {math.degrees(yaw):.0f} deg (asked for 90)")


def test_point_inside_obstacle_fails():
    set_obstacles([{"type": "rect", "x": 0.50, "y": 0.40, "w": 0.15, "h": 0.15}])
    st = run(tree('<NavigateToPoint name="go" x="0.57" y="0.47"/>'))
    assert st["state"] == "failure", st
    assert any("blocked" in n["message"] for n in st["notes"]), st["notes"]
    print(f"    {st['notes'][0]['message']}")


def test_point_off_the_table_fails():
    st = run(tree('<NavigateToPoint name="go" x="3.0" y="3.0"/>'))
    assert st["state"] == "failure", st
    assert any("blocked" in n["message"] for n in st["notes"]), st["notes"]


def test_camera_approach():
    """Nav2 gets near, then the onboard camera closes the last centimetres."""
    cup = (0.45, 0.25)
    set_cup(*cup)
    teleport(0.10, 0.10, 0.0)
    st = run(tree(
        '<Sequence>'
        '<VisualizeObject name="look" object_name="cup"/>'
        '<NavigateToObject name="nav" object_name="cup" distance="far"/>'
        '<TrackObject name="camera" object_name="cup" distance="contact"/>'
        '</Sequence>'))
    assert st["state"] == "success", st
    r = robot()
    g = gap(r, cup)
    facing = math.atan2(cup[1] - r["y"], cup[0] - r["x"]) - r["yaw"]
    facing = abs(math.atan2(math.sin(facing), math.cos(facing)))
    assert abs(g - PRESETS["contact"]) <= 0.02, f"camera approach left a {g:.3f} m gap"
    assert facing <= 0.15, f"facing error {facing:.2f} rad"
    print(f"    gap {g * 100:.1f} cm, facing error {facing:.3f} rad")


def test_camera_approach_needs_the_object_in_view():
    """The camera cannot see behind the robot: TrackObject must fail, not search."""
    set_cup(0.45, 0.25)
    teleport(0.10, 0.10, math.pi)   # facing away from the cup
    st = run(tree('<TrackObject name="camera" object_name="cup" distance="near"/>'))
    assert st["state"] == "failure", st
    assert any("never saw" in n["message"] for n in st["notes"]), st["notes"]
    print(f"    {st['notes'][0]['message']}")


def test_camera_approach_finishes_blind_when_too_close():
    """The fake camera loses the object under 8 cm; the servo drives the rest blind."""
    cup = (0.45, 0.25)
    set_cup(*cup)
    teleport(0.10, 0.10, 0.0)
    st = run(tree(
        '<Sequence>'
        '<VisualizeObject name="look" object_name="cup"/>'
        '<NavigateToObject name="nav" object_name="cup" distance="far"/>'
        '<TrackObject name="camera" object_name="cup" distance="contact"/>'
        '</Sequence>'))
    assert st["state"] == "success", st
    trace = " ".join(e["node"] for e in st["trace"])
    assert "camera" in trace
    g = gap(robot(), cup)
    assert g <= 0.035, f"ended {g:.3f} m away, blind phase did not close the gap"
    print(f"    final gap {g * 100:.1f} cm")


def test_bad_preset_rejected():
    code, out = http("POST", f"{ENGINE}/validate", tree('<NavigateToObject object_name="cup" distance="close"/>'))
    assert code == 422 and "contact" in out["error"], out


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main():
    wanted = [t for t in TESTS if not sys.argv[1:] or any(a in t.__name__ for a in sys.argv[1:])]
    failed = 0
    try:
        for t in wanted:
            set_obstacles([])
            t0 = time.time()
            try:
                t()
                print(f"PASS {t.__name__} ({time.time() - t0:.0f}s)")
            except AssertionError as e:
                failed += 1
                print(f"FAIL {t.__name__}: {e}")
    finally:
        set_obstacles([])
        set_cup(0.40, 0.20)
    print(f"== {failed} failure(s) of {len(wanted)}")
    sys.exit(failed)


if __name__ == "__main__":
    main()
