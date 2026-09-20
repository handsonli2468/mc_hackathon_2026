from glob import glob

from setuptools import setup

package_name = "diff_nav"

setup(
    name=package_name,
    version="0.1.0",
    packages=[package_name],
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        ("share/" + package_name + "/launch", glob("launch/*.launch.py")),
        ("share/" + package_name + "/config", glob("config/*.yaml") + glob("config/*.xml")),
        ("share/" + package_name + "/rviz", glob("rviz/*.rviz")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "nav_server = diff_nav.nav_server:main",
            "sim_base = diff_nav.sim_base:main",
            "pose_bridge = diff_nav.pose_bridge:main",
            "tf_pose_bridge = diff_nav.tf_pose_bridge:main",
            "cmd_vel_flip = diff_nav.cmd_vel_flip:main",
            "sim_camera = diff_nav.sim_camera:main",
            "gripper = diff_nav.gripper:main",
        ],
    },
)
