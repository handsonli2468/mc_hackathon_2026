# ROS 2 Robot Localization

Dockerized ROS 2 Humble localization for a mobile robot. One container runs a
timestamp bridge and two `robot_localization` extended Kalman filters (EKFs):

```text
                         +------------------------------+
                         | robot-localization container |
                         |                              |
/wheel/odom ------------>| timestamp bridge             |
                         |        |                     |
                         |        v                     |
                         | /wheel/odom_stamped           |
                         |        |                     |
                         |        +--> odom EKF --------+--> /odometry/local
                         |        |    (world: odom)     |
                         |        |                     |
/pose/global ------------|--------+--> map EKF ---------+--> /odometry/global
                         |             (world: map)      |
                         +------------------------------+
```

The image is based on `ros:humble-ros-core-jammy` and installs only the ROS
packages required by this stack. The Compose service uses host networking and
host IPC so it can participate directly in ROS 2 DDS discovery on the host.

## What runs

| Node | Input topics | Output topic | Purpose |
| --- | --- | --- | --- |
| `/mcu_odom_timestamp_bridge` | `/wheel/odom` | `/wheel/odom_stamped` | Replaces the MCU message timestamp with the current ROS clock |
| `/ekf_filter_node_odom` | `/wheel/odom_stamped` | `/odometry/local` | Produces local odometry in the `odom` world frame |
| `/ekf_filter_node_map` | `/wheel/odom_stamped`, `/pose/global` | `/odometry/global` | Produces globally corrected odometry in the `map` world frame |

The bridge changes only `header.stamp`. The MCU publisher must provide valid
`header.frame_id`, `child_frame_id`, pose, twist, and covariance values.

## Requirements

- A Linux host with Docker Engine and Docker Compose
- Access to Docker (for example, membership in the `docker` group)
- A ROS 2 odometry publisher on `/wheel/odom`
- A global pose publisher on `/pose/global` if global corrections are required

The image is suitable for Docker hosts that can run the upstream ROS 2 Humble
image, including common `amd64` and `arm64` systems.

## Configuration

Runtime settings are stored in `.env`:

| Variable | Default | Description |
| --- | --- | --- |
| `COMPOSE_PROJECT_NAME` | `robot-localization` | Compose project name |
| `ROS_DOMAIN_ID` | `59` | ROS 2 DDS domain; all communicating ROS devices must match |
| `USE_SIM_TIME` | `false` | Use the `/clock` topic instead of the system clock |

Filter inputs, frames, update rates, and covariance matrices are configured in
[`config/ekf.yaml`](config/ekf.yaml). The file is mounted read-only at
`/config/ekf.yaml` in the container.

## Start

Run all commands from this project directory:

```bash
docker compose --env-file .env -f docker/compose.yaml config --quiet
docker compose --env-file .env -f docker/compose.yaml up --build -d
```

Follow the logs:

```bash
docker compose --env-file .env -f docker/compose.yaml logs -f robot-localization
```

All three processes are supervised in the same container. If one exits, the
others are stopped and Compose restarts the service.

## Verify

List the running nodes:

```bash
docker compose --env-file .env -f docker/compose.yaml exec -T robot-localization \
  bash -lc 'source /opt/ros/humble/setup.bash && ros2 node list'
```

Expected nodes include:

```text
/mcu_odom_timestamp_bridge
/ekf_filter_node_map
/ekf_filter_node_odom
```

Check that odometry reaches the bridge and both filters:

```bash
docker compose --env-file .env -f docker/compose.yaml exec -T robot-localization \
  bash -lc 'source /opt/ros/humble/setup.bash && ros2 topic echo --once /wheel/odom_stamped'

docker compose --env-file .env -f docker/compose.yaml exec -T robot-localization \
  bash -lc 'source /opt/ros/humble/setup.bash && ros2 topic hz /odometry/local'

docker compose --env-file .env -f docker/compose.yaml exec -T robot-localization \
  bash -lc 'source /opt/ros/humble/setup.bash && ros2 topic hz /odometry/global'
```

Press `Ctrl+C` to stop either `ros2 topic hz` command.

## Stop or rebuild

```bash
# Stop and remove the container.
docker compose --env-file .env -f docker/compose.yaml down

# Rebuild after changing the Dockerfile or bridge node.
docker compose --env-file .env -f docker/compose.yaml up --build -d
```

Changes to `config/ekf.yaml` require a service restart:

```bash
docker compose --env-file .env -f docker/compose.yaml restart robot-localization
```

## Troubleshooting

### Nodes or topics are not discovered

- Confirm every ROS 2 participant uses the same `ROS_DOMAIN_ID`.
- Confirm the host firewall permits DDS traffic.
- This Compose file uses `network_mode: host`; run it on a Linux Docker host
  for the expected discovery behavior.

### The EKF reports missing or stale data

- Check that `/wheel/odom` is publishing and that the bridge produces
  `/wheel/odom_stamped`.
- Ensure the odometry frame IDs and covariance values are valid.
- If `USE_SIM_TIME=true`, ensure a `/clock` publisher is running.
- The global EKF also expects `/pose/global`; local odometry can run without it.

### Transform warnings appear

The configured frame chain is `map -> odom -> base_link`. Ensure other nodes do
not publish conflicting transforms and that sensor messages use frame names
consistent with `config/ekf.yaml`.

## Project layout

```text
.
├── .env
├── config/ekf.yaml
├── docker/
│   ├── Dockerfile
│   └── compose.yaml
└── nodes/mcu_odom_timestamp_bridge.py
```
