
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

ARGUMENTS = [
	# Basic arguments
    DeclareLaunchArgument(
        "RGB_topic",
        default_value="/camera/camera/color/image_raw",
        description="RGB image topic subscribed by homography_sima_node",
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

	# Static camera TF (world_frame -> camera_link), measured by hand
	DeclareLaunchArgument(
        "cam_tf.enable",
        default_value="true",
        description="Publish static TF from world_frame to cam_tf.child_frame",
    ),
	DeclareLaunchArgument(
        "cam_tf.child_frame",
        default_value="camera_link",
        description="Root frame of the RealSense TF tree",
    ),
	DeclareLaunchArgument("cam_tf.x", default_value="1.6759", description="Camera X in world frame (m)"),
	DeclareLaunchArgument("cam_tf.y", default_value="2.0753", description="Camera Y in world frame (m)"),
	DeclareLaunchArgument("cam_tf.z", default_value="1.4602", description="Camera Z in world frame (m)"),
	DeclareLaunchArgument("cam_tf.roll", default_value="0.0", description="Camera roll (rad)"),
	DeclareLaunchArgument("cam_tf.pitch", default_value="1.5708", description="Camera pitch (rad), 1.5708 = looking straight down"),
	DeclareLaunchArgument("cam_tf.yaw", default_value="0.0", description="Camera yaw (rad)"),

	# Target arguments
	DeclareLaunchArgument(
        "marker_size",
        default_value="0.1",
        description="Marker size in meters (for markers on the field)",
    ),
	DeclareLaunchArgument(
        "sima.ids",
        default_value="[1, 2, 3, 4, 5, 11, 12, 13, 14, 15, 19, 20]",
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
        default_value="false",
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
        default_value="true",
        description="Enable image show for debugging",
    ),
]


def generate_launch_description():

	homography_sima_node = Node(
		package='aruco_test',
		executable='homography_sima_node',
		name='homography_sima_node',
		output='screen',
        parameters=[
            {'RGB_topic': LaunchConfiguration("RGB_topic")},
            {'world_frame': LaunchConfiguration("world_frame")},
            {'camera_frame': LaunchConfiguration("camera_frame")},
            {'marker_size': LaunchConfiguration("marker_size")},
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

	camera_static_tf = Node(
		package='tf2_ros',
		executable='static_transform_publisher',
		name='camera_static_tf',
		output='screen',
		condition=IfCondition(LaunchConfiguration("cam_tf.enable")),
		arguments=[
			'--x', LaunchConfiguration("cam_tf.x"),
			'--y', LaunchConfiguration("cam_tf.y"),
			'--z', LaunchConfiguration("cam_tf.z"),
			'--roll', LaunchConfiguration("cam_tf.roll"),
			'--pitch', LaunchConfiguration("cam_tf.pitch"),
			'--yaw', LaunchConfiguration("cam_tf.yaw"),
			'--frame-id', LaunchConfiguration("world_frame"),
			'--child-frame-id', LaunchConfiguration("cam_tf.child_frame"),
		],
	)

	ld = LaunchDescription(ARGUMENTS)

	ld.add_action(camera_static_tf)
	ld.add_action(homography_sima_node)

	return ld
