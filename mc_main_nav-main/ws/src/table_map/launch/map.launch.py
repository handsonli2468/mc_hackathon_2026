"""Mini PC: table map + obstacle web UI.

  ros2 launch table_map map.launch.py [width:=1.8 height:=1.2 http_port:=8081]
"""
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    def num(name, kind=float):
        return ParameterValue(LaunchConfiguration(name), value_type=kind)

    args = {
        "width": "1.8",           # field size (m) until the camera publishes /table_map
        "height": "1.2",
        "resolution": "0.005",
        "edge_margin": "0.01",
        "http_port": "8081",
        "obstacles_file": "~/mc_main_nav/data/obstacles.json",
    }
    return LaunchDescription(
        [DeclareLaunchArgument(k, default_value=v) for k, v in args.items()]
        + [
            Node(
                package="table_map",
                executable="map_node",
                name="table_map",
                parameters=[{
                    "width": num("width"),
                    "height": num("height"),
                    "resolution": num("resolution"),
                    "edge_margin": num("edge_margin"),
                    "http_port": num("http_port", int),
                    "obstacles_file": LaunchConfiguration("obstacles_file"),
                }],
                output="screen",
            )
        ]
    )
