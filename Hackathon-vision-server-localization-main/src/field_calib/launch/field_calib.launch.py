import os
import tempfile

import yaml
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

# Parameter precedence (high -> low):
#   command line > param.yaml > LAUNCH_DEFAULTS below > node declare_parameter defaults
# Every launch argument defaults to '' (= not given), so only values actually typed on the
# command line are passed as overrides (written under the node name, see aruco_test launch files).
LAUNCH_DEFAULTS = {
    'image_topic': '/camera/camera/color/image_raw',
    'camera_info_topic': '/camera/camera/color/camera_info',
    'world_frame': 'map',
    'field_file': '',
    'field.disabled_segments': '',
    'calib.frames': 30,
    'calib.stage_delay': 1.0,
    'live.enable': True,
    'live.period': 1.0,
    'live.compact': False,
    'live.jpeg_quality': 80,
    'depth.enable': True,
    'calib.apply': True,
    'calib.on_startup': 'if_missing',
    'calib.hold_s': 15.0,
}

ARGUMENTS = [
    DeclareLaunchArgument(
        'params_file',
        default_value=os.path.join(get_package_share_directory('field_calib'), 'config', 'param.yaml'),
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

    node = Node(
        package='field_calib',
        executable='field_calib_node',
        name='field_calib_node',
        output='screen',
        parameters=[
            LAUNCH_DEFAULTS,
            LaunchConfiguration('params_file').perform(context),
            write_node_params_file('field_calib_node', overrides),
        ],
    )
    return [node]


def generate_launch_description():
    return LaunchDescription(ARGUMENTS + [OpaqueFunction(function=launch_setup)])
