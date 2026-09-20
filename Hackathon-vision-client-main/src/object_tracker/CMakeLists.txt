cmake_minimum_required(VERSION 3.8)
project(object_tracker)

if(NOT CMAKE_CXX_STANDARD)
  set(CMAKE_CXX_STANDARD 17)
endif()

find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(cv_bridge REQUIRED)
find_package(image_transport REQUIRED)
find_package(message_filters REQUIRED)
find_package(tf2 REQUIRED)
find_package(tf2_ros REQUIRED)
find_package(tf2_geometry_msgs REQUIRED)
find_package(ament_index_cpp REQUIRED)
find_package(OpenCV REQUIRED COMPONENTS core imgproc features2d calib3d video highgui imgcodecs)

include_directories(
  ${OpenCV_INCLUDE_DIRS}
)

add_executable(orb_tracker_node
  src/orb_tracker_node.cpp
)

ament_target_dependencies(orb_tracker_node
  rclcpp
  image_transport
  sensor_msgs
  geometry_msgs
  cv_bridge
  message_filters
  tf2
  tf2_ros
  tf2_geometry_msgs
  ament_index_cpp
)

target_link_libraries(orb_tracker_node
  ${OpenCV_LIBRARIES}
)

add_executable(frame_capture_node
  src/frame_capture_node.cpp
)

ament_target_dependencies(frame_capture_node
  rclcpp
  sensor_msgs
  cv_bridge
)

target_link_libraries(frame_capture_node
  ${OpenCV_LIBRARIES}
)

install(TARGETS
  orb_tracker_node
  frame_capture_node
  DESTINATION lib/${PROJECT_NAME}
)

# Development tool: simulates upstream VLM masks
add_executable(mask_init_tool
  src/mask_init_tool.cpp
)

ament_target_dependencies(mask_init_tool
  rclcpp
  sensor_msgs
  cv_bridge
)

target_link_libraries(mask_init_tool
  ${OpenCV_LIBRARIES}
)

install(TARGETS
  mask_init_tool
  DESTINATION lib/${PROJECT_NAME}
)

# Mask-initialized tracker for non-planar targets (KLT + similarity + depth gating)
add_executable(mask_tracker_node
  src/mask_tracker_node.cpp
)

target_include_directories(mask_tracker_node PRIVATE
  include
)

ament_target_dependencies(mask_tracker_node
  rclcpp
  image_transport
  sensor_msgs
  geometry_msgs
  cv_bridge
  message_filters
  tf2
  tf2_ros
  tf2_geometry_msgs
)

target_link_libraries(mask_tracker_node
  ${OpenCV_LIBRARIES}
)

install(TARGETS
  mask_tracker_node
  DESTINATION lib/${PROJECT_NAME}
)

# VLM bridge: ZeroMQ client that turns server masks into mask_topic messages
find_package(PkgConfig REQUIRED)
pkg_check_modules(ZMQ REQUIRED libzmq)
find_package(nlohmann_json REQUIRED)

add_executable(vlm_bridge_node
  src/vlm_bridge_node.cpp
)

ament_target_dependencies(vlm_bridge_node
  rclcpp
  sensor_msgs
  cv_bridge
)

target_include_directories(vlm_bridge_node PRIVATE
  include
  ${ZMQ_INCLUDE_DIRS}
)

target_link_libraries(vlm_bridge_node
  ${OpenCV_LIBRARIES}
  ${ZMQ_LIBRARIES}
  nlohmann_json::nlohmann_json
)

# mask_tracker_node with the VLM client built in: the color image is subscribed only once
add_executable(mask_tracker_vlm_node
  src/mask_tracker_vlm_node.cpp
)

target_include_directories(mask_tracker_vlm_node PRIVATE
  include
  ${ZMQ_INCLUDE_DIRS}
)

ament_target_dependencies(mask_tracker_vlm_node
  rclcpp
  image_transport
  sensor_msgs
  geometry_msgs
  cv_bridge
  message_filters
  tf2
  tf2_ros
  tf2_geometry_msgs
)

target_link_libraries(mask_tracker_vlm_node
  ${OpenCV_LIBRARIES}
  ${ZMQ_LIBRARIES}
  nlohmann_json::nlohmann_json
)

install(TARGETS
  vlm_bridge_node
  mask_tracker_vlm_node
  DESTINATION lib/${PROJECT_NAME}
)

# Mock VLM server for development (docs/vlm_transport.md section 9)
install(PROGRAMS
  scripts/mock_vlm_server.py
  DESTINATION lib/${PROJECT_NAME}
)

# Install launch, config and target images
install(DIRECTORY launch config targets
  DESTINATION share/${PROJECT_NAME}
)

ament_package()
