"""Object-level navigation on top of Nav2 (runs on the robot Pi).

Serves (see robot_interfaces):
  /navigate_to_object  NavigateToObject.action  drive next to an object, facing it
  /navigate_to_point   NavigateToPoint.action   drive to a coordinate in the map frame
  /rotate_in_place     RotateInPlace.action     turn by a relative angle (Nav2 Spin)
  /track_object        TrackObject.action       camera-guided approach; the servo drives
                                                /cmd_vel directly, without Nav2
  /is_at_object        ObjectQuery.srv          is the robot at its last stop distance?
Uses:
  /get_object_pose     GetObjectPose.srv        object position (from the VLM module)
  /navigate_to_pose    nav2_msgs NavigateToPose
  /spin                nav2_msgs Spin
  /object_detections   onboard camera pipeline: where the object is, relative to the robot
  <costmap_topic>      Nav2 global costmap, to pick a reachable spot next to the object
  TF map -> base_link  robot pose

Stop distance (goal.stop_distance) is measured from the robot's FRONT to the object's
centre; `robot_front_offset` converts it to a centre-to-centre distance.
"""
import math
import threading
import time
from dataclasses import fields

import rclpy
from action_msgs.msg import GoalStatus
from builtin_interfaces.msg import Duration
from geometry_msgs.msg import Twist
from nav2_msgs.msg import SpeedLimit
from nav2_msgs.action import NavigateToPose, Spin
from nav_msgs.msg import OccupancyGrid
from rclpy.action import ActionClient, ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from rclpy.time import Time
from tf2_ros import Buffer, TransformException, TransformListener

from diff_nav.geometry import (
    GridLookup,
    pick_approach_pose,
    quaternion_from_yaw,
    yaw_from_quaternion,
)
from diff_nav.visual_servo import ServoParams, VisualServo
from robot_interfaces.action import (
    NavigateToObject,
    NavigateToPoint,
    RotateInPlace,
    TrackObject,
)
from robot_interfaces.msg import ObjectDetection
from robot_interfaces.srv import GetObjectPose, ObjectQuery

STATUS_NAMES = {
    GoalStatus.STATUS_SUCCEEDED: "succeeded",
    GoalStatus.STATUS_ABORTED: "aborted",
    GoalStatus.STATUS_CANCELED: "canceled",
}


class Canceled(Exception):
    pass


class NavServer(Node):
    def __init__(self):
        super().__init__("diff_nav")
        dp = self.declare_parameter
        self.map_frame = dp("map_frame", "map").value
        self.base_frame = dp("base_frame", "base_link").value
        dp("default_stop_distance", 0.05)  # used when a goal sends stop_distance <= 0
        dp("robot_front_offset", 0.05)     # robot centre -> front edge (m). MEASURE ON THE ROBOT.
        dp("at_object_tolerance", 0.02)    # extra slack for /is_at_object
        dp("approach_candidates", 16)      # spots tried around the object
        dp("free_threshold", 99)           # costmap value (0-100) from which a cell counts as blocked
        dp("nav_timeout", 90.0)
        dp("rotate_timeout", 20.0)
        dp("pose_timeout", 0.5)            # TF older than this = no pose
        dp("pose_lost_timeout", 3.0)       # no pose this long while moving -> cancel Nav2 and fail
        # Camera-guided approach (TrackObject)
        dp("servo_rate", 10.0)
        dp("servo_timeout", 40.0)
        dp("detection_timeout", 1.0)       # a detection older than this counts as "not visible"
        for f in fields(ServoParams):
            if f.name not in ("front_offset", "stop_distance"):
                dp(f"servo.{f.name}", f.default)

        self._lock = threading.Lock()
        self._motion_token = 0
        self._stop_distance = {}           # object -> stop distance of its last navigate goal
        self._costmap = None
        self._detections = {}              # object -> ((x, y) or None, monotonic time)

        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)

        cb = ReentrantCallbackGroup()
        latched = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.create_subscription(
            OccupancyGrid, dp("costmap_topic", "/global_costmap/costmap").value,
            self._on_costmap, latched, callback_group=cb,
        )
        self.create_subscription(
            ObjectDetection, dp("detections_topic", "/object_detections").value,
            self._on_detection, 10, callback_group=cb)
        self._cmd_pub = self.create_publisher(Twist, dp("cmd_vel_topic", "/cmd_vel").value, 10)
        # Nav2's controller applies this as a percentage of its configured maximum, so the
        # behavior tree can ask for a slower approach without us touching any parameters.
        self._speed_pub = self.create_publisher(
            SpeedLimit, dp("speed_limit_topic", "/speed_limit").value, 1)
        self._object_client = self.create_client(
            GetObjectPose, dp("object_pose_service", "get_object_pose").value, callback_group=cb
        )
        self._nav = ActionClient(self, NavigateToPose, "navigate_to_pose", callback_group=cb)
        self._spin = ActionClient(self, Spin, "spin", callback_group=cb)

        accept = dict(
            goal_callback=lambda _g: GoalResponse.ACCEPT,
            cancel_callback=lambda _g: CancelResponse.ACCEPT,
            callback_group=cb,
        )
        self._servers = [
            ActionServer(self, NavigateToObject, "navigate_to_object", self._execute_nav, **accept),
            ActionServer(self, RotateInPlace, "rotate_in_place", self._execute_rotate, **accept),
            ActionServer(
                self, NavigateToPoint, "navigate_to_point", self._execute_point, **accept),
            ActionServer(self, TrackObject, "track_object", self._execute_track, **accept),
        ]
        self.create_service(ObjectQuery, "is_at_object", self._is_at_object, callback_group=cb)
        self.get_logger().info("diff_nav ready (Nav2 backend)")

    # ------------------------------------------------------------------ helpers
    def _param(self, name):
        return self.get_parameter(name).value

    def _load(self, prefix, cls, **overrides):
        """Build a params dataclass from ROS parameters `<prefix>.<field>`."""
        values = {
            f.name: float(self._param(f"{prefix}.{f.name}"))
            for f in fields(cls) if f.name not in overrides
        }
        return cls(**values, **overrides)

    def _on_detection(self, msg):
        with self._lock:
            self._detections[msg.object_name] = (
                (msg.x, msg.y) if msg.visible else None, time.monotonic())

    def _fresh_detection(self, name):
        """(x, y) if the pipeline can see it right now, else None."""
        with self._lock:
            entry = self._detections.get(name)
        if entry is None or time.monotonic() - entry[1] > self._param("detection_timeout"):
            return None
        return entry[0]

    def _publish_cmd(self, v, w):
        msg = Twist()
        msg.linear.x = float(v)
        msg.angular.z = float(w)
        self._cmd_pub.publish(msg)

    def _on_costmap(self, msg):
        info = msg.info
        grid = GridLookup(
            info.width, info.height, info.resolution,
            info.origin.position.x, info.origin.position.y, msg.data,
        )
        with self._lock:
            self._costmap = grid

    def _robot_pose(self):
        """(x, y, yaw) in the map frame, or None if TF is missing or stale."""
        try:
            tf = self.tf_buffer.lookup_transform(self.map_frame, self.base_frame, Time())
        except TransformException:
            return None
        age = (self.get_clock().now() - Time.from_msg(tf.header.stamp)).nanoseconds * 1e-9
        if age > self._param("pose_timeout"):
            return None
        t, r = tf.transform.translation, tf.transform.rotation
        return t.x, t.y, yaw_from_quaternion(r.x, r.y, r.z, r.w)

    @staticmethod
    def _wait(future, timeout, goal_handle=None):
        """Wait for a future; raise Canceled if our own goal gets a cancel request."""
        deadline = time.monotonic() + timeout
        while not future.done():
            if goal_handle is not None and goal_handle.is_cancel_requested:
                raise Canceled()
            if time.monotonic() > deadline:
                return False
            time.sleep(0.02)
        return True

    def _query_object(self, name, timeout=2.0):
        """((x, y), message) or (None, reason)."""
        if not self._object_client.wait_for_service(timeout_sec=timeout):
            return None, f"object pose service '{self._object_client.srv_name}' not available"
        req = GetObjectPose.Request()
        req.object_name = name
        future = self._object_client.call_async(req)
        if not self._wait(future, timeout):
            self._object_client.remove_pending_request(future)
            return None, "object pose service timed out"
        res = future.result()
        if not res.found:
            return None, res.message or f"'{name}' has not been located"
        return (res.pose.pose.position.x, res.pose.pose.position.y), res.message

    def _begin_motion(self):
        with self._lock:
            self._motion_token += 1
            return self._motion_token

    def _is_current(self, token):
        with self._lock:
            return self._motion_token == token

    def _finish(self, goal_handle, result, ok, message, how="succeed"):
        getattr(goal_handle, how)()
        result.success = ok
        result.message = message
        # rclpy forbids one call site logging at different severities.
        if ok:
            self.get_logger().info(message)
        else:
            self.get_logger().warning(message)
        return result

    def _run_nav2(self, client, name, goal, goal_handle, token, timeout, on_feedback=None):
        """Send a goal to a Nav2 action and follow it. Returns (ok, message)."""
        if not client.wait_for_server(timeout_sec=2.0):
            return False, f"Nav2 '{name}' not available"
        send = client.send_goal_async(goal, feedback_callback=on_feedback)
        if not self._wait(send, 5.0, goal_handle):
            return False, f"Nav2 '{name}' did not answer"
        nav_handle = send.result()
        if not nav_handle.accepted:
            return False, f"Nav2 '{name}' rejected the goal"
        result_future = nav_handle.get_result_async()
        started = time.monotonic()
        lost_since = None
        try:
            while not result_future.done():
                now = time.monotonic()
                if goal_handle.is_cancel_requested:
                    raise Canceled()
                if not self._is_current(token):
                    nav_handle.cancel_goal_async()
                    return False, "preempted by a newer motion goal"
                if now - started > timeout:
                    nav_handle.cancel_goal_async()
                    return False, f"timed out after {timeout:.0f}s"
                # Nav2 keeps waiting for a transform when the camera drops out; don't.
                if self._robot_pose() is None:
                    lost_since = lost_since or now
                    if now - lost_since > self._param("pose_lost_timeout"):
                        nav_handle.cancel_goal_async()
                        return False, "lost the robot pose (global camera) while moving"
                else:
                    lost_since = None
                time.sleep(0.05)
        except Canceled:
            nav_handle.cancel_goal_async()
            raise
        status = result_future.result().status
        if status == GoalStatus.STATUS_SUCCEEDED:
            return True, ""
        return False, f"Nav2 '{name}' {STATUS_NAMES.get(status, f'status {status}')}"

    # ------------------------------------------------------------------ actions
    def _execute_nav(self, goal_handle):
        name = goal_handle.request.object_name
        stop = goal_handle.request.stop_distance
        if stop <= 0.0:
            stop = self._param("default_stop_distance")
        token = self._begin_motion()
        result = NavigateToObject.Result()
        self.get_logger().info(f"navigate_to_object({name}, stop {stop * 100:.1f} cm)")
        with self._lock:
            self._stop_distance[name] = stop

        target, msg = self._query_object(name)
        if target is None:
            return self._finish(goal_handle, result, False, msg)
        pose = self._robot_pose()
        if pose is None:
            return self._finish(goal_handle, result, False, "no robot pose (TF map -> base_link)")
        with self._lock:
            costmap = self._costmap
        if costmap is None:
            return self._finish(goal_handle, result, False, "no costmap from Nav2 yet")

        centre = stop + self._param("robot_front_offset")
        threshold = self._param("free_threshold")
        spot = pick_approach_pose(
            pose[:2], target, centre,
            lambda x, y: costmap.is_free(x, y, threshold),
            self._param("approach_candidates"),
        )
        if spot is None:
            return self._finish(
                goal_handle, result, False,
                f"no free spot {stop * 100:.1f} cm from '{name}' (blocked by obstacles or the table edge)",
            )

        goal = NavigateToPose.Goal()
        goal.pose.header.frame_id = self.map_frame
        goal.pose.header.stamp = self.get_clock().now().to_msg()
        goal.pose.pose.position.x = spot[0]
        goal.pose.pose.position.y = spot[1]
        q = quaternion_from_yaw(spot[2])
        (goal.pose.pose.orientation.x, goal.pose.pose.orientation.y,
         goal.pose.pose.orientation.z, goal.pose.pose.orientation.w) = q

        def feedback(msg):
            fb = NavigateToObject.Feedback()
            fb.distance_remaining = float(msg.feedback.distance_remaining)
            goal_handle.publish_feedback(fb)

        try:
            ok, why = self._run_nav2(
                self._nav, "navigate_to_pose", goal, goal_handle, token, self._param("nav_timeout"), feedback
            )
        except Canceled:
            return self._finish(goal_handle, result, False, "canceled", "canceled")
        if ok:
            return self._finish(
                goal_handle, result, True, f"arrived at '{name}' ({stop * 100:.1f} cm away)"
            )
        how = "abort" if why.startswith("preempted") else "succeed"
        return self._finish(goal_handle, result, False, f"navigating to '{name}': {why}", how)

    def _set_speed_limit(self, percent):
        """Ask Nav2's controller for a share of its maximum speed. 0/100 means no limit."""
        msg = SpeedLimit()
        msg.header.frame_id = self.map_frame
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.percentage = True
        # 0 means "leave it alone"; Nav2 reads 100% as no limit either way.
        msg.speed_limit = 100.0 if percent <= 0.0 else float(min(percent, 100.0))
        self._speed_pub.publish(msg)

    def _goto(self, goal_handle, result, token, x, y, yaw, what, feedback_cls, speed_percent=0.0):
        """Send one pose to Nav2 and follow it. Returns the finished result."""
        self._set_speed_limit(speed_percent)
        goal = NavigateToPose.Goal()
        goal.pose.header.frame_id = self.map_frame
        goal.pose.header.stamp = self.get_clock().now().to_msg()
        goal.pose.pose.position.x = float(x)
        goal.pose.pose.position.y = float(y)
        q = quaternion_from_yaw(yaw)
        (goal.pose.pose.orientation.x, goal.pose.pose.orientation.y,
         goal.pose.pose.orientation.z, goal.pose.pose.orientation.w) = q

        def publish_feedback(msg):
            fb = feedback_cls()
            fb.distance_remaining = float(msg.feedback.distance_remaining)
            goal_handle.publish_feedback(fb)

        try:
            ok, why = self._run_nav2(
                self._nav, "navigate_to_pose", goal, goal_handle, token,
                self._param("nav_timeout"), publish_feedback,
            )
        except Canceled:
            self._set_speed_limit(100.0)
            return self._finish(goal_handle, result, False, "canceled", "canceled")
        finally_speed = 100.0
        self._set_speed_limit(finally_speed)
        if ok:
            return self._finish(goal_handle, result, True, f"arrived at {what}")
        how = "abort" if why.startswith("preempted") else "succeed"
        return self._finish(goal_handle, result, False, f"navigating to {what}: {why}", how)

    def _execute_point(self, goal_handle):
        req = goal_handle.request
        token = self._begin_motion()
        result = NavigateToPoint.Result()
        self.get_logger().info(
            f"navigate_to_point({req.x:.3f}, {req.y:.3f})"
            + (f" facing {req.yaw_deg:.0f} deg" if req.use_yaw else "")
            + (f" at {req.speed_percent:.0f}% speed" if req.speed_percent > 0 else "")
        )
        pose = self._robot_pose()
        if pose is None:
            return self._finish(goal_handle, result, False, "no robot pose (TF map -> base_link)")
        with self._lock:
            costmap = self._costmap
        if costmap is None:
            return self._finish(goal_handle, result, False, "no costmap from Nav2 yet")
        if not costmap.is_free(req.x, req.y, self._param("free_threshold")):
            return self._finish(
                goal_handle, result, False,
                f"({req.x:.3f}, {req.y:.3f}) is blocked, off the table, or too close to an obstacle",
            )
        yaw = (math.radians(req.yaw_deg) if req.use_yaw
               else math.atan2(req.y - pose[1], req.x - pose[0]))
        return self._goto(
            goal_handle, result, token, req.x, req.y, yaw,
            f"({req.x:.3f}, {req.y:.3f})", NavigateToPoint.Feedback,
            speed_percent=getattr(req, "speed_percent", 0.0),
        )

    def _execute_rotate(self, goal_handle):
        angle = goal_handle.request.angle_rad
        token = self._begin_motion()
        result = RotateInPlace.Result()
        self.get_logger().info(f"rotate_in_place({math.degrees(angle):.0f} deg)")

        timeout = self._param("rotate_timeout")
        goal = Spin.Goal()
        goal.target_yaw = float(angle)
        goal.time_allowance = Duration(sec=int(timeout))
        try:
            ok, why = self._run_nav2(self._spin, "spin", goal, goal_handle, token, timeout + 2.0)
        except Canceled:
            return self._finish(goal_handle, result, False, "canceled", "canceled")
        if ok:
            return self._finish(goal_handle, result, True, f"rotated {math.degrees(angle):.0f} deg")
        how = "abort" if why.startswith("preempted") else "succeed"
        return self._finish(goal_handle, result, False, f"rotating: {why}", how)

    def _execute_track(self, goal_handle):
        """Camera-guided approach: drive on what the onboard camera sees, not the map."""
        name = goal_handle.request.object_name
        stop = goal_handle.request.stop_distance
        if stop <= 0.0:
            stop = self._param("default_stop_distance")
        token = self._begin_motion()
        result = TrackObject.Result()
        self.get_logger().info(f"track_object({name}, stop {stop * 100:.1f} cm)")
        with self._lock:
            self._stop_distance[name] = stop

        params = self._load(
            "servo", ServoParams,
            front_offset=self._param("robot_front_offset"), stop_distance=stop)
        servo = VisualServo(params)
        period = 1.0 / self._param("servo_rate")
        started = time.monotonic()
        previous = self._robot_pose()
        if previous is None:
            return self._finish(goal_handle, result, False, "no robot pose (TF map -> base_link)")
        travelled = 0.0

        while rclpy.ok():
            tick = time.monotonic()
            if goal_handle.is_cancel_requested:
                self._publish_cmd(0.0, 0.0)
                return self._finish(goal_handle, result, False, "canceled", "canceled")
            if not self._is_current(token):
                self._publish_cmd(0.0, 0.0)
                return self._finish(
                    goal_handle, result, False, "preempted by a newer motion goal", "abort")
            if tick - started > self._param("servo_timeout"):
                self._publish_cmd(0.0, 0.0)
                return self._finish(
                    goal_handle, result, False,
                    f"camera approach to '{name}' timed out after "
                    f"{self._param('servo_timeout'):.0f}s")

            pose = self._robot_pose()
            if pose is None:
                self._publish_cmd(0.0, 0.0)
                return self._finish(
                    goal_handle, result, False, "lost the robot pose (global camera)")
            travelled += math.hypot(pose[0] - previous[0], pose[1] - previous[1])
            previous = pose

            command = servo.update(self._fresh_detection(name), travelled)
            if command.failure:
                self._publish_cmd(0.0, 0.0)
                return self._finish(
                    goal_handle, result, False, f"camera approach to '{name}': {command.failure}")
            if command.done:
                self._publish_cmd(0.0, 0.0)
                how = " (last stretch blind, too close for the camera)" if command.blind else ""
                return self._finish(
                    goal_handle, result, True,
                    f"lined up with '{name}' at {stop * 100:.1f} cm{how}")
            self._publish_cmd(command.v, command.w)
            fb = TrackObject.Feedback()
            fb.status = f"{'blind ' if command.blind else ''}gap {command.gap * 100:.1f} cm"
            goal_handle.publish_feedback(fb)
            time.sleep(max(0.0, period - (time.monotonic() - tick)))

        self._publish_cmd(0.0, 0.0)
        return self._finish(goal_handle, result, False, "shutting down", "abort")

    # ------------------------------------------------------------------ services
    def _is_at_object(self, req, res):
        pose = self._robot_pose()
        if pose is None:
            res.result, res.message = False, "no robot pose (TF map -> base_link)"
            return res
        target, msg = self._query_object(req.object_name, timeout=1.0)
        if target is None:
            res.result, res.message = False, msg
            return res
        with self._lock:
            stop = self._stop_distance.get(req.object_name, self._param("default_stop_distance"))
        gap = math.hypot(target[0] - pose[0], target[1] - pose[1]) - self._param("robot_front_offset")
        limit = stop + self._param("at_object_tolerance")
        res.result = gap <= limit
        res.message = f"front is {gap * 100:.1f} cm from '{req.object_name}' (limit {limit * 100:.1f} cm)"
        return res


def main():
    rclpy.init()
    node = NavServer()
    executor = MultiThreadedExecutor(num_threads=6)
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
