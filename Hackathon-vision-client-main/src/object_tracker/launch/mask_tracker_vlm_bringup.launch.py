import os

import yaml
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription, OpaqueFunction
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

# mask_tracker_vlm_node: mask_tracker_node with the VLM bridge built in (no separate vlm_bridge_node, no
# mask_init_tool). The color image is subscribed only once.
#
# Node parameter priority (highest first):
#   1. command line `name:=value` (any parameter listed in either params file, e.g. vlm.endpoint:=...)
#   2. params files: params_file (tracker, default config/mask_tracker_params.yaml), then only the vlm.* keys of
#      vlm_params_file (default config/vlm_bridge_params.yaml), so both setups share one set of VLM settings
#   3. NODE_ARG_DEFAULTS below
#   4. declare_parameter defaults inside the node
# Node-parameter launch arguments default to UNSET meaning "not given on the command line",
# so they never override the params files.
UNSET = "<params_file>"
NODE_ARG_DEFAULTS = {
    "world_frame": "map",
    "debug.enable": "true",
    "debug.img": "false",
}

SHARE = get_package_share_directory('object_tracker')

ARGUMENTS = [
    DeclareLaunchArgument(
        "params_file",
        default_value=os.path.join(SHARE, 'config', 'mask_tracker_params.yaml'),
        description="Tracker parameter file",
    ),
    DeclareLaunchArgument(
        "vlm_params_file",
        default_value=os.path.join(SHARE, 'config', 'vlm_bridge_params.yaml'),
        description="VLM client parameter file; only its vlm.* keys are used",
    ),

    # Basic arguments (node parameters)
    DeclareLaunchArgument(
        "world_frame",
        default_value=UNSET,
        description="Output frame; the robot TF tree must connect it to camera_link. Use camera_link for bench tests "
                    "(unset = params file, else 'map')",
    ),

    DeclareLaunchArgument(
        "use_sim_time",
        default_value="false",
        description="Use /clock (bag replay with --clock)",
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
        description="Enable debug log, VLM stats and tracked_object TF (unset = params file, else true)",
    ),
    DeclareLaunchArgument(
        "debug.img",
        default_value=UNSET,
        description="Publish the debug image on debug.image_topic (unset = params file, else false)",
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
    return value


def launch_setup(context):
    params_file = LaunchConfiguration("params_file").perform(context)
    tracker_params = load_node_params(params_file)
    # the bridge file also sets color_topic, mask_topic and debug.enable; only its VLM settings apply here
    vlm_params = {k: v for k, v in load_node_params(LaunchConfiguration("vlm_params_file").perform(context)).items()
                  if k.startswith('vlm.')}
    file_params = {**tracker_params, **vlm_params}

    # layer 3: launch defaults, only for keys the params files do not set
    launch_defaults = {k: parse_value(v) for k, v in NODE_ARG_DEFAULTS.items()}

    # layer 1: command-line values; declared node arguments are UNSET unless given, other node
    # parameters only exist in the launch configurations when passed on the command line
    overrides = {}
    for key in set(file_params) | set(NODE_ARG_DEFAULTS) | {'vlm.publish_mask'}:
        text = context.launch_configurations.get(key, UNSET)
        if text == UNSET:
            continue
        value = parse_value(text, file_params.get(key, launch_defaults.get(key)))
        if value is not None:
            overrides[key] = value
    use_sim_time = parse_value(LaunchConfiguration("use_sim_time").perform(context), False)

    # same priority for the frame used by the static TF
    world_frame = str(overrides.get("world_frame", file_params.get("world_frame", launch_defaults["world_frame"])))

    tracker_node = Node(
        package='object_tracker',
        executable='mask_tracker_vlm_node',
        name='mask_tracker_vlm_node',
        output='screen',
        parameters=[launch_defaults, params_file, vlm_params, overrides, {'use_sim_time': use_sim_time}],
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

    return [camera_static_tf, tracker_node]


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
