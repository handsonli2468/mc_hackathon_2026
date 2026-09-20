#!/bin/bash 
git clone -b $ROS_DISTRO https://github.com/micro-ROS/micro_ros_setup.git src/micro_ros_setup
source /opt/ros/$ROS_DISTRO/setup.bash 
rosdep update && rosdep install --from-paths src --ignore-src -y
colcon build --packages-select micro_ros_setup
source install/setup.bash
ros2 run micro_ros_setup create_agent_ws.sh
ros2 run micro_ros_setup build_agent.sh
colcon build --packages-select micro_ros_agent
source install/setup.bash
#!/bin/bash