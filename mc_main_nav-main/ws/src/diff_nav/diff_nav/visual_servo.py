"""Control law for the camera-guided approach. No ROS imports, so it is unit-tested.

The onboard camera pipeline says where the object is relative to the robot (metres,
x forward / y left). We turn to face it, creep forward, and stop `stop_distance`
from the robot's front edge.

The camera cannot see the object in the last few centimetres (fixed forward camera,
no tilt). When the detection disappears that close, we finish the move "blind":
the remaining gap is short and known, and the global camera still tells us how far
the robot has travelled, so we drive exactly that far and stop.
"""
import math
from dataclasses import dataclass


def wrap(angle):
    return math.atan2(math.sin(angle), math.cos(angle))


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


@dataclass
class ServoParams:
    front_offset: float = 0.05        # robot centre -> front edge (m)
    stop_distance: float = 0.01       # front edge -> object (m)
    distance_tolerance: float = 0.01  # m
    yaw_tolerance: float = 0.10       # rad, "facing it" when this close
    align_first: float = 0.35         # rad; turn in place when the bearing is worse
    k_linear: float = 0.8
    k_angular: float = 1.8
    max_linear: float = 0.05          # m/s, slower than Nav2: we are close to things
    max_angular: float = 0.8          # rad/s
    blind_gap: float = 0.12           # finish blind only if the gap was under this (m)


@dataclass
class ServoCommand:
    v: float = 0.0
    w: float = 0.0
    done: bool = False
    failure: str = ""      # non-empty = give up, with this reason
    blind: bool = False    # driving without seeing the object
    gap: float = math.nan  # front edge -> object, metres (last known when blind)


def gap_and_bearing(x, y, front_offset):
    """Object seen at (x, y) in robot coordinates -> (gap from front edge, bearing)."""
    distance = math.hypot(x, y)
    return distance - front_offset, math.atan2(y, x)


class VisualServo:
    """Feed it detections (or the fact that there is none); it returns wheel commands.

    `travelled` is how far the robot has moved since the servo started, measured by the
    global camera. It is only used for the blind final centimetres.
    """

    def __init__(self, params=None):
        self.p = params or ServoParams()
        self.seen = False
        self._last_gap = None       # last gap measured with the object in sight
        self._blind_from = None     # (gap, travelled) at the moment it was lost

    def update(self, detection, travelled):
        """detection: (x, y) in robot coordinates, or None when not visible."""
        p = self.p
        if detection is not None:
            self.seen = True
            self._blind_from = None
            gap, bearing = gap_and_bearing(detection[0], detection[1], p.front_offset)
            self._last_gap = gap
            error = gap - p.stop_distance

            if abs(error) <= p.distance_tolerance and abs(bearing) <= p.yaw_tolerance:
                return ServoCommand(done=True, gap=gap)
            if abs(bearing) > p.align_first:
                return ServoCommand(
                    v=0.0, w=clamp(p.k_angular * bearing, -p.max_angular, p.max_angular), gap=gap)
            return ServoCommand(
                v=clamp(p.k_linear * error, -p.max_linear, p.max_linear),
                w=clamp(p.k_angular * bearing, -p.max_angular, p.max_angular),
                gap=gap)

        # Nothing visible.
        if not self.seen:
            return ServoCommand(failure="never saw the object; is it in the camera's view?")
        if self._blind_from is None:
            if self._last_gap is None or self._last_gap > p.blind_gap:
                return ServoCommand(
                    failure=f"lost sight of the object {self._last_gap:.2f} m short of the target")
            self._blind_from = (self._last_gap, travelled)
        gap0, travelled0 = self._blind_from
        moved = travelled - travelled0
        remaining = (gap0 - p.stop_distance) - moved
        if remaining <= p.distance_tolerance:
            return ServoCommand(done=True, blind=True, gap=p.stop_distance)
        return ServoCommand(
            v=clamp(p.k_linear * remaining, 0.0, p.max_linear), w=0.0,
            blind=True, gap=gap0 - moved)
