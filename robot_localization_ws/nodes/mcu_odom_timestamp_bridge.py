#!/usr/bin/env python3

import rclpy
from nav_msgs.msg import Odometry
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node


class McuOdomTimestampBridge(Node):
    """Republish MCU odometry with a timestamp from the ROS host clock."""

    def __init__(self) -> None:
        super().__init__("mcu_odom_timestamp_bridge")

        self.declare_parameter("input_topic", "wheel/odom")
        self.declare_parameter("output_topic", "wheel/odom_stamped")

        input_topic = self.get_parameter("input_topic").value
        output_topic = self.get_parameter("output_topic").value

        if not isinstance(input_topic, str) or not input_topic:
            raise ValueError("input_topic must be a non-empty string")
        if not isinstance(output_topic, str) or not output_topic:
            raise ValueError("output_topic must be a non-empty string")
        if self.resolve_topic_name(input_topic) == self.resolve_topic_name(output_topic):
            raise ValueError("input_topic and output_topic must be different")

        self._publisher = self.create_publisher(Odometry, output_topic, 10)
        self._subscription = self.create_subscription(
            Odometry,
            input_topic,
            self._on_odometry,
            10,
        )

        self.get_logger().info(
            f"Bridging {input_topic} -> {output_topic} with ROS publish timestamps"
        )

    def _on_odometry(self, message: Odometry) -> None:
        # Keep every field supplied by the MCU and replace only its timestamp.
        message.header.stamp = self.get_clock().now().to_msg()
        self._publisher.publish(message)


def main(args=None) -> None:
    rclpy.init(args=args)
    node = None
    try:
        node = McuOdomTimestampBridge()
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
