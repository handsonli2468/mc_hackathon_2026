cmake_minimum_required(VERSION 3.8)
project(aruco_test)

find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(cv_bridge REQUIRED)
find_package(OpenCV REQUIRED)
find_package(tf2 REQUIRED)
find_package(tf2_ros REQUIRED)
find_package(tf2_geometry_msgs REQUIRED)
find_package(tf2_eigen REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(std_msgs REQUIRED)
find_package(visualization_msgs REQUIRED)


include_directories(
  include
  ${OpenCV_INCLUDE_DIRS}
)

add_executable(homography_aruco_node
  src/homography_aruco.cpp
)

add_executable(homography_sima_node
  src/homography_sima.cpp
)

add_executable(homography_all_node
  src/homography_all.cpp
)

add_executable(homography_duck_node
  src/homography_duck.cpp
)

add_executable(pnp_duck_node
  src/pnp_duck.cpp
)

ament_target_dependencies(homography_aruco_node
  rclcpp
  sensor_msgs
  std_msgs
  cv_bridge
  tf2_ros
  tf2_geometry_msgs
  tf2_eigen
  geometry_msgs
  visualization_msgs
)

ament_target_dependencies(homography_sima_node
  rclcpp
  sensor_msgs
  std_msgs
  cv_bridge
  tf2_ros
  tf2_geometry_msgs
  tf2_eigen
  geometry_msgs
  visualization_msgs
)

ament_target_dependencies(homography_all_node
  rclcpp
  sensor_msgs
  std_msgs
  cv_bridge
  tf2_ros
  tf2_geometry_msgs
  tf2_eigen
  geometry_msgs
  visualization_msgs
)

ament_target_dependencies(homography_duck_node
  rclcpp
  sensor_msgs
  std_msgs
  cv_bridge
  tf2_ros
  tf2_geometry_msgs
  tf2_eigen
  geometry_msgs
  visualization_msgs
)

ament_target_dependencies(pnp_duck_node
  rclcpp
  sensor_msgs
  std_msgs
  cv_bridge
  tf2_ros
  tf2_geometry_msgs
  tf2_eigen
  geometry_msgs
  visualization_msgs
)

target_link_libraries(homography_aruco_node
  ${OpenCV_LIBRARIES}
)

target_link_libraries(homography_sima_node
  ${OpenCV_LIBRARIES}
)

target_link_libraries(homography_all_node
  ${OpenCV_LIBRARIES}
)

target_link_libraries(homography_duck_node
  ${OpenCV_LIBRARIES}
)

target_link_libraries(pnp_duck_node
  ${OpenCV_LIBRARIES}
)

install(TARGETS
  homography_aruco_node
  homography_sima_node
  homography_all_node
  homography_duck_node
  pnp_duck_node
  DESTINATION lib/${PROJECT_NAME}
)

# Install headers
# install(DIRECTORY include/
#   DESTINATION include
# )

# Install launch and config
install(DIRECTORY launch config
  DESTINATION share/${PROJECT_NAME}
)

ament_package()
