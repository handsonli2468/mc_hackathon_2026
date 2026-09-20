# ROS 2 Humble micro-ROS Agent

Docker Compose deployment of a micro-ROS Agent for connecting a serial
microcontroller to the ROS 2 graph. The image installs ROS 2 Humble on Ubuntu
22.04, builds the Humble micro-ROS Agent from source, and runs it with host
networking.

```text
micro-ROS device                     Docker host / ROS 2 network
      |                                          |
      | USB serial                               |
      v                                          v
/dev/ttyACM0 --> micro-ROS Agent --> DDS / ROS_DOMAIN_ID --> ROS 2 nodes
```

## Requirements

- A Linux host with Docker Engine and Docker Compose
- Internet access while building the image
- A connected micro-ROS device with a serial transport
- The device path on the host, such as `/dev/ttyACM0` or `/dev/ttyUSB0`

The source build may take several minutes, especially on an ARM single-board
computer.

## Configuration

Edit `.env` before starting the service:

| Variable | Default | Description |
| --- | --- | --- |
| `ROS_DOMAIN_ID` | `0` | ROS 2 DDS domain; it must match the rest of the ROS graph |
| `AGENT_TYPE` | `serial` | micro-ROS Agent transport used by the startup script |
| `AGENT_BAUD_RATE` | `115200` | Serial baud rate; it must match the client firmware |
| `AGENT_DEVICE` | `/dev/ttyACM0` | Serial device path on the Docker host |

The current startup script is designed for the serial transport and always
passes `--dev` and `-b` arguments. Additional transports require adapting
`script/uros-agent-start.sh` and the Compose device/network configuration.

`ROS_DISTRO=humble` also appears in `.env`, but the Dockerfile currently fixes
the image and source branch to ROS 2 Humble; changing that value alone does not
select another ROS distribution.

## Check the serial device

Connect the microcontroller and identify its device path:

```bash
ls -l /dev/ttyACM* /dev/ttyUSB* 2>/dev/null
```

Update `AGENT_DEVICE` in `.env` if necessary. The current Compose deployment
mounts `/dev` and runs the container as privileged, so the selected device is
visible inside the container. See the security note below before using this
configuration outside a development robot.

## Build and start

Run all commands from this project directory:

```bash
docker compose --env-file .env -f docker-compose.yaml config --quiet
docker compose --env-file .env -f docker-compose.yaml up --build -d
```

Watch the Agent output:

```bash
docker compose --env-file .env -f docker-compose.yaml logs -f uros-agent
```

When the microcontroller connects, the log should show a new client/session and
the creation of its ROS entities.

## Verify

Confirm that the container is running:

```bash
docker compose --env-file .env -f docker-compose.yaml ps
```

Inspect the ROS graph from inside the container:

```bash
docker compose --env-file .env -f docker-compose.yaml exec -T uros-agent \
  bash -lc 'source /opt/ros/humble/setup.bash && source /root/uros-agent-ws/install/setup.bash && ros2 node list'

docker compose --env-file .env -f docker-compose.yaml exec -T uros-agent \
  bash -lc 'source /opt/ros/humble/setup.bash && source /root/uros-agent-ws/install/setup.bash && ros2 topic list'
```

Client nodes and topics appear only after the micro-ROS firmware establishes a
session with the Agent.

## Stop or rebuild

```bash
# Stop and remove the container.
docker compose --env-file .env -f docker-compose.yaml down

# Rebuild the source workspace without using cached image layers.
docker compose --env-file .env -f docker-compose.yaml build --no-cache uros-agent
docker compose --env-file .env -f docker-compose.yaml up -d
```

## Build process

The multi-stage Dockerfile performs these steps:

1. Installs ROS 2 Humble and build tools on Ubuntu 22.04.
2. Clones the `humble` branch of `micro_ros_setup`.
3. Resolves ROS dependencies and builds `micro_ros_setup`.
4. Creates and builds the Agent workspace from source.
5. Copies the built install tree into the deployment stage.

The runtime entrypoint sources the built workspace and starts:

```text
ros2 run micro_ros_agent micro_ros_agent serial -b <baud> --dev <device>
```

## Troubleshooting

### The serial device does not exist

- Reconnect the board and check `dmesg` or `/dev/ttyACM*` and `/dev/ttyUSB*`.
- Update `AGENT_DEVICE` in `.env`, then recreate the container.
- If the device name changes frequently, create a stable udev symlink on the
  host and use that path instead.

### The Agent starts but no client appears

- Confirm the firmware uses the serial micro-ROS transport.
- Confirm `AGENT_BAUD_RATE` matches the firmware configuration.
- Confirm both sides use compatible ROS 2 and micro-ROS distributions.
- Restart the microcontroller after the Agent is listening.

### ROS 2 nodes on the host cannot see the client

- Set the same `ROS_DOMAIN_ID` on the host and in `.env`.
- Check that the host firewall permits DDS traffic.
- The service uses `network_mode: host`, which is intended for Linux hosts.

### The image fails while resolving or building dependencies

The build clones repositories and runs `rosdep`, so it needs working DNS,
internet access, and the Ubuntu/ROS package repositories. Retry with the
no-cache rebuild commands above after connectivity is restored.

## Security note

`docker-compose.yaml` currently uses `privileged: true` and mounts all of
`/dev`. This is convenient during hardware development but grants broad host
device access. For production, replace the `/dev:/dev` mount with a specific
device mapping and remove privileged mode after verifying the required serial
permissions.

## Project layout

```text
.
├── .env
├── Dockerfile.uros_agent
├── docker-compose.yaml
└── script/
    ├── install-ros2.sh
    ├── install-uros-agent.sh
    └── uros-agent-start.sh
```

`script/entrypoint.sh` is not referenced by the current Dockerfile or Compose
service; it belongs to a separate `SIMA-ws` launch workflow.
