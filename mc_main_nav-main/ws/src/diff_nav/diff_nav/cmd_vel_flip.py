"""Turn ROS's velocity convention into the one the MCU actually implements.

ROS says linear.x > 0 drives forward. This robot's firmware drives BACKWARDS for a
positive linear.x, which is fatal for a closed loop: Nav2 commands forward to close the
distance to a goal, the robot moves away, the error grows, so it commands more. It runs
away from goals rather than reaching them.

Until the firmware is fixed, everything that would publish /cmd_vel publishes
/cmd_vel_ros instead (see nav2_navigation_launch.py and nav.yaml), and this node
republishes it on /cmd_vel with the signs corrected.

A 180-degree mounting flips x and y but leaves rotation about z alone, so the default
negates linear.x only. flip_angular is there for a robot whose wheels are also swapped.

  THIS NODE IS A WORKAROUND. When the MCU is fixed: set flip_linear false (or drop the
  node), point Nav2 back at nav2_bringup's own launch, and set cmd_vel_topic to /cmd_vel.
"""
import rclpy
from geometry_msgs.msg import Twist
from rclpy.node import Node


class CmdVelFlip(Node):
    def __init__(self):
        super().__init__("cmd_vel_flip")
        dp = self.declare_parameter
        self.flip_linear = bool(dp("flip_linear", True).value)
        self.flip_angular = bool(dp("flip_angular", False).value)
        in_topic = dp("input_topic", "/cmd_vel_ros").value
        out_topic = dp("output_topic", "/cmd_vel").value
        if in_topic == out_topic:
            raise RuntimeError(
                f"input_topic and output_topic are both '{in_topic}'; a node cannot "
                "subscribe and publish on the same topic")

        self.pub = self.create_publisher(Twist, out_topic, 10)
        self.create_subscription(Twist, in_topic, self._on_cmd, 10)
        self.get_logger().info(
            f"cmd_vel_flip: {in_topic} -> {out_topic} "
            f"(linear {'negated' if self.flip_linear else 'as-is'}, "
            f"angular {'negated' if self.flip_angular else 'as-is'})")

    def _on_cmd(self, msg):
        out = Twist()
        out.linear.x = -msg.linear.x if self.flip_linear else msg.linear.x
        out.linear.y = -msg.linear.y if self.flip_linear else msg.linear.y
        out.angular.z = -msg.angular.z if self.flip_angular else msg.angular.z
        self.pub.publish(out)


def main():
    rclpy.init()
    node = CmdVelFlip()
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
