"""Simulated robot: sim_base (fake motors + fake global camera), sim_camera (fake onboard
camera feeding /object_detections) + the full robot stack.

  ros2 launch diff_nav sim.launch.py [x:=0.10 y:=0.10 yaw:=0.0] [use_rviz:=true]

Still needs /map from table_map (compose service `map`).
"""
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    share = get_package_share_directory("diff_nav")

    def arg(name):
        return ParameterValue(LaunchConfiguration(name), value_type=float)

    return LaunchDescription([
        DeclareLaunchArgument("x", default_value="0.10"),
        DeclareLaunchArgument("y", default_value="0.10"),
        DeclareLaunchArgument("yaw", default_value="0.0"),
        DeclareLaunchArgument("use_rviz", default_value="false"),
        Node(
            package="diff_nav",
            executable="sim_base",
            name="sim_base",
            parameters=[{"initial_x": arg("x"), "initial_y": arg("y"), "initial_yaw": arg("yaw")}],
            output="screen",
        ),
        Node(
            package="diff_nav",
            executable="sim_camera",
            name="sim_camera",
            output="screen",
        ),
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(os.path.join(share, "launch", "nav.launch.py"))
        ),
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(os.path.join(share, "launch", "rviz.launch.py")),
            condition=IfCondition(LaunchConfiguration("use_rviz")),
        ),
    ])
