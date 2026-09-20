"""Robot Pi: pose_bridge + Nav2 + diff_nav + the gripper. Motor driver and global camera run elsewhere.

  ros2 launch diff_nav nav.launch.py [params:=nav.yaml] [nav2_params:=nav2_table.yaml]

Needs /robot_pose (global camera) and /map (table_map on the mini PC).
"""
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from nav2_common.launch import RewrittenYaml


def generate_launch_description():
    share = get_package_share_directory("diff_nav")
    # Vendored copy of nav2_bringup's navigation_launch.py: identical except that the
    # velocity smoother publishes cmd_vel_ros, which cmd_vel_flip negates onto /cmd_vel.
    nav2_launch = os.path.join(share, "launch", "nav2_navigation.launch.py")
    params = LaunchConfiguration("params")
    nav2_params = RewrittenYaml(
        source_file=LaunchConfiguration("nav2_params"),
        param_rewrites={
            "default_nav_to_pose_bt_xml": os.path.join(share, "config", "nav2_bt_table.xml")
        },
        convert_types=True,
    )

    return LaunchDescription([
        DeclareLaunchArgument("params", default_value=os.path.join(share, "config", "nav.yaml")),
        DeclareLaunchArgument(
            "nav2_params", default_value=os.path.join(share, "config", "nav2_table.yaml")
        ),
        # The camera publishes the robot's pose as TF, not as a PoseStamped. This turns
        # it back into /robot_pose; it does nothing until robot_tf_frame is set in nav.yaml.
        Node(package="diff_nav", executable="tf_pose_bridge", name="tf_pose_bridge",
             parameters=[params], output="screen"),
        Node(package="diff_nav", executable="pose_bridge", name="pose_bridge",
             parameters=[params], output="screen"),
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(nav2_launch),
            launch_arguments={
                "params_file": nav2_params,
                "use_sim_time": "false",
                "autostart": "true",
                "use_composition": "False",
            }.items(),
        ),
        Node(package="diff_nav", executable="nav_server", name="diff_nav",
             parameters=[params], output="screen"),
        Node(package="diff_nav", executable="gripper", name="gripper_server",
             parameters=[params], output="screen"),
        # The MCU drives backwards for a positive linear.x; this is the only place that
        # knows it. Everything upstream speaks ordinary ROS.
        Node(package="diff_nav", executable="cmd_vel_flip", name="cmd_vel_flip",
             parameters=[params], output="screen"),
    ])
