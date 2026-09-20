import math

import pytest

from diff_nav.geometry import (
    GridLookup,
    approach_candidates,
    pick_approach_pose,
    quaternion_from_yaw,
    wrap,
    yaw_from_quaternion,
)


def test_first_candidate_is_on_robot_side_and_faces_object():
    x, y, yaw = next(approach_candidates((0.0, 0.0), (0.40, 0.0), 0.10))
    assert (x, y) == pytest.approx((0.30, 0.0))
    assert yaw == pytest.approx(0.0)


def test_candidates_are_at_distance_facing_object_and_unique():
    obj = (0.40, 0.20)
    poses = list(approach_candidates((0.1, 0.5), obj, 0.08, count=16))
    assert len(poses) == 16
    angles = set()
    for x, y, yaw in poses:
        assert math.hypot(x - obj[0], y - obj[1]) == pytest.approx(0.08)
        assert wrap(math.atan2(obj[1] - y, obj[0] - x) - yaw) == pytest.approx(0.0, abs=1e-9)
        angles.add(round(math.atan2(y - obj[1], x - obj[0]), 6))
    assert len(angles) == 16


def test_candidates_widen_away_from_robot_side():
    obj = (0.0, 0.0)
    poses = list(approach_candidates((-1.0, 0.0), obj, 0.1, count=8))
    detours = [round(abs(wrap(math.atan2(y, x) - math.pi)), 9) for x, y, _ in poses]
    assert detours == sorted(detours)
    assert detours[-1] == pytest.approx(math.pi)


def test_pick_skips_blocked_side():
    # Everything with x < 0.35 is blocked -> must approach from the far side.
    pose = pick_approach_pose((0.0, 0.0), (0.40, 0.0), 0.10, lambda x, y: x >= 0.35)
    assert pose is not None
    assert pose[0] >= 0.35


def test_pick_returns_none_when_surrounded():
    assert pick_approach_pose((0, 0), (1, 1), 0.1, lambda x, y: False) is None


def test_grid_lookup():
    # 4 x 3 grid, 5 mm cells, origin (-0.01, 0); cell (2, 1) occupied, cell (0, 0) unknown.
    data = [0] * 12
    data[1 * 4 + 2] = 100
    data[0] = -1
    grid = GridLookup(4, 3, 0.005, -0.01, 0.0, data)
    assert grid.value(0.001, 0.006) == 100
    assert grid.is_free(0.001, 0.006, 99) is False
    assert grid.is_free(-0.009, 0.001, 99) is False  # unknown
    assert grid.is_free(-0.004, 0.001, 99) is True
    assert grid.value(0.02, 0.0) is None
    assert grid.is_free(0.0, -0.001, 99) is False


def test_quaternion_round_trip():
    for yaw in (-3.0, -1.0, 0.0, 0.5, 3.1):
        assert yaw_from_quaternion(*quaternion_from_yaw(yaw)) == pytest.approx(yaw)
