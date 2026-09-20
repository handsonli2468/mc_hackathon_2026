import os

import yaml
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription, OpaqueFunction
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

# Node parameter priority (highest first):
#   1. command line `name:=value` (any parameter listed in the params file)
#   2. params file (params_file argument, default config/params.yaml)
#   3. NODE_ARG_DEFAULTS below
#   4. declare_parameter defaults inside orb_tracker_node
# Node-parameter launch arguments default to UNSET meaning "not given on the command line",
# so they never override the params file. An explicit empty value (e.g. target_image_path:='') is passed on.
UNSET = "<params_file>"
NODE_ARG_DEFAULTS = {
    "target_image_path": "target.png",
    "world_frame": "map",
    "init.enable": "false",
    "debug.enable": "true",
    "debug.img": "false",
}

# mask_init_tool parameters that can be given on the command line (value = type reference)
TOOL_PARAM_DEFAULTS = {
    "cache_s": 3.0,
    "delay_s": 1.5,
    "use_grabcut": True,
    "grabcut_iters": 3,
}

ARGUMENTS = [
    DeclareLaunchArgument(
        "params_file",
        default_value=os.path.join(get_package_share_directory('object_tracker'), 'config', 'params.yaml'),
        description="Node parameter file",
    ),

    # Basic arguments (node parameters)
    DeclareLaunchArgument(
        "target_image_path",
        default_value=UNSET,
        description="Target image: absolute path, or file name under share/object_tracker/targets "
                    "(empty = params file, else 'target.png')",
    ),
    DeclareLaunchArgument(
        "world_frame",
        default_value=UNSET,
        description="Output frame; the robot TF tree must connect it to camera_link. Use camera_link for bench tests "
                    "(empty = params file, else 'map')",
    ),

    DeclareLaunchArgument(
        "use_sim_time",
        default_value="false",
        description="Use /clock (bag replay with --clock); passed to the tracker and mask_init_tool",
    ),

    # Mask init (simulated upstream VLM masks)
    DeclareLaunchArgument(
        "init.enable",
        default_value=UNSET,
        description="Subscribe to masks and build targets from them (unset = params file, else false)",
    ),
    DeclareLaunchArgument(
        "launch_init_tool",
        default_value="false",
        description="Also start mask_init_tool (needs a display); delay_s:=, cache_s:=, use_grabcut:=, "
                    "grabcut_iters:= are passed to it",
    ),

    # RealSense camera
    DeclareLaunchArgument(
        "launch_camera",
        default_value="true",
        description="Also launch realsense2_camera (D405) with aligned depth",
    ),
    DeclareLaunchArgument(
        "camera_namespace",
        default_value="camera_duck",
        description="Camera namespace passed to rs_launch.py; topics are /<camera_namespace>/<camera_name>/...",
    ),
    DeclareLaunchArgument(
        "camera_name",
        default_value="camera",
        description="Camera node name passed to rs_launch.py; also the TF prefix (<camera_name>_link)",
    ),
    DeclareLaunchArgument(
        "camera_profile",
        default_value="848,480,30",
        description="D405 color and depth profile (width,height,fps); keep both at the same fps for sync",
    ),

    # Static camera TF (world_frame -> camera_link)
    # The robot normally publishes this; enable only for standalone tests.
    DeclareLaunchArgument(
        "cam_tf.enable",
        default_value="false",
        description="Publish static TF from world_frame to cam_tf.child_frame (only if the robot does not)",
    ),
    DeclareLaunchArgument(
        "cam_tf.child_frame",
        default_value="camera_link",
        description="Root frame of the RealSense TF tree",
    ),
    DeclareLaunchArgument("cam_tf.x", default_value="0.0", description="Camera X in world frame (m)"),
    DeclareLaunchArgument("cam_tf.y", default_value="0.0", description="Camera Y in world frame (m)"),
    DeclareLaunchArgument("cam_tf.z", default_value="0.0", description="Camera Z in world frame (m)"),
    DeclareLaunchArgument("cam_tf.roll", default_value="0.0", description="Camera roll (rad)"),
    DeclareLaunchArgument("cam_tf.pitch", default_value="0.0", description="Camera pitch (rad)"),
    DeclareLaunchArgument("cam_tf.yaw", default_value="0.0", description="Camera yaw (rad)"),

    # Debug arguments (node parameters)
    DeclareLaunchArgument(
        "debug.enable",
        default_value=UNSET,
        description="Enable debug log and tracked_object TF (empty = params file, else true)",
    ),
    DeclareLaunchArgument(
        "debug.img",
        default_value=UNSET,
        description="Publish the debug image on debug.image_topic (empty = params file, else false)",
    ),
]


def load_node_params(path):
    """Return the ros__parameters of the params file (all node sections merged)."""
    with open(path, 'r') as f:
        data = yaml.safe_load(f) or {}
    params = {}
    for section in data.values():
        if isinstance(section, dict):
            params.update(section.get('ros__parameters', {}))
    return params


def parse_value(text, reference=None):
    """Parse a command-line string like YAML; follow the params file type so 100 stays a double."""
    value = yaml.safe_load(text)
    if isinstance(reference, str):
        # keep text like "123" as a string; "''" (ros2 launch rejects a bare empty value) becomes ""
        return value if isinstance(value, str) else text
    if isinstance(reference, float) and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    if isinstance(reference, str) and not isinstance(value, str):
        return text
    return value


def launch_setup(context):
    params_file = LaunchConfiguration("params_file").perform(context)
    file_params = load_node_params(params_file)

    # layer 3: launch defaults, only for keys the params file does not set
    launch_defaults = {k: parse_value(v) for k, v in NODE_ARG_DEFAULTS.items()}

    # layer 1: command-line values; declared node arguments are UNSET unless given, other node
    # parameters only exist in the launch configurations when passed on the command line
    overrides = {}
    for key in set(file_params) | set(NODE_ARG_DEFAULTS):
        text = context.launch_configurations.get(key, UNSET)
        if text == UNSET:
            continue
        value = parse_value(text, file_params.get(key, launch_defaults.get(key)))
        if value is not None:
            overrides[key] = value
    use_sim_time = parse_value(LaunchConfiguration("use_sim_time").perform(context), False)

    # same priority for the frame used by the static TF
    world_frame = str(overrides.get("world_frame", file_params.get("world_frame", launch_defaults["world_frame"])))

    orb_tracker_node = Node(
        package='object_tracker',
        executable='orb_tracker_node',
        name='orb_tracker_node',
        output='screen',
        parameters=[launch_defaults, params_file, overrides, {'use_sim_time': use_sim_time}],
    )

    # the tool talks to the tracker, so it uses the tracker's effective topics
    def effective(key, default):
        return overrides.get(key, file_params.get(key, default))

    tool_params = {
        'color_topic': effective('color_topic', '/camera_duck/camera/color/image_rect_raw'),
        'mask_topic': effective('mask_topic', '/tracked_object/init_mask'),
        'use_sim_time': use_sim_time,
    }
    for key, reference in TOOL_PARAM_DEFAULTS.items():
        text = context.launch_configurations.get(key)
        if text:
            tool_params[key] = parse_value(text, reference)

    mask_init_tool = Node(
        package='object_tracker',
        executable='mask_init_tool',
        name='mask_init_tool',
        output='screen',
        condition=IfCondition(LaunchConfiguration("launch_init_tool")),
        parameters=[tool_params],
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
            '--frame-id', world_frame,
            '--child-frame-id', LaunchConfiguration("cam_tf.child_frame"),
        ],
    )

    return [camera_static_tf, orb_tracker_node, mask_init_tool]


def generate_launch_description():
    realsense = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(get_package_share_directory('realsense2_camera'), 'launch', 'rs_launch.py')
        ),
        condition=IfCondition(LaunchConfiguration("launch_camera")),
        launch_arguments={
            'camera_namespace': LaunchConfiguration("camera_namespace"),
            'camera_name': LaunchConfiguration("camera_name"),
            'align_depth.enable': 'true',
            'enable_sync': 'true',
            # D405 has no RGB module: color is configured on the depth module
            'depth_module.color_profile': LaunchConfiguration("camera_profile"),
            'depth_module.depth_profile': LaunchConfiguration("camera_profile"),
        }.items(),
    )

    ld = LaunchDescription(ARGUMENTS)

    ld.add_action(realsense)
    ld.add_action(OpaqueFunction(function=launch_setup))

    return ld
