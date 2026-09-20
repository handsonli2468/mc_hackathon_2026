import os

import yaml
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

# Node parameter priority (highest first):
#   1. command line `name:=value` (any parameter listed in the params file)
#   2. params file (params_file argument, default config/vlm_bridge_params.yaml)
#   3. NODE_ARG_DEFAULTS below
#   4. declare_parameter defaults inside vlm_bridge_node
# Node-parameter launch arguments default to UNSET meaning "not given on the command line",
# so they never override the params file.
UNSET = "<params_file>"
NODE_ARG_DEFAULTS = {
    "debug.enable": "true",
}

ARGUMENTS = [
    DeclareLaunchArgument(
        "params_file",
        default_value=os.path.join(get_package_share_directory('object_tracker'), 'config', 'vlm_bridge_params.yaml'),
        description="Node parameter file",
    ),
    DeclareLaunchArgument(
        "vlm.endpoint",
        default_value=UNSET,
        description="VLM server address, e.g. tcp://192.168.0.100:5555 (unset = params file)",
    ),
    DeclareLaunchArgument(
        "use_sim_time",
        default_value="false",
        description="Use /clock (bag replay with --clock)",
    ),
    DeclareLaunchArgument(
        "debug.enable",
        default_value=UNSET,
        description="Print the 2 s VLM stats line (unset = params file, else true)",
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

    vlm_bridge_node = Node(
        package='object_tracker',
        executable='vlm_bridge_node',
        name='vlm_bridge_node',
        output='screen',
        parameters=[launch_defaults, params_file, overrides, {'use_sim_time': use_sim_time}],
    )

    return [vlm_bridge_node]


def generate_launch_description():
    ld = LaunchDescription(ARGUMENTS)
    ld.add_action(OpaqueFunction(function=launch_setup))
    return ld
