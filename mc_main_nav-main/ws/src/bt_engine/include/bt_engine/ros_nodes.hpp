#pragma once

#include <chrono>
#include <cmath>
#include <memory>
#include <mutex>
#include <optional>
#include <string>

#include <behaviortree_cpp/action_node.h>
#include <behaviortree_cpp/condition_node.h>

#include "bt_engine/ros_context.hpp"

namespace bt_engine
{

// BT leaf backed by a ROS 2 action server. Every action in robot_interfaces
// has `bool success` + `string message` in its result.
template<class ActionT>
class RosActionLeaf : public BT::StatefulActionNode
{
public:
  using Goal = typename ActionT::Goal;
  using Client = rclcpp_action::Client<ActionT>;
  using GoalHandle = rclcpp_action::ClientGoalHandle<ActionT>;

  RosActionLeaf(
    const std::string & name, const BT::NodeConfig & config,
    std::shared_ptr<RosContext> ctx, std::string server)
  : BT::StatefulActionNode(name, config), ctx_(std::move(ctx)), server_(std::move(server)),
    client_(ctx_->actionClient<ActionT>(server_)) {}

protected:
  // Fill the goal from input ports; return an error message to fail the node.
  virtual std::optional<std::string> buildGoal(Goal & goal) = 0;

  BT::NodeStatus fail(const std::string & msg)
  {
    ctx_->note(fullPath(), msg);
    return BT::NodeStatus::FAILURE;
  }

  BT::NodeStatus onStart() override
  {
    Goal goal;
    if (auto err = buildGoal(goal)) {
      return fail(*err);
    }
    if (!client_->wait_for_action_server(std::chrono::milliseconds(ctx_->server_wait_ms))) {
      return fail("action server '/" + server_ + "' not available");
    }

    state_ = std::make_shared<State>();
    auto state = state_;
    std::weak_ptr<Client> weak_client = client_;

    typename Client::SendGoalOptions opts;
    opts.goal_response_callback = [state, weak_client](typename GoalHandle::SharedPtr gh) {
        std::lock_guard<std::mutex> lk(state->mtx);
        state->responded = true;
        state->handle = gh;
        // Halted before the server accepted: cancel right away.
        if (gh && state->halted) {
          if (auto c = weak_client.lock()) {
            c->async_cancel_goal(gh);
          }
        }
      };
    opts.result_callback = [state](const typename GoalHandle::WrappedResult & r) {
        std::lock_guard<std::mutex> lk(state->mtx);
        state->done = true;
        state->code = r.code;
        if (r.result) {
          state->success = r.result->success;
          state->message = r.result->message;
        }
      };
    client_->async_send_goal(goal, opts);
    sent_at_ = std::chrono::steady_clock::now();
    last_liveness_check_ = sent_at_;
    return BT::NodeStatus::RUNNING;
  }

  BT::NodeStatus onRunning() override
  {
    std::unique_lock<std::mutex> lk(state_->mtx);
    if (state_->responded && !state_->handle) {
      lk.unlock();
      return fail("goal rejected by '/" + server_ + "'");
    }
    if (!state_->done) {
      const bool responded = state_->responded;
      lk.unlock();
      return checkStillAlive(responded);
    }
    const auto code = state_->code;
    const bool success = state_->success;
    const std::string message = state_->message;
    lk.unlock();

    switch (code) {
      case rclcpp_action::ResultCode::SUCCEEDED:
        if (success) {
          return BT::NodeStatus::SUCCESS;
        }
        return fail(message.empty() ? "server reported failure" : message);
      case rclcpp_action::ResultCode::ABORTED:
        return fail(
          message.empty() ? "aborted by '/" + server_ + "' (server crashed? check its log)"
          : "aborted: " + message);
      case rclcpp_action::ResultCode::CANCELED:
        return fail("canceled: " + message);
      default:
        return fail("unknown result code");
    }
  }

  void onHalted() override
  {
    if (!state_) {
      return;
    }
    std::lock_guard<std::mutex> lk(state_->mtx);
    state_->halted = true;
    if (state_->handle && !state_->done) {
      try {
        client_->async_cancel_goal(state_->handle);
      } catch (const std::exception &) {
        // Context may already be shut down; nothing left to cancel.
      }
    }
  }

  std::shared_ptr<RosContext> ctx_;
  std::string server_;

private:
  // A crashed or unplugged module must not leave the tree RUNNING forever.
  BT::NodeStatus checkStillAlive(bool responded)
  {
    using namespace std::chrono;
    const auto now = steady_clock::now();
    if (!responded) {
      if (now - sent_at_ > milliseconds(ctx_->goal_response_timeout_ms)) {
        return fail("no response to goal from '/" + server_ + "'");
      }
      return BT::NodeStatus::RUNNING;
    }
    if (now - last_liveness_check_ > milliseconds(500)) {
      last_liveness_check_ = now;
      if (!client_->action_server_is_ready()) {
        return fail("action server '/" + server_ + "' went away during the goal");
      }
    }
    return BT::NodeStatus::RUNNING;
  }

  struct State
  {
    std::mutex mtx;
    bool responded = false;
    bool halted = false;
    bool done = false;
    typename GoalHandle::SharedPtr handle;
    rclcpp_action::ResultCode code = rclcpp_action::ResultCode::UNKNOWN;
    bool success = false;
    std::string message;
  };

  std::shared_ptr<Client> client_;
  std::shared_ptr<State> state_;
  std::chrono::steady_clock::time_point sent_at_;
  std::chrono::steady_clock::time_point last_liveness_check_;
};

// Action whose goal is just `string object_name`.
template<class ActionT>
class ObjectActionLeaf : public RosActionLeaf<ActionT>
{
public:
  using RosActionLeaf<ActionT>::RosActionLeaf;

  static BT::PortsList providedPorts()
  {
    return {BT::InputPort<std::string>("object_name", "Name of the target object, e.g. cup")};
  }

protected:
  std::optional<std::string> buildGoal(typename ActionT::Goal & goal) override
  {
    auto name = this->template getInput<std::string>("object_name");
    if (!name) {
      return name.error();
    }
    goal.object_name = name.value();
    return std::nullopt;
  }
};

// NavigateToObject: object_name + a named stop distance (contact | near | far).
template<class ActionT>
class NavigateLeaf : public RosActionLeaf<ActionT>
{
public:
  using RosActionLeaf<ActionT>::RosActionLeaf;

  static BT::PortsList providedPorts()
  {
    return {
      BT::InputPort<std::string>("object_name", "Name of the target object, e.g. cup"),
      BT::InputPort<std::string>(
        "distance", "near",
        "How close to stop, robot front to object: contact (~1 cm) | near (~5 cm) | far (~10 cm)"),
    };
  }

protected:
  std::optional<std::string> buildGoal(typename ActionT::Goal & goal) override
  {
    auto name = this->template getInput<std::string>("object_name");
    if (!name) {
      return name.error();
    }
    auto distance = this->template getInput<std::string>("distance");
    if (!distance) {
      return distance.error();
    }
    const auto metres = this->ctx_->navDistance(distance.value());
    if (!metres) {
      return "unknown distance '" + distance.value() + "'; use one of: " +
             this->ctx_->navDistanceList();
    }
    goal.object_name = name.value();
    goal.stop_distance = static_cast<float>(*metres);
    return std::nullopt;
  }
};

// NavigateToPoint: a coordinate in the map frame, optionally with a final heading.
template<class ActionT>
class NavigateToPointLeaf : public RosActionLeaf<ActionT>
{
public:
  using RosActionLeaf<ActionT>::RosActionLeaf;

  static BT::PortsList providedPorts()
  {
    return {
      BT::InputPort<double>("x", "X in metres, map frame"),
      BT::InputPort<double>("y", "Y in metres, map frame"),
      BT::InputPort<std::string>(
        "yaw_deg", "",
        "Heading to face on arrival, degrees (0 = +x). Leave empty to face the way you drove."),
      BT::InputPort<std::string>(
        "speed", "normal", "How fast to drive: slow | normal | fast"),
    };
  }

protected:
  std::optional<std::string> buildGoal(typename ActionT::Goal & goal) override
  {
    auto x = this->template getInput<double>("x");
    if (!x) {
      return x.error();
    }
    auto y = this->template getInput<double>("y");
    if (!y) {
      return y.error();
    }
    goal.x = static_cast<float>(x.value());
    goal.y = static_cast<float>(y.value());
    const auto yaw = this->template getInput<std::string>("yaw_deg").value_or("");
    goal.use_yaw = !yaw.empty();
    if (goal.use_yaw) {
      try {
        goal.yaw_deg = std::stof(yaw);
      } catch (const std::exception &) {
        return "yaw_deg must be a number in degrees, got '" + yaw + "'";
      }
    }
    const auto speed = this->template getInput<std::string>("speed").value_or("normal");
    const auto percent = this->ctx_->speedProfile(speed);
    if (!percent) {
      return "speed='" + speed + "'; use one of: " + this->ctx_->speedProfileList();
    }
    goal.speed_percent = static_cast<float>(*percent);
    return std::nullopt;
  }
};

template<class ActionT>
class RotateInPlaceLeaf : public RosActionLeaf<ActionT>
{
public:
  using RosActionLeaf<ActionT>::RosActionLeaf;

  static BT::PortsList providedPorts()
  {
    return {BT::InputPort<double>(
        "angle_deg", 45.0, "Rotation in degrees, positive = counter-clockwise")};
  }

protected:
  std::optional<std::string> buildGoal(typename ActionT::Goal & goal) override
  {
    auto deg = this->template getInput<double>("angle_deg");
    if (!deg) {
      return deg.error();
    }
    goal.angle_rad = static_cast<float>(deg.value() * M_PI / 180.0);
    return std::nullopt;
  }
};

// Gripper to an arbitrary position on the 0 (open) .. 100 (closed) span.
template<class ActionT>
class GripperLeaf : public RosActionLeaf<ActionT>
{
public:
  GripperLeaf(
    const std::string & name, const BT::NodeConfig & config,
    std::shared_ptr<RosContext> ctx, std::string server)
  : RosActionLeaf<ActionT>(name, config, std::move(ctx), std::move(server)) {}

  static BT::PortsList providedPorts()
  {
    return {BT::InputPort<int>(
        "position", "How far to close the jaws: 0 = fully open, 100 = fully closed")};
  }

protected:
  std::optional<std::string> buildGoal(typename ActionT::Goal & goal) override
  {
    auto position = this->template getInput<int>("position");
    if (!position) {
      return position.error();
    }
    if (*position < 0 || *position > 100) {
      return "position must be between 0 (open) and 100 (closed), got " +
             std::to_string(*position);
    }
    goal.position = *position;
    return std::nullopt;
  }
};

}  // namespace bt_engine
