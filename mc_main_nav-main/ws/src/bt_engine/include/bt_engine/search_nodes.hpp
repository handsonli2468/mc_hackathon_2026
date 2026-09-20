#pragma once

#include <chrono>
#include <cmath>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <vector>

#include <behaviortree_cpp/action_node.h>
#include <behaviortree_cpp/condition_node.h>

#include "bt_engine/ros_context.hpp"
#include "robot_interfaces/action/navigate_to_point.hpp"

namespace bt_engine
{

// Sends NavigateToPoint goals from nodes that are not themselves action leaves
// (the patrol loop and the approach decorator both drive this way).
class NavGoalDriver
{
public:
  using ActionT = robot_interfaces::action::NavigateToPoint;
  using Client = rclcpp_action::Client<ActionT>;
  using GoalHandle = rclcpp_action::ClientGoalHandle<ActionT>;

  enum class Progress { Running, Succeeded, Failed };

  NavGoalDriver(std::shared_ptr<RosContext> ctx, std::string server)
  : ctx_(std::move(ctx)), server_(std::move(server)),
    client_(ctx_->actionClient<ActionT>(server_)) {}

  bool send(
    double x, double y, std::optional<double> yaw_deg, std::string & err,
    double speed_percent = 0.0)
  {
    if (!client_->wait_for_action_server(std::chrono::milliseconds(ctx_->server_wait_ms))) {
      err = "action server '/" + server_ + "' not available";
      return false;
    }
    ActionT::Goal goal;
    goal.x = static_cast<float>(x);
    goal.y = static_cast<float>(y);
    goal.use_yaw = yaw_deg.has_value();
    goal.yaw_deg = static_cast<float>(yaw_deg.value_or(0.0));
    goal.speed_percent = static_cast<float>(speed_percent);

    state_ = std::make_shared<State>();
    auto state = state_;
    typename Client::SendGoalOptions opts;
    opts.goal_response_callback = [state](typename GoalHandle::SharedPtr gh) {
        std::lock_guard<std::mutex> lk(state->mtx);
        state->responded = true;
        state->handle = gh;
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
    goal_x_ = x;
    goal_y_ = y;
    return true;
  }

  Progress poll(std::string & message)
  {
    if (!state_) {
      message = "no goal in flight";
      return Progress::Failed;
    }
    std::unique_lock<std::mutex> lk(state_->mtx);
    if (state_->responded && !state_->handle) {
      lk.unlock();
      message = "goal rejected by '/" + server_ + "'";
      return Progress::Failed;
    }
    if (!state_->done) {
      const bool responded = state_->responded;
      lk.unlock();
      if (!responded &&
        std::chrono::steady_clock::now() - sent_at_ >
        std::chrono::milliseconds(ctx_->goal_response_timeout_ms))
      {
        message = "no response to goal from '/" + server_ + "'";
        return Progress::Failed;
      }
      return Progress::Running;
    }
    const auto code = state_->code;
    const bool success = state_->success;
    message = state_->message;
    lk.unlock();

    if (code == rclcpp_action::ResultCode::SUCCEEDED && success) {
      return Progress::Succeeded;
    }
    if (message.empty()) {
      message = code == rclcpp_action::ResultCode::ABORTED ?
        "aborted by '/" + server_ + "'" : "navigation did not succeed";
    }
    return Progress::Failed;
  }

  void cancel()
  {
    if (!state_) {
      return;
    }
    std::lock_guard<std::mutex> lk(state_->mtx);
    if (state_->handle && !state_->done) {
      try {
        client_->async_cancel_goal(state_->handle);
      } catch (const std::exception &) {
        // Context already shutting down.
      }
    }
    state_.reset();
  }

  bool active() const {return state_ != nullptr;}
  double goalX() const {return goal_x_;}
  double goalY() const {return goal_y_;}

private:
  struct State
  {
    std::mutex mtx;
    bool responded = false;
    bool done = false;
    typename GoalHandle::SharedPtr handle;
    rclcpp_action::ResultCode code = rclcpp_action::ResultCode::UNKNOWN;
    bool success = false;
    std::string message;
  };

  std::shared_ptr<RosContext> ctx_;
  std::string server_;
  std::shared_ptr<Client> client_;
  std::shared_ptr<State> state_;
  std::chrono::steady_clock::time_point sent_at_;
  double goal_x_ = 0.0;
  double goal_y_ = 0.0;
};

// ---------------------------------------------------------------- VisualizeObject
// Action. Asks the camera service to start looking for an object, and succeeds once the
// request is accepted. One job: it does not wait, poll, or drive.
class VisualizeObjectLeaf : public BT::SyncActionNode
{
public:
  VisualizeObjectLeaf(
    const std::string & name, const BT::NodeConfig & config, std::shared_ptr<RosContext> ctx)
  : BT::SyncActionNode(name, config), ctx_(std::move(ctx)) {}

  static BT::PortsList providedPorts()
  {
    return {BT::InputPort<std::string>(
        "object_name", "What the camera should start looking for, e.g. cup")};
  }

  BT::NodeStatus tick() override
  {
    auto name = getInput<std::string>("object_name");
    if (!name) {
      ctx_->note(fullPath(), name.error());
      return BT::NodeStatus::FAILURE;
    }
    const auto reply = ctx_->camera().start(name.value());
    if (!reply.ok) {
      ctx_->note(fullPath(), reply.message);
      return BT::NodeStatus::FAILURE;
    }
    RCLCPP_INFO(ctx_->node->get_logger(), "[%s] %s", fullPath().c_str(), reply.message.c_str());
    return BT::NodeStatus::SUCCESS;
  }

private:
  std::shared_ptr<RosContext> ctx_;
};

// ------------------------------------------------------------------ IsObjectFound
// Condition. Asks the camera service whether it has seen the object yet. Ticked inside a
// ReactiveFallback it is re-checked every tick, so the answer is cached for `poll_ms` to
// keep the engine's 20 Hz tick from hammering their service.
class IsObjectFoundCondition : public BT::ConditionNode
{
public:
  IsObjectFoundCondition(
    const std::string & name, const BT::NodeConfig & config, std::shared_ptr<RosContext> ctx)
  : BT::ConditionNode(name, config), ctx_(std::move(ctx)) {}

  static BT::PortsList providedPorts()
  {
    return {
      BT::InputPort<std::string>("object_name", "Which object to ask about"),
    };
  }

  BT::NodeStatus tick() override
  {
    using namespace std::chrono;
    auto name = getInput<std::string>("object_name");
    if (!name) {
      ctx_->note(fullPath(), name.error());
      return BT::NodeStatus::FAILURE;
    }
    const auto now = steady_clock::now();
    if (asked_ && now - last_poll_ < milliseconds(ctx_->camera_poll_ms.load())) {
      return found_ ? BT::NodeStatus::SUCCESS : BT::NodeStatus::FAILURE;
    }
    last_poll_ = now;
    asked_ = true;

    const auto reply = ctx_->camera().status(name.value());
    if (!reply.ok) {
      // Not found, but say why -- at most once every few seconds, so a dead service is
      // visible in the run notes without burying them.
      if (now - last_complaint_ > seconds(5)) {
        last_complaint_ = now;
        ctx_->note(fullPath(), reply.message);
      }
      found_ = false;
      return BT::NodeStatus::FAILURE;
    }
    if (reply.found && !found_) {
      RCLCPP_INFO(
        ctx_->node->get_logger(), "[%s] the camera has found '%s'",
        fullPath().c_str(), name.value().c_str());
    }
    found_ = reply.found;
    return found_ ? BT::NodeStatus::SUCCESS : BT::NodeStatus::FAILURE;
  }

private:
  std::shared_ptr<RosContext> ctx_;
  bool asked_ = false;
  bool found_ = false;
  std::chrono::steady_clock::time_point last_poll_{};
  std::chrono::steady_clock::time_point last_complaint_{};
};

// ---------------------------------------------------------------------- Patrol
// Drives a closed rectangle clockwise, for ever, one NavigateToPoint goal per corner.
// It never succeeds on its own: something above it (TrackWhenFound) stops it.
class PatrolLeaf : public BT::StatefulActionNode
{
public:
  PatrolLeaf(
    const std::string & name, const BT::NodeConfig & config, std::shared_ptr<RosContext> ctx)
  : BT::StatefulActionNode(name, config), ctx_(std::move(ctx)),
    driver_(ctx_, "navigate_to_point") {}

  // The patrol loop is fixed: a 0.60 x 0.40 m box in the middle of the 1.8 x 1.2 m
  // field, which is the "2-3 eighths of the track" the robot is meant to cover.
  static constexpr double kCentreX = 0.90;
  static constexpr double kCentreY = 0.60;
  static constexpr double kWidth = 0.60;
  static constexpr double kHeight = 0.40;

  static BT::PortsList providedPorts()
  {
    return {
      BT::InputPort<std::string>(
        "speed", "normal", "How fast to drive: slow | normal | fast"),
    };
  }

protected:
  BT::NodeStatus onStart() override
  {
    // Clockwise seen from above with +Y up: top-left, top-right, bottom-right, bottom-left.
    corners_ = {
      {kCentreX - kWidth / 2, kCentreY + kHeight / 2},
      {kCentreX + kWidth / 2, kCentreY + kHeight / 2},
      {kCentreX + kWidth / 2, kCentreY - kHeight / 2},
      {kCentreX - kWidth / 2, kCentreY - kHeight / 2},
    };
    next_ = 0;
    consecutive_failures_ = 0;
    return goToNextCorner();
  }

  BT::NodeStatus onRunning() override
  {
    std::string message;
    switch (driver_.poll(message)) {
      case NavGoalDriver::Progress::Running:
        return BT::NodeStatus::RUNNING;
      case NavGoalDriver::Progress::Succeeded:
        consecutive_failures_ = 0;
        return goToNextCorner();
      case NavGoalDriver::Progress::Failed:
        // One unreachable corner should not end the patrol; a wall of them should.
        if (++consecutive_failures_ >= static_cast<int>(corners_.size())) {
          return fail("cannot reach any patrol corner: " + message);
        }
        ctx_->note(fullPath(), "skipping a patrol corner: " + message);
        return goToNextCorner();
    }
    return BT::NodeStatus::RUNNING;
  }

  void onHalted() override {driver_.cancel();}

private:
  struct Point {double x, y;};

  // Turn the named profile into a percentage, or explain what the names are.
  std::optional<double> resolveSpeed(std::string & err)
  {
    const auto name = getInput<std::string>("speed").value_or("normal");
    const auto percent = ctx_->speedProfile(name);
    if (!percent) {
      err = "speed='" + name + "'; use one of: " + ctx_->speedProfileList();
      return std::nullopt;
    }
    return percent;
  }


  BT::NodeStatus fail(const std::string & msg)
  {
    ctx_->note(fullPath(), msg);
    return BT::NodeStatus::FAILURE;
  }

  BT::NodeStatus goToNextCorner()
  {
    const Point & target = corners_[next_];
    const Point & from = corners_[(next_ + corners_.size() - 1) % corners_.size()];
    // Face along the leg we are about to drive, like a robot doing laps.
    const double yaw_deg = std::atan2(target.y - from.y, target.x - from.x) * 180.0 / M_PI;
    next_ = (next_ + 1) % corners_.size();

    driver_.cancel();
    std::string err;
    const auto speed = resolveSpeed(err);
    if (!speed) {
      return fail(err);
    }
    if (!driver_.send(target.x, target.y, yaw_deg, err, *speed)) {
      return fail(err);
    }
    return BT::NodeStatus::RUNNING;
  }

  std::shared_ptr<RosContext> ctx_;
  NavGoalDriver driver_;
  std::vector<Point> corners_;
  size_t next_ = 0;
  int consecutive_failures_ = 0;
};

// -------------------------------------------------- NavigateToDetectedObject
// Action. Drives to wherever the camera says the object is *now*: the pose keeps being
// republished and moves, so this follows it rather than driving to one fixed point.
//
// Arrival is judged by how far the robot is from the latest pose, not by the navigation
// goal finishing. With a pose that keeps shifting, each shift cancels and re-sends the
// goal, so waiting for one to complete could wait for ever. Getting within
// `standoff + arrive_tolerance` of the object is what "arrived" means.
class NavigateToDetectedObjectLeaf : public BT::StatefulActionNode
{
public:
  NavigateToDetectedObjectLeaf(
    const std::string & name, const BT::NodeConfig & config, std::shared_ptr<RosContext> ctx)
  : BT::StatefulActionNode(name, config), ctx_(std::move(ctx)),
    driver_(ctx_, "navigate_to_point") {}

  static BT::PortsList providedPorts()
  {
    return {
      BT::InputPort<double>(
        "standoff", 0.18,
        "Stop this far short of the reported pose, metres. Driving onto the object would "
        "put it closer than the onboard camera can see. 0 drives exactly to the pose."),
      BT::InputPort<double>(
        "replan_distance", 0.05,
        "Re-send the goal if the reported pose moves more than this, metres"),
      BT::InputPort<double>(
        "arrive_tolerance", 0.05,
        "Succeed once the robot is within standoff + this of the latest pose, metres"),
      BT::InputPort<std::string>(
        "speed", "normal", "How fast to drive: slow | normal | fast"),
    };
  }

protected:
  BT::NodeStatus onStart() override
  {
    started_at_ = std::chrono::steady_clock::now();
    last_sent_ = {};
    warned_no_robot_pose_ = false;
    return drive();
  }

  BT::NodeStatus onRunning() override {return drive();}

  void onHalted() override {driver_.cancel();}

private:
  BT::NodeStatus fail(const std::string & msg)
  {
    ctx_->note(fullPath(), msg);
    return BT::NodeStatus::FAILURE;
  }

  // Turn the named profile into a percentage, or explain what the names are.
  std::optional<double> resolveSpeed(std::string & err)
  {
    const auto name = getInput<std::string>("speed").value_or("normal");
    const auto percent = ctx_->speedProfile(name);
    if (!percent) {
      err = "speed='" + name + "'; use one of: " + ctx_->speedProfileList();
      return std::nullopt;
    }
    return percent;
  }

  BT::NodeStatus drive()
  {
    using namespace std::chrono;
    const auto latest = ctx_->latestGoalPose();
    if (!latest) {
      if (steady_clock::now() - started_at_ >
        milliseconds(ctx_->object_pose_timeout_ms.load()))
      {
        return fail("no object pose on '" + ctx_->goalPoseTopic() + "'; has the camera seen it?");
      }
      return BT::NodeStatus::RUNNING;
    }

    // The camera may publish in its own optical frame, where the object is ~0.3 m away
    // and the numbers look perfectly plausible as map coordinates. Driving there would
    // send the robot to the corner of the field, so say so instead.
    const auto & frame = latest->pose.header.frame_id;
    if (!frame.empty() && frame != ctx_->mapFrame()) {
      return fail(
        "the object pose on '" + ctx_->goalPoseTopic() + "' is in frame '" + frame +
        "', not '" + ctx_->mapFrame() + "'; ask the camera team for map-frame coordinates");
    }

    const double object_x = latest->pose.pose.position.x;
    const double object_y = latest->pose.pose.position.y;
    const double standoff = getInput<double>("standoff").value_or(0.18);
    const auto me = ctx_->latestRobotPose();

    // Close enough to the object as it is *now*: stop, whatever the navigation goal is doing.
    if (me) {
      const double gap = std::hypot(
        object_x - me->pose.position.x, object_y - me->pose.position.y);
      if (gap <= standoff + getInput<double>("arrive_tolerance").value_or(0.05)) {
        driver_.cancel();
        return BT::NodeStatus::SUCCESS;
      }
    } else if (!warned_no_robot_pose_) {
      warned_no_robot_pose_ = true;
      ctx_->note(
        fullPath(),
        "no robot pose on '" + ctx_->robotPoseTopic() +
        "', so 'standoff' is ignored and arrival is left to the navigation goal");
    }

    // Aim `standoff` short of the object, along the line from the robot to it.
    double x = object_x;
    double y = object_y;
    if (standoff > 0.0 && me) {
      const double dx = object_x - me->pose.position.x;
      const double dy = object_y - me->pose.position.y;
      const double dist = std::hypot(dx, dy);
      if (dist > 1e-6) {
        x -= dx / dist * standoff;
        y -= dy / dist * standoff;
      }
    }

    using clock = std::chrono::steady_clock;
    const auto now = clock::now();
    const bool moved = driver_.active() &&
      std::hypot(x - driver_.goalX(), y - driver_.goalY()) >
      getInput<double>("replan_distance").value_or(0.05);
    const bool may_replan = now - last_sent_ >
      std::chrono::milliseconds(ctx_->replan_min_interval_ms.load());

    if (!driver_.active() || (moved && may_replan)) {
      driver_.cancel();
      std::string err;
      const auto speed = resolveSpeed(err);
      if (!speed) {
        return fail(err);
      }
      if (!driver_.send(x, y, std::nullopt, err, *speed)) {
        return fail(err);
      }
      last_sent_ = now;
      return BT::NodeStatus::RUNNING;
    }

    std::string message;
    switch (driver_.poll(message)) {
      case NavGoalDriver::Progress::Running:
        return BT::NodeStatus::RUNNING;
      case NavGoalDriver::Progress::Succeeded:
        // Reached the point we aimed at. If the object has since moved away, the next
        // tick sends a fresh goal rather than declaring victory in the wrong place.
        driver_.cancel();
        return me ? BT::NodeStatus::RUNNING : BT::NodeStatus::SUCCESS;
      case NavGoalDriver::Progress::Failed:
        return fail("could not reach the object: " + message);
    }
    return BT::NodeStatus::RUNNING;
  }

  std::shared_ptr<RosContext> ctx_;
  NavGoalDriver driver_;
  bool warned_no_robot_pose_ = false;
  std::chrono::steady_clock::time_point started_at_{};
  std::chrono::steady_clock::time_point last_sent_{};
};

}  // namespace bt_engine
