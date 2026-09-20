"""Mock robot: implements every server in robot_interfaces with a tiny fake world.

World rules (so trees behave plausibly):
  visualize_object  -> object becomes visible + located (if it is in `objects`)
  rotate_in_place   -> nothing is visible anymore (view changed)
  navigate_to_object-> requires located; robot is then at the object
  track_object      -> requires located; robot is then at the object
  grasp_object      -> requires at object + gripper open; object is held
  set_gripper       -> position 0 (open) .. 100 (closed); at or above
                       `gripper_hold_position` it picks up the object the robot is
                       standing at, below it the jaws open and drop whatever is held.
                       Also mirrored from the /gripper topic, so the fake world stays
                       right when the real gripper server owns the action.

Tune at runtime, e.g.:
  ros2 param set /mock_robot fail.navigate_to_object 0.5   # 50% failure
  ros2 param set /mock_robot delay.visualize_object 3.0
  ros2 param set /mock_robot objects "['cup','bottle']"

By default the robot can find **any** object the tree asks for: an unknown name gets a made-up
position on the table (the same name always lands in the same place), so the LLM is free to invent
objects. `accept_any_object:=false` restricts it to the listed ones instead.

Objects and where they are (map frame; /get_object_pose only answers after visualize_object).
Set them so they survive a restart with the MOCK_OBJECTS environment variable:
  MOCK_OBJECTS="cup:0.40:0.20,box:0.70:0.50,bottle:0.20:0.60"
or at runtime (lost on restart):
  -p objects:="['cup','box']" -p object_positions:="['cup:0.40:0.20']"
Hand some servers to a real module (e.g. diff_nav) instead of faking them:
  -p disable:="['navigate_to_object','rotate_in_place','is_at_object']"
When navigate_to_object is disabled, "being at the object" is not checked here.
"""
import hashlib
import os
import random
import threading
import time

import rclpy
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.exceptions import ParameterUninitializedException
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.parameter import Parameter
from std_msgs.msg import Int16

from robot_interfaces.action import (
    GraspObject,
    NavigateToObject,
    NavigateToPoint,
    RotateInPlace,
    SetGripper,
    TrackObject,
    VisualizeObject,
)
from robot_interfaces.srv import GetObjectPose, ObjectQuery

ACTIONS = {
    "visualize_object": (VisualizeObject, 1.0),
    "navigate_to_object": (NavigateToObject, 2.0),
    "navigate_to_point": (NavigateToPoint, 2.0),
    "track_object": (TrackObject, 1.0),
    "grasp_object": (GraspObject, 1.5),
    "rotate_in_place": (RotateInPlace, 1.0),
    "set_gripper": (SetGripper, 0.5),
}


class MockRobot(Node):
    def __init__(self):
        super().__init__("mock_robot")
        # MOCK_OBJECTS="cup:0.40:0.20,box:0.70:0.50" survives container restarts;
        # ros2 param set does not.
        env = [o.strip() for o in os.environ.get("MOCK_OBJECTS", "").split(",") if o.strip()]
        positions = env or ["cup:0.40:0.20", "box:0.70:0.50", "bottle:0.20:0.60"]
        self.declare_parameter("objects", [p.split(":")[0] for p in positions])
        self.declare_parameter("object_positions", positions)
        # Anything the tree asks for can be found, at a made-up but stable position.
        self.declare_parameter("accept_any_object", True)
        self.declare_parameter("table_width", 1.2)
        self.declare_parameter("table_height", 0.8)
        self.invented = {}
        self.declare_parameter("disable", Parameter.Type.STRING_ARRAY)
        try:
            disabled = set(self.get_parameter("disable").value or [])
        except ParameterUninitializedException:
            disabled = set()
        self.nav_external = "navigate_to_object" in disabled
        for name, (_, delay) in ACTIONS.items():
            self.declare_parameter(f"delay.{name}", delay)
            self.declare_parameter(f"fail.{name}", 0.0)

        self.lock = threading.Lock()
        self.visible = set()
        self.located = set()
        self.at = None
        self.gripper_open = False
        self.held = None
        self.last_object = None
        self.position = 100   # 0 = open .. 100 = closed; starts closed, so trees must open it

        # The real gripper server (diff_nav) may own /set_gripper instead of us. Watch the
        # command topic so the fake world still knows what the jaws did, and IsObjectHeld
        # keeps answering sensibly either way.
        self.declare_parameter("gripper_topic", "/gripper")
        # 0 = fully open .. 100 = fully closed. Anything at or above this counts as a grip.
        self.declare_parameter("gripper_hold_position", 50)
        self.create_subscription(
            Int16, self.get_parameter("gripper_topic").value, self._on_gripper, 10)

        cb = ReentrantCallbackGroup()
        self._servers = []
        for name, (action_type, _) in ACTIONS.items():
            if name in disabled:
                continue
            self._servers.append(
                ActionServer(
                    self,
                    action_type,
                    name,
                    execute_callback=self._make_execute(name, action_type),
                    goal_callback=lambda _g: GoalResponse.ACCEPT,
                    cancel_callback=lambda _g: CancelResponse.ACCEPT,
                    callback_group=cb,
                )
            )
        for name, fn in {
            "is_object_visible": lambda o: o in self.visible,
            "is_at_object": lambda o: self.at == o,
            "is_object_held": lambda o: self.held == o,
        }.items():
            if name not in disabled:
                self.create_service(ObjectQuery, name, self._make_query(name, fn), callback_group=cb)
        if "get_object_pose" not in disabled:
            self.create_service(GetObjectPose, "get_object_pose", self._get_object_pose, callback_group=cb)

        self.get_logger().info(
            f"mock robot ready, objects={self._objects()}, disabled={sorted(disabled)}"
        )

    def _positions(self):
        out = dict(self.invented)
        for entry in self.get_parameter("object_positions").value:
            name, x, y = entry.split(":")
            out[name] = (float(x), float(y))
        return out

    def _invent(self, name):
        """A stable, plausible spot on the table for an object nobody placed."""
        digest = hashlib.sha1(name.encode()).digest()
        margin = 0.15
        x = margin + (digest[0] / 255) * (self.get_parameter("table_width").value - 2 * margin)
        y = margin + (digest[1] / 255) * (self.get_parameter("table_height").value - 2 * margin)
        self.invented[name] = (round(x, 3), round(y, 3))
        self.get_logger().info(f"invented a position for '{name}': ({x:.2f}, {y:.2f})")
        return self.invented[name]

    def _get_object_pose(self, req, res):
        name = req.object_name
        pos = self._positions().get(name)
        with self.lock:
            located = name in self.located
        if not located or pos is None:
            res.found = False
            res.message = (
                f"'{name}' has not been located yet; visualize it first" if pos
                else f"no position for '{name}'; VisualizeObject it first "
                     f"(placed objects: {', '.join(self._positions())})")
            return res
        res.found = True
        res.pose.header.frame_id = "map"
        res.pose.header.stamp = self.get_clock().now().to_msg()
        res.pose.pose.position.x, res.pose.pose.position.y = pos
        res.pose.pose.orientation.w = 1.0
        res.message = f"'{name}' at ({pos[0]:.2f}, {pos[1]:.2f})"
        return res

    def _on_gripper(self, msg):
        """Someone commanded the gripper directly; mirror it in the fake world."""
        with self.lock:
            self._set_gripper(msg.data)

    def _set_gripper(self, position):
        """Shared by /set_gripper and the /gripper topic. Called with self.lock held.

        The jaws are a 0 (open) .. 100 (closed) span, so "is it holding something" is a
        threshold, not a flag: closing only part way does not pick anything up.
        """
        hold = self.get_parameter("gripper_hold_position").value
        want_open = position < hold
        self.gripper_open = want_open
        self.position = position
        if want_open:
            self.held = None
            return f"gripper at {position} (open)"
        # Closing on the object we drove to picks it up, so a tree that ends in
        # Closing the gripper (no arm, fixed gripper) still satisfies IsObjectHeld.
        # With navigation handed to the real robot we never see the arrival, so fall
        # back to the last object the tree pointed at.
        target = self.at if self.at is not None else (
            self.last_object if self.nav_external else None)
        if target is not None:
            self.held = target
            return f"gripper closed to {position} on '{target}'"
        return f"gripper closed to {position} (nothing there to hold)"

    def _near(self, obj):
        return self.nav_external or self.at == obj

    def _objects(self):
        return list(self.get_parameter("objects").value)

    def _make_query(self, name, fn):
        def handle(req, res):
            with self.lock:
                res.result = bool(fn(req.object_name))
            self.get_logger().info(f"{name}({req.object_name}) -> {res.result}")
            return res

        return handle

    def _make_execute(self, name, action_type):
        def execute(goal_handle):
            goal = goal_handle.request
            arg = getattr(goal, "object_name", None)
            if arg is None:
                # Whatever the goal carries; never let logging be the thing that crashes us.
                if hasattr(goal, "position"):
                    arg = f"position={goal.position}"
                elif hasattr(goal, "angle_rad"):
                    arg = f"angle_rad={goal.angle_rad:.2f}"
                elif hasattr(goal, "x"):
                    arg = f"({goal.x:.2f}, {goal.y:.2f})"
                else:
                    arg = ""
            self.get_logger().info(f"{name}({arg}) started")

            result = action_type.Result()
            delay = float(self.get_parameter(f"delay.{name}").value)
            steps = max(1, int(delay / 0.1))
            for i in range(steps):
                if goal_handle.is_cancel_requested:
                    goal_handle.canceled()
                    result.success = False
                    result.message = "canceled"
                    self.get_logger().info(f"{name}({arg}) canceled")
                    return result
                self._feedback(goal_handle, action_type, 1.0 - i / steps)
                time.sleep(delay / steps)

            if random.random() < float(self.get_parameter(f"fail.{name}").value):
                ok, msg = False, "injected failure"
            else:
                with self.lock:
                    ok, msg = self._apply(name, goal)
            goal_handle.succeed()
            result.success = ok
            result.message = msg
            self.get_logger().info(f"{name}({arg}) -> {'OK' if ok else 'FAIL'} {msg}")
            return result

        return execute

    @staticmethod
    def _feedback(goal_handle, action_type, remaining):
        fb = action_type.Feedback()
        if hasattr(fb, "distance_remaining"):
            fb.distance_remaining = float(remaining)
        elif hasattr(fb, "angle_remaining"):
            fb.angle_remaining = float(remaining)
        else:
            fb.status = f"{int((1 - remaining) * 100)}%"
        goal_handle.publish_feedback(fb)

    def _apply(self, name, goal):
        """Update the fake world. Called with self.lock held."""
        obj = getattr(goal, "object_name", "")
        if name == "visualize_object":
            known = obj in self._objects() or obj in self.invented
            if not known:
                if not self.get_parameter("accept_any_object").value:
                    self.visible.discard(obj)
                    return False, (f"'{obj}' not found in view "
                                   f"(this robot knows about: {', '.join(self._objects())})")
                self._invent(obj)
            self.visible.add(obj)
            self.located.add(obj)
            self.last_object = obj
            return True, f"'{obj}' detected"
        if name == "rotate_in_place":
            self.visible.clear()
            return True, "rotated"
        if name == "navigate_to_point":
            self.at = None  # somewhere else on the table now
            return True, f"arrived at ({goal.x:.2f}, {goal.y:.2f})"
        if name == "navigate_to_object":
            if obj not in self.located:
                return False, f"'{obj}' location unknown; visualize it first"
            self.at = obj
            return True, f"arrived at '{obj}'"
        if name == "track_object":
            # TrackObject is now the node that does the approaching, so it no longer
            # requires having navigated first -- it only needs to know where to look.
            if obj not in self.located:
                return False, f"'{obj}' location unknown; visualize it first"
            self.at = obj
            self.last_object = obj
            return True, f"approached '{obj}'"
        if name == "grasp_object":
            if not self._near(obj):
                return False, f"not near '{obj}'"
            if not self.gripper_open:
                return False, f"gripper is closed (at {self.position}); open it first"
            self.held = obj
            return True, f"grasped '{obj}'"
        if name == "set_gripper":
            return True, self._set_gripper(goal.position)
        return False, f"unknown action {name}"


def main():
    rclpy.init()
    node = MockRobot()
    executor = MultiThreadedExecutor(num_threads=8)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
