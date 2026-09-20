FROM ros:humble-ros-core-jammy

RUN apt-get update \
    && apt-get install -y --no-install-recommends ros-humble-robot-localization \
        ros-$ROS_DISTRO-tf2-tools \
    && rm -rf /var/lib/apt/lists/*

COPY nodes/mcu_odom_timestamp_bridge.py /opt/robot-localization/nodes/mcu_odom_timestamp_bridge.py
