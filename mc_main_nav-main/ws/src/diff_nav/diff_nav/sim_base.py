"""Kinematic differential-drive simulator standing in for the robot + global camera.

/cmd_vel in -> integrates a unicycle model -> /robot_pose out (PoseStamped, map
frame), exactly like the global camera would publish it. Stops if no cmd_vel
arrives for `cmd_timeout` seconds (like a real motor driver should).

Test hooks (runtime parameters):
  ros2 param set /sim_base teleport "0.10,0.10,0.0"   # jump to x,y,yaw
  ros2 param set /sim_base camera_enabled false       # simulate losing the camera
"""
import math
import random

import rclpy
from geometry_msgs.msg import PoseStamped, Twist
from rcl_interfaces.msg import SetParametersResult
from rclpy.node import Node


class SimBase(Node):
    def __init__(self):
        super().__init__("sim_base")
        dp = self.declare_parameter
        self.x = dp("initial_x", 0.10).value
        self.y = dp("initial_y", 0.10).value
        self.yaw = dp("initial_yaw", 0.0).value
        self.frame = dp("frame_id", "map").value
        dp("cmd_timeout", 0.5)       # runtime-tunable
        dp("velocity_noise", 0.0)    # relative, e.g. 0.05 = 5 %; runtime-tunable
        dp("pose_noise", 0.0)        # metres of camera jitter; runtime-tunable
        dp("camera_enabled", True)   # runtime-tunable
        dp("teleport", "")           # "x,y,yaw"; applied when set
        self.add_on_set_parameters_callback(self._on_params)
        rate = dp("rate", 50.0).value
        camera_rate = dp("camera_rate", 20.0).value

        self.v = self.w = 0.0
        self.last_cmd = self.get_clock().now()
        self.create_subscription(Twist, dp("cmd_vel_topic", "/cmd_vel").value, self._on_cmd, 10)
        self.pub = self.create_publisher(PoseStamped, dp("pose_topic", "/robot_pose").value, 10)
        self.dt = 1.0 / rate
        self.create_timer(self.dt, self._step)
        self.create_timer(1.0 / camera_rate, self._publish)
        self.get_logger().info(f"sim_base at ({self.x:.3f}, {self.y:.3f}, {self.yaw:.2f})")

    def _param(self, name):
        return self.get_parameter(name).value

    def _on_params(self, params):
        for p in params:
            if p.name == "teleport" and p.value:
                try:
                    x, y, yaw = (float(v) for v in p.value.split(","))
                except ValueError:
                    return SetParametersResult(successful=False, reason="teleport: 'x,y,yaw'")
                self.x, self.y, self.yaw = x, y, yaw
                self.v = self.w = 0.0
                self.get_logger().info(f"teleported to ({x:.3f}, {y:.3f}, {yaw:.2f})")
        return SetParametersResult(successful=True)

    def _on_cmd(self, msg):
        self.v, self.w = msg.linear.x, msg.angular.z
        self.last_cmd = self.get_clock().now()

    def _step(self):
        if (self.get_clock().now() - self.last_cmd).nanoseconds * 1e-9 > self._param("cmd_timeout"):
            self.v = self.w = 0.0
        noise = self._param("velocity_noise")
        v = self.v * (1.0 + random.gauss(0.0, noise))
        w = self.w * (1.0 + random.gauss(0.0, noise))
        self.x += v * math.cos(self.yaw) * self.dt
        self.y += v * math.sin(self.yaw) * self.dt
        self.yaw = math.atan2(math.sin(self.yaw + w * self.dt), math.cos(self.yaw + w * self.dt))

    def _publish(self):
        if not self._param("camera_enabled"):
            return
        jitter = self._param("pose_noise")
        msg = PoseStamped()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = self.frame
        msg.pose.position.x = self.x + random.gauss(0.0, jitter)
        msg.pose.position.y = self.y + random.gauss(0.0, jitter)
        msg.pose.orientation.z = math.sin(self.yaw / 2.0)
        msg.pose.orientation.w = math.cos(self.yaw / 2.0)
        self.pub.publish(msg)


def main():
    rclpy.init()
    node = SimBase()
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
