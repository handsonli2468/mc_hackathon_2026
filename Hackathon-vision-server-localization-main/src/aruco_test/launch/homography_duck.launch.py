import os
import tempfile

import yaml
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

# Parameter precedence (high -> low):
#   command line > param.yaml > LAUNCH_DEFAULTS below > C++ declare_parameter defaults
# Every launch argument defaults to '' (= not given), so only values actually typed on the
# command line are passed as overrides; everything else falls through to param.yaml.
# Note: rclcpp applies '/**' entries before node-name entries regardless of file order, so the
# command-line overrides are written under the node name to beat node sections in param.yaml.
LAUNCH_DEFAULTS = {
    'RGB_topic': '/camera/camera/color/image_raw',
    'camera_info_topic': '/camera/camera/color/camera_info',
    'pose_topic': '/pose/global/homography',
    'world_frame': 'map',
    'camera_frame': 'camera_color_optical_frame',
    'target_height': 0.2,
    'camera_pose_refresh_s': 1.0,
    'robot.id': 1,
    'robot.marker_size': 0.1,
    'plane_lm.enable': True,
    'plane_lm.pose_topic': '/duck/pose/plane_lm',
    'plane_lm.max_iter': 15,
    'pose_filter.enable': False,
    'pose_filter.alpha': 0.1,
    'pose_filter.max_jump_m': 0.15,
    'debug.enable': True,
    'debug.img': True,
    'debug.img_topic': '~/debug/image',
}

ARGUMENTS = [
    DeclareLaunchArgument(
        'params_file',
        default_value=os.path.join(get_package_share_directory('aruco_test'), 'config', 'param.yaml'),
        description='Parameter file (overrides launch defaults, overridden by command line)',
    ),
] + [
    DeclareLaunchArgument(name, default_value='', description=f'Override param.yaml (launch default: {value})')
    for name, value in LAUNCH_DEFAULTS.items()
]


def cast_like(default, text):
    """Convert a command-line string to the type of the launch default."""
    if isinstance(default, bool):
        value = yaml.safe_load(text)
        if not isinstance(value, bool):
            raise ValueError(f'expected true/false, got {text!r}')
        return value
    if isinstance(default, (int, float)):
        return type(default)(yaml.safe_load(text))
    return text


def write_node_params_file(node_name, params):
    """Write flat dotted params as a yaml file keyed by the node name."""
    tree = {}
    for name, value in params.items():
        *path, leaf = name.split('.')
        branch = tree
        for key in path:
            branch = branch.setdefault(key, {})
        branch[leaf] = value
    with tempfile.NamedTemporaryFile('w', prefix=f'{node_name}_cli_', suffix='.yaml', delete=False) as f:
        yaml.safe_dump({f'/{node_name}': {'ros__parameters': tree}}, f)
        return f.name


def launch_setup(context):
    overrides = {}
    for name, default in LAUNCH_DEFAULTS.items():
        text = LaunchConfiguration(name).perform(context)
        if text != '':
            overrides[name] = cast_like(default, text)

    homography_duck_node = Node(
        package='aruco_test',
        executable='homography_duck_node',
        name='homography_duck_node',
        output='screen',
        # launch defaults ('/**') < param.yaml < command line (node-name key, see note at top)
        parameters=[
            LAUNCH_DEFAULTS,
            LaunchConfiguration('params_file').perform(context),
            write_node_params_file('homography_duck_node', overrides),
        ],
    )
    return [homography_duck_node]


def generate_launch_description():
    return LaunchDescription(ARGUMENTS + [OpaqueFunction(function=launch_setup)])
