#!/bin/bash

source install/setup.bash
ros2 run micro_ros_agent micro_ros_agent ${AGENT_TYPE} -b ${AGENT_BAUD_RATE} --dev ${AGENT_DEVICE}

# sleep 1000
exec "$@"
#!/bin/bash