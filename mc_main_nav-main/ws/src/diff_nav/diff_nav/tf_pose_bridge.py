"""Robot pose published as TF by somebody else -> the PoseStamped we expect.

The camera team's `pnp_duck_node` publishes the robot's pose as a transform
(map -> pnp_duck_1) rather than as a PoseStamped on /robot_pose, which is what
pose_bridge and the BT's approach nodes read. This node closes that gap: it looks
up the transform and republishes it as a PoseStamped, changing nothing else.

  ros2 run diff_nav tf_pose_bridge --ros-args -p source_frame:=pnp_duck_1

It is deliberately quiet about a missing transform: the camera drops out often
enough that a warning per lookup would bury everything else. One line when the
pose starts flowing, one when it stops.
"""
import math

import rclpy
from geometry_msgs.msg import PoseStamped
from rclpy.node import Node
from tf2_ros import Buffer, TransformListener

from diff_nav.geometry import yaw_from_quaternion


class TfPoseBridge(Node):
    def __init__(self):
        super().__init__("tf_pose_bridge")
        dp = self.declare_parameter
        self.map_frame = dp("map_frame", "map").value
        # The frame the camera gives the robot. Empty disables the node entirely, so it
        # can sit in the launch file until somebody sets it.
        self.source_frame = dp("source_frame", "").value
        self.rate = float(dp("publish_rate", 20.0).value)
        # A transform older than this is not a pose any more, it is a memory.
        self.max_age_s = float(dp("max_age_s", 1.0).value)
        # ROS says +x is forward. If the marker is mounted the other way round, the camera
        # reports the robot facing backwards, and Nav2 drives in reverse to reach a goal
        # in front of it. 180 turns the reported heading round; the position is unaffected.
        self.yaw_offset = math.radians(float(dp("yaw_offset_deg", 0.0).value))

        topic = dp("pose_topic", "/robot_pose").value
        self.pub = self.create_publisher(PoseStamped, topic, 10)

        if not self.source_frame:
            self.get_logger().info(
                "tf_pose_bridge: source_frame is not set, so nothing is republished "
                "(set it to the frame the camera gives the robot, e.g. pnp_duck_1)")
            return

        self.buffer = Buffer()
        self.listener = TransformListener(self.buffer, self)
        self.have_pose = False
        self.create_timer(1.0 / self.rate, self._tick)
        turned = ("" if self.yaw_offset == 0.0
                  else f", heading turned by {math.degrees(self.yaw_offset):g} deg")
        self.get_logger().info(
            f"tf_pose_bridge: TF {self.map_frame}->{self.source_frame} -> {topic} "
            f"at {self.rate:g} Hz{turned}")

    def _tick(self):
        try:
            # Time() = "the latest you have", rather than a stamp the camera may not
            # have reached yet.
            tf = self.buffer.lookup_transform(
                self.map_frame, self.source_frame, rclpy.time.Time())
        except Exception:
            if self.have_pose:
                self.have_pose = False
                self.get_logger().warn(
                    f"lost {self.map_frame}->{self.source_frame}; "
                    "the robot pose has stopped")
            return

        stamp = rclpy.time.Time.from_msg(tf.header.stamp)
        if stamp.nanoseconds > 0 and self.max_age_s > 0.0:
            age = (self.get_clock().now() - stamp).nanoseconds * 1e-9
            if age > self.max_age_s:
                if self.have_pose:
                    self.have_pose = False
                    self.get_logger().warn(
                        f"{self.map_frame}->{self.source_frame} is {age:.1f} s old; "
                        "not republishing a stale pose")
                return

        msg = PoseStamped()
        msg.header.stamp = tf.header.stamp
        msg.header.frame_id = self.map_frame
        msg.pose.position.x = tf.transform.translation.x
        msg.pose.position.y = tf.transform.translation.y
        # The robot drives on a table: whatever height the marker sits at is not the
        # robot's height, and a non-zero z would only confuse the planner.
        msg.pose.position.z = 0.0
        if self.yaw_offset == 0.0:
            msg.pose.orientation = tf.transform.rotation
        else:
            r = tf.transform.rotation
            yaw = yaw_from_quaternion(r.x, r.y, r.z, r.w) + self.yaw_offset
            msg.pose.orientation.z = math.sin(yaw / 2.0)
            msg.pose.orientation.w = math.cos(yaw / 2.0)
        self.pub.publish(msg)

        if not self.have_pose:
            self.have_pose = True
            self.get_logger().info(
                f"robot pose flowing: {self.map_frame}->{self.source_frame} "
                f"at ({msg.pose.position.x:.2f}, {msg.pose.position.y:.2f})")


def main():
    rclpy.init()
    node = TfPoseBridge()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
