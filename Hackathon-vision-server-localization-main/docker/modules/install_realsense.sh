#!/bin/bash
set -e

echo "Installing RealSense ros packages for ROS distro: ${ROS_DISTRO:-humble}"

sudo apt-get update && sudo apt-get install -y \
    ros-humble-point-cloud-transport \
    ros-humble-librealsense2* \
    ros-humble-realsense2-* \
    && sudo rm -rf /var/lib/apt/lists/*

echo "RealSense installation completed successfully!"
