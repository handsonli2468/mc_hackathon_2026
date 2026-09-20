#pragma once

#include <atomic>
#include <chrono>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <vector>

#include <geometry_msgs/msg/point_stamped.hpp>
#include <geometry_msgs/msg/pose_stamped.hpp>
#include <rclcpp/rclcpp.hpp>
#include <rclcpp_action/rclcpp_action.hpp>

#include "bt_engine/camera_client.hpp"

namespace bt_engine
{

// Shared by all BT leaves: the ROS node, cached clients, and failure notes
// that get reported back to the caller (e.g. the LLM) after a run.
class RosContext
{
public:
  struct Note
  {
    std::string node_path;
    std::string message;
  };

  explicit RosContext(rclcpp::Node::SharedPtr node)
  : node(std::move(node)) {}

  rclcpp::Node::SharedPtr node;
  int server_wait_ms = 1000;
  int service_timeout_ms = 2000;
  int goal_response_timeout_ms = 3000;

  // Tuning that used to be tree ports. These are engine business, not something the
  // LLM should reason about, so they live here and are live-settable, e.g.
  //   ros2 param set /bt_engine camera_poll_ms 250
  std::atomic<int> camera_poll_ms{500};           // IsObjectFound: how often to ask the camera
  std::atomic<int> object_pose_timeout_ms{15000};  // NavigateToDetectedObject: wait for a pose
  std::atomic<int> replan_min_interval_ms{400};   // NavigateToDetectedObject: re-send floor

  // `distance` presets for NavigateToObject / TrackObject (metres, robot front to
  // object centre). The names are the contract with the LLM; the values are engine
  // parameters and can be changed while running (ros2 param set /bt_engine nav_distance.near ...).
  std::vector<std::string> navDistanceNames() const
  {
    std::lock_guard<std::mutex> lk(distances_mtx_);
    std::vector<std::string> names;
    for (const auto & [name, _] : nav_distances_) {
      names.push_back(name);
    }
    return names;
  }

  std::string navDistanceList() const
  {
    std::string out;
    for (const auto & name : navDistanceNames()) {
      out += (out.empty() ? "" : ", ") + name;
    }
    return out;
  }

  // Metres for a preset name, or nullopt if there is no such preset.
  std::optional<double> navDistance(const std::string & name) const
  {
    std::lock_guard<std::mutex> lk(distances_mtx_);
    const auto it = nav_distances_.find(name);
    return it == nav_distances_.end() ? std::nullopt : std::optional<double>(it->second);
  }

  void setNavDistance(const std::string & name, double metres)
  {
    std::lock_guard<std::mutex> lk(distances_mtx_);
    nav_distances_[name] = metres;
  }

  // Named speed profiles for the navigation nodes, as a percentage of Nav2's configured
  // maximum. The names are the contract with the LLM; the numbers are engine parameters
  // and can be changed while running (ros2 param set /bt_engine speed_profile.slow 25.0).
  std::optional<double> speedProfile(const std::string & name) const
  {
    std::lock_guard<std::mutex> lk(speeds_mtx_);
    const auto it = speeds_.find(name);
    return it == speeds_.end() ? std::nullopt : std::optional<double>(it->second);
  }

  void setSpeedProfile(const std::string & name, double percent)
  {
    std::lock_guard<std::mutex> lk(speeds_mtx_);
    speeds_[name] = percent;
  }

  std::vector<std::string> speedProfileNames() const
  {
    std::lock_guard<std::mutex> lk(speeds_mtx_);
    std::vector<std::string> names;
    for (const auto & [name, _] : speeds_) {
      names.push_back(name);
    }
    return names;
  }

  std::string speedProfileList() const
  {
    std::string out;
    for (const auto & name : speedProfileNames()) {
      out += (out.empty() ? "" : ", ") + name;
    }
    return out;
  }

  template<class ActionT>
  std::shared_ptr<rclcpp_action::Client<ActionT>> actionClient(const std::string & server)
  {
    std::lock_guard<std::mutex> lk(clients_mtx_);
    auto & slot = clients_["action:" + server];
    if (!slot) {
      slot = rclcpp_action::create_client<ActionT>(node, server);
    }
    return std::static_pointer_cast<rclcpp_action::Client<ActionT>>(slot);
  }

  template<class SrvT>
  std::shared_ptr<rclcpp::Client<SrvT>> serviceClient(const std::string & server)
  {
    std::lock_guard<std::mutex> lk(clients_mtx_);
    auto & slot = clients_["srv:" + server];
    if (!slot) {
      slot = node->create_client<SrvT>(server);
    }
    return std::static_pointer_cast<rclcpp::Client<SrvT>>(slot);
  }

  // ---- camera service (the VLM team's HTTP API) ----
  // Settings live in ROS parameters so they can be changed while running.
  void setCameraConfig(const CameraConfig & config)
  {
    std::lock_guard<std::mutex> lk(camera_mtx_);
    camera_ = config;
  }

  CameraClient camera() const
  {
    std::lock_guard<std::mutex> lk(camera_mtx_);
    return CameraClient(camera_);
  }

  // ---- where the camera says the object is ----
  // Where the camera says the object is. Deliberately NOT /goal_pose: Nav2's bt_navigator
  // subscribes to that name and would drive off on its own, behind the tree's back.
  //
  // Two subscriptions, because the camera team publishes a PointStamped (a position, no
  // orientation) while a PoseStamped is the more usual shape. Only one of them will ever
  // receive anything - ROS 2 matches publishers to subscriptions by type - so whichever
  // they choose works without a change here. A point becomes a pose with no rotation,
  // which is all the approach needs: it works out its own heading from the robot's pose.
  void subscribeGoalPose(const std::string & topic)
  {
    std::lock_guard<std::mutex> lk(pose_mtx_);
    if (goal_pose_sub_) {
      return;
    }
    goal_pose_topic_ = topic;
    goal_pose_sub_ = node->create_subscription<geometry_msgs::msg::PoseStamped>(
      topic, rclcpp::QoS(1),
      [this](geometry_msgs::msg::PoseStamped::SharedPtr msg) {
        std::lock_guard<std::mutex> lk(pose_mtx_);
        latest_goal_pose_ = *msg;
        goal_pose_at_ = std::chrono::steady_clock::now();
      });
    goal_point_sub_ = node->create_subscription<geometry_msgs::msg::PointStamped>(
      topic, rclcpp::QoS(1),
      [this](geometry_msgs::msg::PointStamped::SharedPtr msg) {
        geometry_msgs::msg::PoseStamped pose;
        pose.header = msg->header;
        pose.pose.position = msg->point;
        pose.pose.orientation.w = 1.0;
        std::lock_guard<std::mutex> lk(pose_mtx_);
        latest_goal_pose_ = pose;
        goal_pose_at_ = std::chrono::steady_clock::now();
      });
  }

  // The robot's own pose, from the global camera. Used to work out where to stop
  // short of an object rather than driving on top of it.
  void subscribeRobotPose(const std::string & topic)
  {
    std::lock_guard<std::mutex> lk(pose_mtx_);
    if (robot_pose_sub_) {
      return;
    }
    robot_pose_topic_ = topic;
    robot_pose_sub_ = node->create_subscription<geometry_msgs::msg::PoseStamped>(
      topic, rclcpp::QoS(1),
      [this](geometry_msgs::msg::PoseStamped::SharedPtr msg) {
        std::lock_guard<std::mutex> lk(pose_mtx_);
        latest_robot_pose_ = *msg;
      });
  }

  std::string robotPoseTopic() const
  {
    std::lock_guard<std::mutex> lk(pose_mtx_);
    return robot_pose_topic_;
  }

  std::optional<geometry_msgs::msg::PoseStamped> latestRobotPose() const
  {
    std::lock_guard<std::mutex> lk(pose_mtx_);
    return latest_robot_pose_;
  }

  struct GoalPose
  {
    geometry_msgs::msg::PoseStamped pose;
    std::chrono::steady_clock::time_point received_at;
  };

  std::optional<GoalPose> latestGoalPose() const
  {
    std::lock_guard<std::mutex> lk(pose_mtx_);
    if (!latest_goal_pose_) {
      return std::nullopt;
    }
    return GoalPose{*latest_goal_pose_, goal_pose_at_};
  }

  // The frame the navigation nodes work in. A pose in any other frame is refused rather
  // than driven to: the camera's own frame has the object 30 cm away and the map frame
  // has it metres away, and the two are indistinguishable once the numbers are copied out.
  std::string mapFrame() const
  {
    std::lock_guard<std::mutex> lk(pose_mtx_);
    return map_frame_;
  }

  void setMapFrame(const std::string & frame)
  {
    std::lock_guard<std::mutex> lk(pose_mtx_);
    map_frame_ = frame;
  }

  std::string goalPoseTopic() const
  {
    std::lock_guard<std::mutex> lk(pose_mtx_);
    return goal_pose_topic_;
  }

  void note(const std::string & node_path, const std::string & message)
  {
    RCLCPP_WARN(node->get_logger(), "[%s] %s", node_path.c_str(), message.c_str());
    std::lock_guard<std::mutex> lk(notes_mtx_);
    notes_.push_back({node_path, message});
  }

  std::vector<Note> takeNotes()
  {
    std::lock_guard<std::mutex> lk(notes_mtx_);
    return std::exchange(notes_, {});
  }

private:
  mutable std::mutex camera_mtx_;
  CameraConfig camera_;
  mutable std::mutex pose_mtx_;
  std::string goal_pose_topic_;
  std::string robot_pose_topic_;
  rclcpp::Subscription<geometry_msgs::msg::PoseStamped>::SharedPtr goal_pose_sub_;
  rclcpp::Subscription<geometry_msgs::msg::PointStamped>::SharedPtr goal_point_sub_;
  std::string map_frame_ = "map";
  std::optional<geometry_msgs::msg::PoseStamped> latest_goal_pose_;
  rclcpp::Subscription<geometry_msgs::msg::PoseStamped>::SharedPtr robot_pose_sub_;
  std::optional<geometry_msgs::msg::PoseStamped> latest_robot_pose_;
  std::chrono::steady_clock::time_point goal_pose_at_;
  mutable std::mutex speeds_mtx_;
  std::map<std::string, double> speeds_{{"slow", 30.0}, {"normal", 70.0}, {"fast", 100.0}};
  mutable std::mutex distances_mtx_;
  std::map<std::string, double> nav_distances_{{"contact", 0.01}, {"near", 0.05}, {"far", 0.10}};
  std::mutex clients_mtx_;
  std::map<std::string, std::shared_ptr<void>> clients_;
  std::mutex notes_mtx_;
  std::vector<Note> notes_;
};

}  // namespace bt_engine
