"""Gripper server for the real robot: turns /set_gripper goals into one /gripper message.

  /set_gripper  SetGripper.action  ->  publishes std_msgs/Int16 on /gripper, once

The value is how far the jaws are closed: **0 = fully open, 100 = fully closed**, and the
behavior tree chooses it with SetGripper.

The driver is fire-and-forget: there is no feedback from the hardware, so the command is
published a single time and then we simply wait for the jaws to finish moving before the
behavior tree is allowed to continue.

  ros2 param set /gripper_server settle_seconds 3.0   # jaws are slower than we thought

The publisher is transient-local ("latched") so the command is not lost when the motor
driver subscribes a moment later, and a driver that restarts picks up the last command.
"""
import time

import rclpy
from rclpy.action import ActionServer, CancelResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
# Int16, not Int32: that is what the MCU (cubeMX_node) subscribes to, and a type
# mismatch on a topic is silent - the action would succeed and the jaws never move.
from std_msgs.msg import Int16

from robot_interfaces.action import SetGripper


class GripperServer(Node):
    def __init__(self):
        super().__init__("gripper_server")
        self.declare_parameter("gripper_topic", "/gripper")
        self.declare_parameter("min_position", 0)      # fully open
        self.declare_parameter("max_position", 100)    # fully closed
        self.declare_parameter("settle_seconds", 2.0)

        latched = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        topic = self.get_parameter("gripper_topic").value
        self.pub = self.create_publisher(Int16, topic, latched)

        cb = ReentrantCallbackGroup()
        self._server = ActionServer(
            self, SetGripper, "set_gripper", self._execute,
            cancel_callback=lambda _: CancelResponse.ACCEPT, callback_group=cb,
        )
        self.get_logger().info(
            f"gripper server ready, publishing Int16 on {topic} "
            f"({self._p('min_position')} = open .. {self._p('max_position')} = closed, "
            f"then waiting {self._p('settle_seconds')} s)"
        )

    def _p(self, name):
        return self.get_parameter(name).value

    def _execute(self, goal_handle):
        lo, hi = self._p("min_position"), self._p("max_position")
        asked = goal_handle.request.position
        value = max(lo, min(hi, asked))
        settle = float(self._p("settle_seconds"))
        if value != asked:
            self.get_logger().warning(f"position {asked} is outside {lo}..{hi}; using {value}")
        word = "moving to " + str(value)
        done = f"at {value}"

        if self.pub.get_subscription_count() == 0:
            self.get_logger().warning(
                f"nothing is subscribed to {self._p('gripper_topic')}; the gripper driver may be "
                "down. Sending anyway (the message is latched)."
            )
        self.pub.publish(Int16(data=int(value)))
        self.get_logger().info(f"gripper -> {value}: published on {self._p('gripper_topic')}")

        # Wait for the jaws, in small steps so a cancel is noticed quickly.
        result = SetGripper.Result()
        deadline = time.monotonic() + settle
        while time.monotonic() < deadline:
            if not rclpy.ok():
                goal_handle.abort()
                result.success, result.message = False, "shutting down"
                return result
            if goal_handle.is_cancel_requested:
                # The command is already out; the jaws keep moving. Say so rather than lie.
                goal_handle.canceled()
                result.success, result.message = False, f"cancelled while the gripper was {word}"
                return result
            left = deadline - time.monotonic()
            fb = SetGripper.Feedback()
            fb.status = f"{word}, {max(left, 0.0):.1f} s left"
            goal_handle.publish_feedback(fb)
            time.sleep(min(0.1, max(left, 0.0)))

        goal_handle.succeed()
        result.success = True
        result.message = f"gripper {done} (sent {value}, waited {settle:.1f} s)"
        return result


def main():
    rclpy.init()
    node = GripperServer()
    executor = MultiThreadedExecutor(num_threads=4)
    try:
        rclpy.spin(node, executor=executor)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
