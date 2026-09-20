"""RViz for the table robot (PC). Works for the simulator and for the real robot on
the same ROS_DOMAIN_ID.

  ros2 launch diff_nav rviz.launch.py [rviz_config:=...]

Top-down view of the table: map + obstacles, costmap, Nav2 path, robot footprint.
"2D Goal Pose" sends a raw Nav2 goal (bypasses the BT engine), handy for testing.
"""
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    default = os.path.join(get_package_share_directory("diff_nav"), "rviz", "table.rviz")
    return LaunchDescription([
        DeclareLaunchArgument("rviz_config", default_value=default),
        Node(
            package="rviz2",
            executable="rviz2",
            name="rviz2",
            arguments=["-d", LaunchConfiguration("rviz_config")],
            output="screen",
        ),
    ])
