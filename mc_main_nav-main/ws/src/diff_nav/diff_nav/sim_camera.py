"""Stand-in for the onboard camera pipeline, for testing without hardware.

Behaves like a fixed forward camera: it publishes /object_detections (relative x/y in
the robot frame) for objects inside its field of view and range, and reports
`visible: false` otherwise — including when the object is too close to see, which is
what a fixed forward camera with no tilt actually does.

Object positions come from the VLM's /get_object_pose service (the mock provides it),
refreshed slowly; the robot pose comes from the global camera topic.
"""
import math
import random
import threading
import time

import rclpy
from geometry_msgs.msg import PoseStamped
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

from diff_nav.geometry import wrap, yaw_from_quaternion
from robot_interfaces.msg import ObjectDetection
from robot_interfaces.srv import GetObjectPose


class SimCamera(Node):
    def __init__(self):
        super().__init__("sim_camera")
        dp = self.declare_parameter
        self.objects = dp("objects", ["cup", "box", "bottle"]).value
        dp("fov_deg", 60.0)          # total horizontal field of view
        dp("max_range", 1.0)         # m, further than this is not reported
        dp("min_range", 0.08)        # m, closer than this leaves the frame
        dp("bearing_noise", 0.01)    # rad
        dp("range_noise", 0.005)     # m
        rate = dp("rate", 10.0).value
        refresh = dp("pose_refresh", 1.0).value

        self._lock = threading.Lock()
        self._robot = None           # (x, y, yaw)
        self._world = {}             # object -> (x, y) in the map frame

        cb = ReentrantCallbackGroup()
        self.pub = self.create_publisher(
            ObjectDetection, dp("detections_topic", "/object_detections").value, 10)
        self.create_subscription(
            PoseStamped, dp("pose_topic", "/robot_pose").value, self._on_pose, 10,
            callback_group=cb)
        self.client = self.create_client(GetObjectPose, "get_object_pose", callback_group=cb)
        self.create_timer(1.0 / rate, self._tick, callback_group=cb)
        self.create_timer(refresh, self._refresh_world, callback_group=cb)
        self.get_logger().info(
            f"sim_camera: {self.objects}, fov {self._p('fov_deg')} deg, "
            f"range {self._p('min_range')}-{self._p('max_range')} m")

    def _p(self, name):
        return self.get_parameter(name).value

    def _on_pose(self, msg):
        o = msg.pose.orientation
        with self._lock:
            self._robot = (
                msg.pose.position.x, msg.pose.position.y,
                yaw_from_quaternion(o.x, o.y, o.z, o.w))

    def _refresh_world(self):
        if not self.client.service_is_ready():
            return
        for name in self._p("objects"):
            req = GetObjectPose.Request()
            req.object_name = name
            future = self.client.call_async(req)
            deadline = time.monotonic() + 1.0
            while not future.done() and time.monotonic() < deadline:
                time.sleep(0.01)
            res = future.result() if future.done() else None
            if res is None:
                continue
            with self._lock:
                if res.found:
                    self._world[name] = (res.pose.pose.position.x, res.pose.pose.position.y)
                else:
                    self._world.pop(name, None)

    def _tick(self):
        with self._lock:
            robot, world = self._robot, dict(self._world)
        if robot is None:
            return
        half_fov = math.radians(self._p("fov_deg")) / 2.0
        for name in self._p("objects"):
            msg = ObjectDetection()
            msg.header.stamp = self.get_clock().now().to_msg()
            msg.header.frame_id = "base_link"
            msg.object_name = name
            pos = world.get(name)
            if pos is not None:
                dx, dy = pos[0] - robot[0], pos[1] - robot[1]
                distance = math.hypot(dx, dy)
                bearing = wrap(math.atan2(dy, dx) - robot[2])
                in_view = (abs(bearing) <= half_fov
                           and self._p("min_range") <= distance <= self._p("max_range"))
                if in_view:
                    distance += random.gauss(0.0, self._p("range_noise"))
                    bearing += random.gauss(0.0, self._p("bearing_noise"))
                    msg.visible = True
                    msg.confidence = 0.9
                    msg.x = float(distance * math.cos(bearing))
                    msg.y = float(distance * math.sin(bearing))
            self.pub.publish(msg)


def main():
    rclpy.init()
    node = SimCamera()
    executor = MultiThreadedExecutor(num_threads=4)
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
