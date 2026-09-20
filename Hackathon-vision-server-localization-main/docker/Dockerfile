FROM ros:humble AS base

# Keep apt cache to speed up subsequent builds
RUN rm -f /etc/apt/apt.conf.d/docker-clean; echo 'Binary::apt::APT::Keep-Downloaded-Packages "true";' > /etc/apt/apt.conf.d/keep-cache

# Install common tools
RUN --mount=type=cache,target=/var/cache/apt,sharing=private \
    apt-get update && apt-get install -y \
    curl \
    git \
    sudo \
    usbutils \
    v4l-utils \
    htop \
    iputils-ping \
    wget \
    net-tools \
    tmux \
    vim \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Install Python pip
RUN --mount=type=cache,target=/var/cache/apt,sharing=private \
    apt-get update && apt-get install -y \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN --mount=type=cache,target=/var/cache/apt,sharing=private \
    apt-get update && apt-get install -y \
        ros-humble-ros-base \
        ros-humble-image-transport-plugins \
        ros-humble-cv-bridge \
        ros-humble-vision-msgs \
        ros-humble-pcl-ros \
        ros-humble-octomap-ros \
        ros-humble-octomap-msgs \
        ros-humble-rviz2 \
        ros-humble-tf2-geometry-msgs \
        ros-humble-tf2-eigen \
        ros-humble-rmw-cyclonedds-cpp \
    && rm -rf /var/lib/apt/lists/*

ARG USERNAME=vision
ARG USER_UID=1000

RUN --mount=type=cache,target=/var/cache/apt,sharing=private \
    apt-get update && apt-get install -y sudo \
    && useradd -m -s /bin/bash -u $USER_UID -G sudo,video,plugdev $USERNAME \
    && echo '%sudo ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers \
    && rm -rf /var/lib/apt/lists/*

USER $USERNAME
# Auto-source ROS Humble
RUN echo "source /opt/ros/humble/setup.bash" >> ~/.bashrc

# Auto-source your specific workspace (Recommended!)
RUN echo "source /home/vision/vision_ws/install/setup.bash" >> ~/.bashrc

COPY --chown=$USERNAME:$USERNAME \
     modules/install_realsense.sh /tmp/install_realsense.sh
RUN --mount=type=cache,target=/var/cache/apt,sharing=private \
    /tmp/install_realsense.sh && rm /tmp/install_realsense.sh

ENTRYPOINT []
CMD ["/bin/bash"]