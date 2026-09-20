
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

ARGUMENTS = [
	# Basic arguments
    DeclareLaunchArgument(
        "RGB_topic",
        default_value="/camera_cb/camera/color/image_raw",
        description="RGB image topic subscribed by homography_aruco_node",
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
        "target_height",
        default_value="0.447",
        description="Target height in meters",
    ),
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

	homography_aruco_node = Node(
		package='aruco_test',
		executable='homography_aruco_node',
		name='homography_aruco_node',
		output='screen',
        parameters=[
            {'RGB_topic': LaunchConfiguration("RGB_topic")},
            {'world_frame': LaunchConfiguration("world_frame")},
            {'camera_frame': LaunchConfiguration("camera_frame")},
            {'target_height': LaunchConfiguration("target_height")},
            {'marker_size': LaunchConfiguration("marker_size")},
            {'robot.id': LaunchConfiguration("robot.id")},
            {'rival.id': LaunchConfiguration("rival.id")},
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
	ld.add_action(homography_aruco_node)

	return ld
