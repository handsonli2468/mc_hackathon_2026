"""Global-camera pose -> what Nav2 needs.

Used when the global camera is the robot's only position source: it publishes
  TF  map -> base_link   the camera pose (no odom frame: there is nothing to put in it)
  /odom                  nav_msgs/Odometry in the map frame, velocity estimated from
                         successive poses (Nav2's controller needs a velocity feed)
If poses stop arriving, it stops publishing, so Nav2 sees a stale transform and halts.

When the robot has its own wheel odometry and an EKF publishing map -> odom -> base_link,
that chain owns the robot's position instead, and two publishers of map -> base_link would
fight. Set publish_tf (and usually publish_odom) to false: the node then does nothing, and
tf_pose_bridge still turns the camera's TF into /robot_pose for the behaviour tree.
"""
import math

import rclpy
from geometry_msgs.msg import PoseStamped, TransformStamped
from nav_msgs.msg import Odometry
from rclpy.node import Node
from rclpy.time import Time
from tf2_ros import TransformBroadcaster

from diff_nav.geometry import wrap, yaw_from_quaternion


class PoseBridge(Node):
    def __init__(self):
        super().__init__("pose_bridge")
        dp = self.declare_parameter
        self.map_frame = dp("map_frame", "map").value
        self.base_frame = dp("base_frame", "base_link").value
        self.smoothing = dp("velocity_smoothing", 0.5).value  # 0 = raw, towards 1 = smoother
        # False when somebody else owns the robot's position (an EKF publishing
        # map -> odom -> base_link). A frame can only have one parent.
        self.publish_tf = bool(dp("publish_tf", True).value)
        self.publish_odom = bool(dp("publish_odom", True).value)

        self.tf = TransformBroadcaster(self) if self.publish_tf else None

        self.odom_pub = (
            self.create_publisher(Odometry, dp("odom_topic", "/odom").value, 10)
            if self.publish_odom else None)
        topic = dp("pose_topic", "/robot_pose").value
        self.create_subscription(PoseStamped, topic, self._on_pose, 10)
        self._last = None  # (t, x, y, yaw)
        self._v = 0.0
        self._w = 0.0
        if self.publish_tf or self.publish_odom:
            parts = []
            if self.publish_tf:
                parts.append(f"TF {self.map_frame}->{self.base_frame}")
            if self.publish_odom:
                parts.append("/odom")
            self.get_logger().info(f"pose_bridge: {topic} -> " + " + ".join(parts))
        else:
            self.get_logger().info(
                "pose_bridge: publish_tf and publish_odom are both false, so this node "
                "does nothing; the robot's own odometry owns map->odom->base_link")

    def _on_pose(self, msg):
        stamp = Time.from_msg(msg.header.stamp)
        if stamp.nanoseconds == 0:
            stamp = self.get_clock().now()
        t = stamp.nanoseconds * 1e-9
        p = msg.pose.position
        o = msg.pose.orientation
        yaw = yaw_from_quaternion(o.x, o.y, o.z, o.w)

        if self._last is not None:
            dt = t - self._last[0]
            if dt <= 0.0:
                return  # duplicate or out-of-order pose
            dx, dy = p.x - self._last[1], p.y - self._last[2]
            # Forward speed in the robot frame (negative when reversing).
            v = (dx * math.cos(yaw) + dy * math.sin(yaw)) / dt
            w = wrap(yaw - self._last[3]) / dt
            a = self.smoothing
            self._v = a * self._v + (1.0 - a) * v
            self._w = a * self._w + (1.0 - a) * w
        self._last = (t, p.x, p.y, yaw)

        if self.tf is not None:
            tf = TransformStamped()
            tf.header.stamp = stamp.to_msg()
            tf.header.frame_id = self.map_frame
            tf.child_frame_id = self.base_frame
            tf.transform.translation.x = p.x
            tf.transform.translation.y = p.y
            tf.transform.rotation = o
            self.tf.sendTransform(tf)

        if self.odom_pub is not None:
            odom = Odometry()
            odom.header.stamp = stamp.to_msg()
            odom.header.frame_id = self.map_frame
            odom.child_frame_id = self.base_frame
            odom.pose.pose = msg.pose
            odom.twist.twist.linear.x = self._v
            odom.twist.twist.angular.z = self._w
            self.odom_pub.publish(odom)


def main():
    rclpy.init()
    node = PoseBridge()
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
