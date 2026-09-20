##############################################################################
# mc_main_nav images (ROS 2 Humble). One Dockerfile, three stages:
#
#   base   ROS 2 Humble, CycloneDDS, colcon, user `main`
#   robot  base + Nav2                          -> robot Raspberry Pi 5 (arm64)
#   pc     robot + BehaviorTree.CPP v4 + RViz   -> mini PC / dev PC (x86)   [default]
#
#   docker build -f docker/Dockerfile --target robot -t mc-main-nav:robot docker
#   docker build -f docker/Dockerfile --target pc    -t mc-main-nav:pc    docker
#
# Every stage builds natively on amd64 and arm64. Cross-build the Pi image on a PC
# (needs QEMU binfmt registered):
#   docker buildx build --platform linux/arm64 --target robot -t <repo>:robot-arm64 docker
#
# Slimmed from DIT-ROBOTICS/Eurobot-2026-Main docker/Dockerfile.
##############################################################################

ARG ROS_DISTRO=humble

# ---------------------------------------------------------------------------- base
FROM ros:${ROS_DISTRO}-ros-base-jammy AS base

ARG ROS_DISTRO
ARG USERNAME=main
ARG USER_UID=1000
ARG USER_GID=1000

ENV USERNAME=${USERNAME} \
    SHELL=/bin/bash \
    TERM=xterm-256color \
    LC_ALL=en_US.UTF-8 \
    LANG=en_US.UTF-8 \
    ROS_DISTRO=${ROS_DISTRO} \
    RMW_IMPLEMENTATION=rmw_cyclonedds_cpp

RUN rm -f /etc/apt/apt.conf.d/docker-clean; echo 'Binary::apt::APT::Keep-Downloaded-Packages "true";' > /etc/apt/apt.conf.d/keep-cache

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    sudo vim curl git tmux htop locales tzdata \
    build-essential cmake python3-pip bash-completion \
    python3-colcon-common-extensions \
    ros-${ROS_DISTRO}-rmw-cyclonedds-cpp \
    && echo "Keep cache"

RUN echo "en_US.UTF-8 UTF-8" > /etc/locale.gen && locale-gen en_US.UTF-8
RUN ln -snf /usr/share/zoneinfo/Asia/Taipei /etc/localtime && echo Asia/Taipei > /etc/timezone

RUN groupadd --gid ${USER_GID} ${USERNAME} \
    && useradd --uid ${USER_UID} --gid ${USER_GID} -m ${USERNAME} -s /bin/bash \
    && echo "${USERNAME} ALL=(root) NOPASSWD:ALL" > /etc/sudoers.d/${USERNAME}

USER ${USERNAME}
WORKDIR /home/${USERNAME}/mc_main_nav
RUN echo "source /opt/ros/${ROS_DISTRO}/setup.bash" >> ~/.bashrc \
    && echo '[ -f ~/mc_main_nav/ws/install/setup.bash ] && source ~/mc_main_nav/ws/install/setup.bash' >> ~/.bashrc \
    && echo "set -g mouse on" > ~/.tmux.conf

CMD ["/bin/bash"]

# ---------------------------------------------------------------------------- robot
# Robot Pi: pose_bridge + Nav2 + diff_nav (Python). Workspace build there:
#   ./scripts/build.sh --packages-select robot_interfaces diff_nav
FROM base AS robot

USER root
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    ros-${ROS_DISTRO}-navigation2 \
    ros-${ROS_DISTRO}-nav2-bringup \
    ros-${ROS_DISTRO}-tf2-ros \
    && echo "Keep cache"
USER ${USERNAME}

# ---------------------------------------------------------------------------- pc
# Mini PC / dev PC: bt_engine (C++), bt_mocks, table_map, simulator, RViz.
FROM robot AS pc

USER root
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    nlohmann-json3-dev \
    ros-${ROS_DISTRO}-behaviortree-cpp \
    ros-${ROS_DISTRO}-rclcpp-action \
    ros-${ROS_DISTRO}-rviz2 \
    ros-${ROS_DISTRO}-nav2-rviz-plugins \
    libgl1-mesa-dri \
    && echo "Keep cache"
USER ${USERNAME}
