import os
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from ament_index_python.packages import get_package_share_directory

ARGUMENTS = [
	# Basic arguments
    DeclareLaunchArgument(
        "RGB_topic",
        default_value="/camera_cb/camera/color/image_raw",
        description="RGB image topic subscribed by homography_all_node",
    ),
	DeclareLaunchArgument(
        "world_frame",
        default_value="map",
        description="World frame",
    ),
	DeclareLaunchArgument(
        "camera_frame",
        default_value="camera_color_optical_frame",
        description="Camera frame",
    ),
    DeclareLaunchArgument(
        "mode",
        default_value="all",
        description="Localization mode: all, robot, sima",
    ),
	# Target arguments
	DeclareLaunchArgument(
        "marker_size",
        default_value="0.1",
        description="Marker size in meters (for markers on the field)",
    ),
	DeclareLaunchArgument(
        "robot.id",
        default_value="2",
        description="Robot ID",
    ),
	DeclareLaunchArgument(
        "rival.enable",
        default_value="false",
        description="Enable rival detection",
    ),
    DeclareLaunchArgument(
        "rival.id",
        default_value="6",
        description="Rival ID",
    ),
	DeclareLaunchArgument(
        "robot.height",
        default_value="0.445",
        description="Robot pose offset along marker yaw direction (meters)",
    ),
	DeclareLaunchArgument(
        "sima.ids",
        default_value="[1, 2, 3, 4]",
        description="SIMA marker id list, e.g. [1, 2, 3]",
    ),
	DeclareLaunchArgument(
        "sima.offset.x",
        default_value="0.029",
        description="SIMA pose offset along marker yaw direction (meters)",
    ),
    DeclareLaunchArgument(
        "sima.height",
        default_value="0.15",
        description="SIMA marker height in meters",
    ),
    DeclareLaunchArgument(
        "ninja.ids",
        default_value="10",
        description="Ninja marker id list",
    ),
    DeclareLaunchArgument(
        "ninja.offset.x",
        default_value="-0.003",
        description="Ninja pose offset along marker yaw direction (meters)",
    ),
    DeclareLaunchArgument(
        "ninja.offset.y",
        default_value="-0.02",
        description="Ninja pose offset along marker pitch direction (meters)",
    ),
    DeclareLaunchArgument(
        "ninja.height",
        default_value="0.205",
        description="Ninja marker height in meters",
    ),

	# Pose filter arguments
	DeclareLaunchArgument(
        "pose_filter.enable",
        default_value="true",
        description="Enable pose filter",
    ),
	DeclareLaunchArgument(
        "pose_filter.alpha",
        default_value="0.1",
        description="Pose filter alpha",
    ),
	DeclareLaunchArgument(
        "pose_filter.max_jump_m",
        default_value="0.15",
        description="Pose filter max jump in meters",
    ),

	# Debug arguments
	DeclareLaunchArgument(
        "debug.enable",
        default_value="true",
        description="Enable debug mode",
    ),
    DeclareLaunchArgument(
        "debug.img",
        default_value="false",
        description="Enable image show for debugging",
    ),
]


def generate_launch_description():
    config = os.path.join(
        get_package_share_directory('aruco_test'),
        'config',
        'params.yaml'
    )
    homography_all_node = Node(
		package='aruco_test',
		executable='homography_all_node',
		name='homography_all_node',
		output='screen',
        parameters=[
			config,
            {'RGB_topic': LaunchConfiguration("RGB_topic")},
            {'world_frame': LaunchConfiguration("world_frame")},
            {'camera_frame': LaunchConfiguration("camera_frame")},
            {'marker_size': LaunchConfiguration("marker_size")},
            {'mode': LaunchConfiguration("mode")},
            {'robot.id': LaunchConfiguration("robot.id")},
			{'rival.enable': LaunchConfiguration("rival.enable")},
			{'rival.id': LaunchConfiguration("rival.id")},
			{'robot.height': LaunchConfiguration("robot.height")},
            {'sima.ids': LaunchConfiguration("sima.ids")},
            {'sima.offset.x': LaunchConfiguration("sima.offset.x")},
            {'sima.height': LaunchConfiguration("sima.height")},
            {'ninja.ids': LaunchConfiguration("ninja.ids")},
            {'ninja.offset.x': LaunchConfiguration("ninja.offset.x")},
            {'ninja.offset.y': LaunchConfiguration("ninja.offset.y")},
            {'ninja.height': LaunchConfiguration("ninja.height")},
            {'pose_filter.enable': LaunchConfiguration("pose_filter.enable")},
            {'pose_filter.alpha': LaunchConfiguration("pose_filter.alpha")},
            {'pose_filter.max_jump_m': LaunchConfiguration("pose_filter.max_jump_m")},
            {'debug.enable': LaunchConfiguration("debug.enable")},
            {'debug.img': LaunchConfiguration("debug.img")},
        ]
	)

    ld = LaunchDescription(ARGUMENTS)

    ld.add_action(homography_all_node)

    return ld
