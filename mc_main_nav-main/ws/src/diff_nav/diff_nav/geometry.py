"""Geometry helpers (no ROS imports, unit-tested)."""
import math


def wrap(angle):
    """Wrap an angle to [-pi, pi]."""
    return math.atan2(math.sin(angle), math.cos(angle))


def yaw_from_quaternion(x, y, z, w):
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def quaternion_from_yaw(yaw):
    """Returns (x, y, z, w)."""
    return 0.0, 0.0, math.sin(yaw / 2.0), math.cos(yaw / 2.0)


def approach_candidates(robot_xy, object_xy, centre_distance, count=16):
    """Poses `centre_distance` from the object, each facing it.

    Ordered by how far around the object they are from the robot's current side,
    so the first free one needs the shortest detour. Yields (x, y, yaw).
    """
    ox, oy = object_xy
    rx, ry = robot_xy
    if math.hypot(rx - ox, ry - oy) < 1e-6:
        base = 0.0
    else:
        base = math.atan2(ry - oy, rx - ox)  # direction object -> robot
    step = 2.0 * math.pi / count
    offsets = [0.0]
    for k in range(1, count // 2 + 1):
        offsets.append(k * step)
        if k * step < math.pi - 1e-9:
            offsets.append(-k * step)
    for off in offsets[:count]:
        a = base + off
        x = ox + centre_distance * math.cos(a)
        y = oy + centre_distance * math.sin(a)
        yield x, y, wrap(a + math.pi)


def pick_approach_pose(robot_xy, object_xy, centre_distance, is_free, count=16):
    """First candidate pose whose position `is_free(x, y)`; None if all are blocked."""
    for pose in approach_candidates(robot_xy, object_xy, centre_distance, count):
        if is_free(pose[0], pose[1]):
            return pose
    return None


class GridLookup:
    """Cell lookup on a nav_msgs/OccupancyGrid-like grid (row-major, origin at cell (0, 0))."""

    def __init__(self, width, height, resolution, origin_x, origin_y, data):
        self.width = width
        self.height = height
        self.resolution = resolution
        self.origin_x = origin_x
        self.origin_y = origin_y
        self.data = data

    def value(self, x, y):
        """Cell value at a map position, or None outside the grid."""
        i = int(math.floor((x - self.origin_x) / self.resolution))
        j = int(math.floor((y - self.origin_y) / self.resolution))
        if not (0 <= i < self.width and 0 <= j < self.height):
            return None
        return self.data[j * self.width + i]

    def is_free(self, x, y, threshold):
        """Inside the grid, known (>= 0) and below `threshold`."""
        v = self.value(x, y)
        return v is not None and 0 <= v < threshold
