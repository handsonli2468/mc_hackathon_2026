#include <thread>

#include <ament_index_cpp/get_package_share_directory.hpp>
#include <behaviortree_cpp/bt_factory.h>
#include <rclcpp/rclcpp.hpp>

#include "bt_engine/http_api.hpp"
#include "bt_engine/register_nodes.hpp"
#include "bt_engine/run_manager.hpp"

int main(int argc, char ** argv)
{
  rclcpp::init(argc, argv);
  auto node = std::make_shared<rclcpp::Node>("bt_engine");

  const auto host = node->declare_parameter<std::string>("http_host", "0.0.0.0");
  const auto port = node->declare_parameter<int>("http_port", 8080);

  auto ctx = std::make_shared<bt_engine::RosContext>(node);
  ctx->server_wait_ms = node->declare_parameter<int>("server_wait_ms", 1000);
  ctx->service_timeout_ms = node->declare_parameter<int>("service_timeout_ms", 2000);
  ctx->goal_response_timeout_ms = node->declare_parameter<int>("goal_response_timeout_ms", 3000);
  ctx->camera_poll_ms = node->declare_parameter<int>("camera_poll_ms", 500);
  ctx->object_pose_timeout_ms = node->declare_parameter<int>("object_pose_timeout_ms", 15000);
  ctx->replan_min_interval_ms = node->declare_parameter<int>("replan_min_interval_ms", 400);
  for (const auto & name : ctx->navDistanceNames()) {
    ctx->setNavDistance(
      name, node->declare_parameter<double>("nav_distance." + name, *ctx->navDistance(name)));
  }
  for (const auto & name : ctx->speedProfileNames()) {
    ctx->setSpeedProfile(
      name, node->declare_parameter<double>("speed_profile." + name, *ctx->speedProfile(name)));
  }
  // Let the presets be re-tuned while running, e.g.
  //   ros2 param set /bt_engine nav_distance.near 0.06
  //   ros2 param set /bt_engine speed_profile.slow 25.0
  auto distance_cb = node->add_on_set_parameters_callback(
    [ctx, node](const std::vector<rclcpp::Parameter> & params) {
      rcl_interfaces::msg::SetParametersResult result;
      result.successful = true;
      for (const auto & p : params) {
        // Timings that used to be tree ports; a non-positive value would busy-loop or
        // never time out, so refuse it rather than wedge a running tree.
        if (p.get_name() == "camera_poll_ms" || p.get_name() == "object_pose_timeout_ms" ||
          p.get_name() == "replan_min_interval_ms")
        {
          if (p.as_int() <= 0) {
            result.successful = false;
            result.reason = p.get_name() + " must be a positive number of milliseconds";
            return result;
          }
          if (p.get_name() == "camera_poll_ms") {ctx->camera_poll_ms = p.as_int();}
          else if (p.get_name() == "object_pose_timeout_ms") {
            ctx->object_pose_timeout_ms = p.as_int();
          } else {ctx->replan_min_interval_ms = p.as_int();}
          RCLCPP_INFO(
            node->get_logger(), "%s is now %ld ms", p.get_name().c_str(), p.as_int());
          continue;
        }
        if (p.get_name().rfind("speed_profile.", 0) == 0) {
          const auto name = p.get_name().substr(std::string("speed_profile.").size());
          if (p.as_double() <= 0.0 || p.as_double() > 100.0) {
            result.successful = false;
            result.reason = "speed profiles are a percentage of the maximum, 0 < x <= 100";
            return result;
          }
          ctx->setSpeedProfile(name, p.as_double());
          RCLCPP_INFO(
            node->get_logger(), "speed '%s' is now %.0f%%", name.c_str(), p.as_double());
          continue;
        }
        if (p.get_name().rfind("nav_distance.", 0) != 0) {
          continue;
        }
        const auto name = p.get_name().substr(std::string("nav_distance.").size());
        if (p.as_double() <= 0.0) {
          result.successful = false;
          result.reason = "distance presets must be positive metres";
          return result;
        }
        ctx->setNavDistance(name, p.as_double());
        RCLCPP_INFO(
          node->get_logger(), "distance '%s' is now %.3f m", name.c_str(), p.as_double());
      }
      return result;
    });

  // The camera team's HTTP service: VisualizeObject starts a search there, TrackWhenFound
  // polls it. All of it is re-settable at runtime, since their endpoints are theirs to pick.
  bt_engine::CameraConfig cam;
  cam.base_url = node->declare_parameter<std::string>("camera.base_url", "");
  cam.start_path = node->declare_parameter<std::string>("camera.start_path", "/api/query");
  cam.start_field = node->declare_parameter<std::string>("camera.start_field", "text");
  cam.status_path = node->declare_parameter<std::string>("camera.status_path", "/api/status");
  cam.status_query_param = node->declare_parameter<std::string>("camera.status_query_param", "");
  cam.max_age_s = node->declare_parameter<double>("camera.max_age_s", 8.0);
  cam.token = node->declare_parameter<std::string>("camera.token", "");
  cam.timeout_ms = node->declare_parameter<int>("camera.timeout_ms", 3000);
  ctx->setCameraConfig(cam);
  if (cam.base_url.empty()) {
    RCLCPP_WARN(
      node->get_logger(),
      "camera.base_url is not set: VisualizeObject and TrackWhenFound will fail until it is "
      "(ros2 param set /bt_engine camera.base_url http://<host>:<port>)");
  }

  // Where the camera publishes the object's pose. NOT /goal_pose: Nav2's bt_navigator
  // subscribes to that and would drive there by itself, behind the tree's back.
  ctx->subscribeGoalPose(
    node->declare_parameter<std::string>("camera.goal_pose_topic", "/object_goal_pose"));
  ctx->subscribeRobotPose(
    node->declare_parameter<std::string>("robot_pose_topic", "/robot_pose"));
  ctx->setMapFrame(node->declare_parameter<std::string>("map_frame", "map"));

  auto camera_cb = node->add_on_set_parameters_callback(
    [ctx, node](const std::vector<rclcpp::Parameter> & params) {
      rcl_interfaces::msg::SetParametersResult result;
      result.successful = true;
      auto cfg = ctx->camera().config();
      bool changed = false;
      for (const auto & p : params) {
        const auto & n = p.get_name();
        if (n == "camera.base_url") {cfg.base_url = p.as_string(); changed = true;}
        else if (n == "camera.start_path") {cfg.start_path = p.as_string(); changed = true;}
        else if (n == "camera.start_field") {cfg.start_field = p.as_string(); changed = true;}
        else if (n == "camera.status_path") {cfg.status_path = p.as_string(); changed = true;}
        else if (n == "camera.status_query_param") {
          cfg.status_query_param = p.as_string(); changed = true;
        }
        else if (n == "camera.max_age_s") {cfg.max_age_s = p.as_double(); changed = true;}
        else if (n == "camera.token") {cfg.token = p.as_string(); changed = true;}
        else if (n == "camera.timeout_ms") {cfg.timeout_ms = p.as_int(); changed = true;}
      }
      if (changed) {
        ctx->setCameraConfig(cfg);
        RCLCPP_INFO(node->get_logger(), "camera service is now %s", cfg.base_url.c_str());
      }
      return result;
    });

  bt_engine::RunOptions opts;
  opts.tick_ms = node->declare_parameter<int>("tick_ms", 50);
  opts.groot_port = node->declare_parameter<int>("groot_port", 1667);

  rclcpp::executors::MultiThreadedExecutor executor;
  executor.add_node(node);
  std::thread spin_thread([&executor] {executor.spin();});

  BT::BehaviorTreeFactory factory;
  bt_engine::registerRobotNodes(factory, ctx);

  // Named places, packaged so a generated tree can call `<SubTree ID="GoHome"/>` without
  // defining it or knowing any coordinates. Set places_file to "" to turn this off.
  const auto places_file = node->declare_parameter<std::string>(
    "places_file",
    ament_index_cpp::get_package_share_directory("bt_engine") + "/places/places.xml");
  if (!places_file.empty()) {
    try {
      factory.registerBehaviorTreeFromFile(places_file);
      std::string ids;
      for (const auto & id : factory.registeredBehaviorTrees()) {
        ids += (ids.empty() ? "" : ", ") + id;
      }
      RCLCPP_INFO(node->get_logger(), "places registered: %s", ids.c_str());
    } catch (const std::exception & e) {
      // A broken places file must not stop the engine serving trees that do not use it.
      RCLCPP_ERROR(
        node->get_logger(), "could not load places from '%s': %s", places_file.c_str(), e.what());
    }
  }

  opts.places_file = places_file;
  bt_engine::RunManager runs(factory, ctx, opts);
  httplib::Server svr;
  const auto web_dir = node->declare_parameter<std::string>(
    "web_dir", ament_index_cpp::get_package_share_directory("bt_engine") + "/web");
  const char * env_token = std::getenv("BT_ENGINE_TOKEN");
  const auto token = node->declare_parameter<std::string>(
    "auth_token", env_token ? env_token : "");
  if (!token.empty()) {
    RCLCPP_INFO(node->get_logger(), "API requires a token (header X-BT-Token or ?token=)");
  }
  bt_engine::setupRoutes(svr, runs, web_dir, token);
  rclcpp::on_shutdown([&svr] {svr.stop();});

  RCLCPP_INFO(
    node->get_logger(), "BT engine on http://%s:%ld  (node palette page at /)", host.c_str(), port);
  if (!svr.listen(host, static_cast<int>(port))) {
    RCLCPP_FATAL(node->get_logger(), "failed to bind %s:%ld", host.c_str(), port);
  }

  runs.shutdown();
  executor.cancel();
  spin_thread.join();
  if (rclcpp::ok()) {
    rclcpp::shutdown();
  }
  return 0;
}
