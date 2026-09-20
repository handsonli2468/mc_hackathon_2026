#pragma once

#include <atomic>
#include <deque>
#include <memory>
#include <mutex>
#include <set>
#include <string>
#include <thread>
#include <vector>

#include <behaviortree_cpp/bt_factory.h>
#include <nlohmann/json.hpp>

#include "bt_engine/ros_context.hpp"

namespace bt_engine
{

class TraceLogger;

struct RunOptions
{
  int tick_ms = 50;
  int groot_port = 1667;   // <= 0 disables the Groot2 publisher
  size_t history = 20;
  // Named places (GoHome, ...). Re-read whenever the file changes, so editing it and
  // sending the next tree is enough - no rebuild, no restart. Empty disables places.
  std::string places_file;
};

// Owns tree execution: one tree at a time, a new start preempts the old one.
class RunManager
{
public:
  RunManager(BT::BehaviorTreeFactory & factory, std::shared_ptr<RosContext> ctx, RunOptions opts);
  ~RunManager();

  // The places imported at start-up (GoHome, GoToStorage, ...). A submitted tree may call
  // them; a definition of its own with the same ID is dropped in favour of the import.
  std::vector<std::string> importedSubtrees() const;

  // Parse + instantiate without running. {"ok": bool, "error": str}
  nlohmann::json validate(const std::string & xml);
  // Validate, preempt the current run, start. {"ok", "run_id"} or {"ok": false, "error"}
  nlohmann::json start(const std::string & xml);
  nlohmann::json cancel();
  // Empty id = latest run. Returns null json if unknown.
  nlohmann::json status(const std::string & run_id, bool full_trace) const;
  nlohmann::json list() const;
  std::string nodesModel(bool include_builtin);
  void shutdown();

private:
  struct Run;

  void work(std::shared_ptr<Run> run, BT::Tree tree);
  void stopCurrent();
  nlohmann::json describe(const Run & run, bool full_trace) const;

  BT::BehaviorTreeFactory & factory_;
  std::set<std::string> imported_;   // subtree IDs that came from places_file
  int64_t places_mtime_ = 0;         // last mtime seen, to notice an edit
  void reloadPlacesIfChanged();      // call with api_mtx_ held
  std::shared_ptr<RosContext> ctx_;
  RunOptions opts_;

  std::mutex api_mtx_;             // serializes validate/start/cancel
  mutable std::mutex runs_mtx_;    // guards runs_
  std::deque<std::shared_ptr<Run>> runs_;
  std::thread worker_;
  int next_id_ = 1;
};

}  // namespace bt_engine
