cmake_minimum_required(VERSION 3.8)
project(robot_interfaces)

find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(action_msgs REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(std_msgs REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/ObjectDetection.msg"
  "action/NavigateToObject.action"
  "action/NavigateToPoint.action"
  "action/VisualizeObject.action"
  "action/TrackObject.action"
  "action/GraspObject.action"
  "action/RotateInPlace.action"
  "action/SetGripper.action"
  "srv/ObjectQuery.srv"
  "srv/GetObjectPose.srv"
  DEPENDENCIES action_msgs geometry_msgs std_msgs
)

ament_export_dependencies(rosidl_default_runtime)
ament_package()
