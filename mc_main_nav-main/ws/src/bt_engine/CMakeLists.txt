cmake_minimum_required(VERSION 3.8)
project(bt_engine)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
add_compile_options(-Wall -Wextra)

find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(rclcpp_action REQUIRED)
find_package(behaviortree_cpp REQUIRED)
find_package(robot_interfaces REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(ament_index_cpp REQUIRED)
find_package(nlohmann_json REQUIRED)
find_package(Threads REQUIRED)
find_package(OpenSSL REQUIRED)   # https:// to the camera service
find_package(tinyxml2_vendor REQUIRED)
find_package(TinyXML2 REQUIRED)  # filtering the node palette XML

add_executable(bt_engine
  src/main.cpp
  src/register_nodes.cpp
  src/run_manager.cpp
  src/http_api.cpp
)
target_include_directories(bt_engine PRIVATE include)
target_include_directories(bt_engine SYSTEM PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/third_party)
ament_target_dependencies(bt_engine rclcpp rclcpp_action behaviortree_cpp robot_interfaces geometry_msgs ament_index_cpp)
target_compile_definitions(bt_engine PRIVATE CPPHTTPLIB_OPENSSL_SUPPORT)
target_link_libraries(bt_engine nlohmann_json::nlohmann_json Threads::Threads OpenSSL::SSL OpenSSL::Crypto tinyxml2::tinyxml2)

install(TARGETS bt_engine DESTINATION lib/${PROJECT_NAME})
install(DIRECTORY web DESTINATION share/${PROJECT_NAME})
install(DIRECTORY places DESTINATION share/${PROJECT_NAME})

ament_package()
