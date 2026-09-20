"""Closed-loop tests for the camera-guided approach, with a fake camera + robot."""
import math

import pytest

from diff_nav.visual_servo import ServoParams, VisualServo, gap_and_bearing

DT = 0.1


def simulate(start, obj, params, min_range=0.08, max_steps=600, fov_deg=60.0):
    """Drive a unicycle under the servo. Camera sees the object inside fov and beyond min_range."""
    x, y, yaw = start
    servo = VisualServo(params)
    travelled = 0.0
    for _ in range(max_steps):
        dx, dy = obj[0] - x, obj[1] - y
        distance = math.hypot(dx, dy)
        bearing = math.atan2(math.sin(math.atan2(dy, dx) - yaw), math.cos(math.atan2(dy, dx) - yaw))
        visible = distance >= min_range and abs(bearing) <= math.radians(fov_deg) / 2
        detection = (distance * math.cos(bearing), distance * math.sin(bearing)) if visible else None
        cmd = servo.update(detection, travelled)
        if cmd.failure or cmd.done:
            return cmd, (x, y, yaw), distance
        x += cmd.v * math.cos(yaw) * DT
        y += cmd.v * math.sin(yaw) * DT
        yaw += cmd.w * DT
        travelled += abs(cmd.v) * DT
    raise AssertionError("servo never finished")


def test_gap_and_bearing():
    gap, bearing = gap_and_bearing(0.20, 0.0, 0.05)
    assert gap == pytest.approx(0.15) and bearing == pytest.approx(0.0)
    _, bearing = gap_and_bearing(0.0, 0.20, 0.05)
    assert bearing == pytest.approx(math.pi / 2)


@pytest.mark.parametrize("stop", [0.01, 0.05, 0.10])
def test_approaches_to_the_stop_distance(stop):
    p = ServoParams(stop_distance=stop)
    cmd, pose, distance = simulate((0.0, 0.0, 0.0), (0.45, 0.0), p)
    assert cmd.done and not cmd.failure
    gap = distance - p.front_offset
    assert abs(gap - stop) <= 0.02, f"stopped with a {gap:.3f} m gap, wanted {stop}"


def test_turns_towards_an_object_off_to_the_side():
    # Inside the camera's view (18 deg), but not straight ahead: it must turn while approaching.
    p = ServoParams(stop_distance=0.05)
    cmd, pose, distance = simulate((0.0, 0.0, 0.0), (0.30, 0.10), p)
    assert cmd.done
    heading_to_object = math.atan2(0.10 - pose[1], 0.30 - pose[0])
    error = abs(math.atan2(math.sin(heading_to_object - pose[2]), math.cos(heading_to_object - pose[2])))
    assert error <= p.yaw_tolerance + 0.05, f"ended {error:.2f} rad off"


def test_close_target_finishes_blind_when_the_camera_loses_it():
    # Stop 1 cm away while the camera stops seeing anything closer than 8 cm:
    # the last centimetres must be driven blind.
    p = ServoParams(stop_distance=0.01, front_offset=0.05)
    cmd, pose, distance = simulate((0.0, 0.0, 0.0), (0.40, 0.0), p, min_range=0.12)
    assert cmd.done and cmd.blind, cmd
    gap = distance - p.front_offset
    assert abs(gap - 0.01) <= 0.02, f"blind finish left a {gap:.3f} m gap"


def test_gives_up_when_the_object_is_lost_far_away():
    p = ServoParams(stop_distance=0.01)
    # Visible only beyond 30 cm: it disappears while still far from the target.
    servo = VisualServo(p)
    servo.update((0.50, 0.0), 0.0)     # seen once, 45 cm gap
    cmd = servo.update(None, 0.05)     # then gone
    assert not cmd.done and "lost sight" in cmd.failure


def test_gives_up_when_never_seen():
    cmd = VisualServo(ServoParams()).update(None, 0.0)
    assert "never saw" in cmd.failure


def test_respects_speed_limits():
    p = ServoParams(stop_distance=0.01, max_linear=0.04, max_angular=0.5)
    servo = VisualServo(p)
    cmd = servo.update((1.0, 0.0), 0.0)
    assert cmd.v <= 0.04 + 1e-9
    cmd = servo.update((0.2, 0.2), 0.0)
    assert abs(cmd.w) <= 0.5 + 1e-9


def test_backs_off_when_too_close():
    p = ServoParams(stop_distance=0.10, front_offset=0.05)
    cmd = VisualServo(p).update((0.08, 0.0), 0.0)   # gap 3 cm, wants 10 cm
    assert cmd.v < 0


def test_object_outside_the_camera_view_fails_immediately():
    # 31 deg off with a 60 deg camera: never visible, so the tree must rotate/search itself.
    p = ServoParams(stop_distance=0.05)
    cmd, _, _ = simulate((0.0, 0.0, 0.0), (0.30, 0.18), p, fov_deg=60.0)
    assert "never saw" in cmd.failure and not cmd.done
