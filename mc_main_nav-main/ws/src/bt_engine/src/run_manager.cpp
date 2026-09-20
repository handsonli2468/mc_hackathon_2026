#include "bt_engine/run_manager.hpp"

#include <chrono>

#include <behaviortree_cpp/loggers/abstract_logger.h>
#include <behaviortree_cpp/loggers/groot2_publisher.h>
#include <behaviortree_cpp/xml_parsing.h>
#include <sys/stat.h>
#include <tinyxml2.h>

#include "bt_engine/register_nodes.hpp"

using json = nlohmann::json;

namespace bt_engine
{

namespace
{

double nowSec()
{
  using namespace std::chrono;
  return duration<double>(system_clock::now().time_since_epoch()).count();
}

bool isLeaf(const BT::TreeNode & n)
{
  return n.type() == BT::NodeType::ACTION || n.type() == BT::NodeType::CONDITION;
}

}  // namespace

// Records status transitions and which leaves are currently running.
class TraceLogger : public BT::StatusChangeLogger
{
public:
  static constexpr size_t kMaxEvents = 1000;

  explicit TraceLogger(const BT::Tree & tree)
  : BT::StatusChangeLogger(tree.rootNode()), t0_(std::chrono::steady_clock::now()) {}

  void callback(
    BT::Duration, const BT::TreeNode & node, BT::NodeStatus prev,
    BT::NodeStatus status) override
  {
    std::lock_guard<std::mutex> lk(mtx_);
    if (isLeaf(node)) {
      if (status == BT::NodeStatus::RUNNING) {
        running_[node.UID()] = node.fullPath();
      } else {
        running_.erase(node.UID());
      }
    }
    if (status == BT::NodeStatus::IDLE) {
      return;
    }
    const double t = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0_).count();
    json ev = {
      {"t", std::round(t * 1000.0) / 1000.0},
      {"node", node.fullPath()},
      {"type", node.registrationName()},
      {"from", BT::toStr(prev)},
      {"to", BT::toStr(status)},
    };
    if (isLeaf(node) && status == BT::NodeStatus::FAILURE) {
      last_leaf_failure_ = ev;
    }
    // A reactive condition re-ticks every tick, so the same transition can repeat hundreds
    // of times. Collapse a run of identical ones into a single entry with a count, so the
    // trace still says what happened without burying it.
    if (!events_.empty()) {
      json & prev_ev = events_.back();
      if (prev_ev["node"] == ev["node"] && prev_ev["from"] == ev["from"] &&
        prev_ev["to"] == ev["to"])
      {
        prev_ev["repeated"] = prev_ev.value("repeated", 1) + 1;
        prev_ev["t"] = ev["t"];      // keep the most recent time
        return;
      }
    }
    events_.push_back(std::move(ev));
    if (events_.size() > kMaxEvents) {
      events_.pop_front();
    }
  }

  void flush() override {}

  json snapshot(bool full_trace, size_t tail = 40) const
  {
    std::lock_guard<std::mutex> lk(mtx_);
    json running = json::array();
    for (const auto & [uid, path] : running_) {
      running.push_back(path);
    }
    json events = json::array();
    size_t first = (full_trace || events_.size() <= tail) ? 0 : events_.size() - tail;
    for (size_t i = first; i < events_.size(); ++i) {
      events.push_back(events_[i]);
    }
    return {
      {"running_leaves", running},
      {"last_leaf_failure", last_leaf_failure_},
      {"trace", events},
      {"trace_truncated", first > 0},
    };
  }

private:
  mutable std::mutex mtx_;
  std::chrono::steady_clock::time_point t0_;
  std::map<uint16_t, std::string> running_;
  std::deque<json> events_;
  json last_leaf_failure_ = nullptr;
};

struct RunManager::Run
{
  std::string id;
  std::string xml;
  std::atomic<bool> stop{false};

  mutable std::mutex mtx;
  std::string state = "running";  // running | success | failure | canceled | error
  std::string error;
  double started_at = 0.0;
  double finished_at = 0.0;
  std::shared_ptr<TraceLogger> logger;   // live while running
  json final_trace;                      // snapshot taken when finished (full)
  json notes = json::array();
};

RunManager::RunManager(
  BT::BehaviorTreeFactory & factory, std::shared_ptr<RosContext> ctx, RunOptions opts)
: factory_(factory), ctx_(std::move(ctx)), opts_(opts)
{
  // Whatever is registered before the first tree is submitted came from places_file.
  for (const auto & id : factory_.registeredBehaviorTrees()) {
    imported_.insert(id);
  }
  struct stat st {};
  if (!opts_.places_file.empty() && stat(opts_.places_file.c_str(), &st) == 0) {
    places_mtime_ = static_cast<int64_t>(st.st_mtime);
  }
}

// Editing places.xml should be enough: notice a new mtime and import it again. The whole
// registration is rebuilt, so a place deleted from the file really disappears.
void RunManager::reloadPlacesIfChanged()
{
  if (opts_.places_file.empty()) {
    return;
  }
  struct stat st {};
  if (stat(opts_.places_file.c_str(), &st) != 0) {
    return;   // gone or unreadable: keep what is already registered
  }
  const auto mtime = static_cast<int64_t>(st.st_mtime);
  if (mtime == places_mtime_) {
    return;
  }
  places_mtime_ = mtime;
  try {
    factory_.clearRegisteredBehaviorTrees();
    factory_.registerBehaviorTreeFromFile(opts_.places_file);
    imported_.clear();
    std::string ids;
    for (const auto & id : factory_.registeredBehaviorTrees()) {
      imported_.insert(id);
      ids += (ids.empty() ? "" : ", ") + id;
    }
    RCLCPP_INFO(ctx_->node->get_logger(), "places re-read after an edit: %s", ids.c_str());
  } catch (const std::exception & e) {
    // Keep serving with whatever was registered before the bad edit.
    RCLCPP_ERROR(ctx_->node->get_logger(), "places file is broken, keeping the old one: %s",
      e.what());
  }
}

std::vector<std::string> RunManager::importedSubtrees() const
{
  return {imported_.begin(), imported_.end()};
}

namespace
{
// A submitted tree may CALL an imported subtree; a <BehaviorTree ID="GoHome"> of its own is
// dropped, so the imported definition stays authoritative for the life of the engine.
// Examples carry copies of the places for readability, and this is what keeps a stale copy
// from silently redefining a place for every later run.
std::string dropRedefinedImports(
  const std::string & xml, const std::set<std::string> & imported,
  std::vector<std::string> & dropped)
{
  if (imported.empty()) {
    return xml;
  }
  tinyxml2::XMLDocument doc;
  if (doc.Parse(xml.c_str()) != tinyxml2::XML_SUCCESS) {
    return xml;   // let BT.CPP report the parse error, with its line numbers
  }
  auto * root = doc.RootElement();
  if (root == nullptr) {
    return xml;
  }
  std::vector<tinyxml2::XMLElement *> drop;
  for (auto * e = root->FirstChildElement("BehaviorTree"); e != nullptr;
    e = e->NextSiblingElement("BehaviorTree"))
  {
    const char * id = e->Attribute("ID");
    if (id != nullptr && imported.count(id) > 0) {
      drop.push_back(e);
      dropped.emplace_back(id);
    }
  }
  if (drop.empty()) {
    return xml;
  }
  // Keep a tree that asks to RUN a place as its main tree working: the import supplies it.
  for (auto * e : drop) {
    root->DeleteChild(e);
  }
  tinyxml2::XMLPrinter printer;
  doc.Print(&printer);
  return printer.CStr();
}
}  // namespace

RunManager::~RunManager() {shutdown();}

void RunManager::shutdown()
{
  std::lock_guard<std::mutex> lk(api_mtx_);
  stopCurrent();
}

json RunManager::validate(const std::string & xml)
{
  std::lock_guard<std::mutex> lk(api_mtx_);
  reloadPlacesIfChanged();
  try {
    std::vector<std::string> dropped;
    auto tree = factory_.createTreeFromText(dropRedefinedImports(xml, imported_, dropped));
    checkTreeStructure(tree, *ctx_, factory_);
    if (dropped.empty()) {
      return {{"ok", true}};
    }
    return {{"ok", true}, {"used_imported_subtrees", dropped}};
  } catch (const std::exception & e) {
    return {{"ok", false}, {"error", e.what()}};
  }
}

json RunManager::start(const std::string & xml)
{
  std::lock_guard<std::mutex> lk(api_mtx_);
  reloadPlacesIfChanged();
  BT::Tree tree;
  std::vector<std::string> dropped;
  try {
    tree = factory_.createTreeFromText(dropRedefinedImports(xml, imported_, dropped));
    checkTreeStructure(tree, *ctx_, factory_);
  } catch (const std::exception & e) {
    // Invalid XML never disturbs the tree that is currently running.
    return {{"ok", false}, {"error", e.what()}};
  }

  const bool preempted = worker_.joinable();
  stopCurrent();

  auto run = std::make_shared<Run>();
  run->id = "run-" + std::to_string(next_id_++);
  run->xml = xml;
  run->started_at = nowSec();
  for (const auto & id : dropped) {
    run->notes.push_back(
      {{"node", id},
        {"message", "the tree redefined '" + id +
          "'; the engine's imported place was used instead"}});
  }
  {
    std::lock_guard<std::mutex> rl(runs_mtx_);
    runs_.push_back(run);
    while (runs_.size() > opts_.history) {
      runs_.pop_front();
    }
  }
  worker_ = std::thread(&RunManager::work, this, run, std::move(tree));
  return {{"ok", true}, {"run_id", run->id}, {"preempted_previous", preempted}};
}

json RunManager::cancel()
{
  std::lock_guard<std::mutex> lk(api_mtx_);
  const bool was_running = worker_.joinable();
  stopCurrent();
  return {{"ok", true}, {"was_running", was_running}};
}

void RunManager::stopCurrent()
{
  if (!worker_.joinable()) {
    return;
  }
  {
    std::lock_guard<std::mutex> rl(runs_mtx_);
    if (!runs_.empty()) {
      runs_.back()->stop = true;
    }
  }
  worker_.join();
}

void RunManager::work(std::shared_ptr<Run> run, BT::Tree tree)
{
  ctx_->takeNotes();  // drop leftovers from previous runs
  auto logger = std::make_shared<TraceLogger>(tree);
  {
    std::lock_guard<std::mutex> lk(run->mtx);
    run->logger = logger;
  }

  std::unique_ptr<BT::Groot2Publisher> groot;
  if (opts_.groot_port > 0) {
    try {
      groot = std::make_unique<BT::Groot2Publisher>(tree, opts_.groot_port);
    } catch (const std::exception & e) {
      RCLCPP_WARN(ctx_->node->get_logger(), "Groot2 publisher disabled: %s", e.what());
    }
  }

  RCLCPP_INFO(ctx_->node->get_logger(), "%s started", run->id.c_str());
  std::string state;
  std::string error;
  try {
    BT::NodeStatus status = BT::NodeStatus::RUNNING;
    while (!run->stop && rclcpp::ok()) {
      status = tree.tickOnce();
      if (status != BT::NodeStatus::RUNNING) {
        break;
      }
      tree.sleep(std::chrono::milliseconds(opts_.tick_ms));
    }
    if (status == BT::NodeStatus::SUCCESS) {
      state = "success";
    } else if (status == BT::NodeStatus::RUNNING) {
      state = "canceled";
      tree.haltTree();
    } else {
      state = "failure";
    }
  } catch (const std::exception & e) {
    state = "error";
    error = e.what();
    try {
      tree.haltTree();
    } catch (...) {
    }
  }

  json notes = json::array();
  for (const auto & n : ctx_->takeNotes()) {
    notes.push_back({{"node", n.node_path}, {"message", n.message}});
  }
  RCLCPP_INFO(ctx_->node->get_logger(), "%s finished: %s", run->id.c_str(), state.c_str());

  groot.reset();
  std::lock_guard<std::mutex> lk(run->mtx);
  run->state = state;
  run->error = error;
  run->finished_at = nowSec();
  run->final_trace = logger->snapshot(true);
  // Append: the run may already carry notes from load time (e.g. a redefined import).
  for (auto & n : notes) {
    run->notes.push_back(std::move(n));
  }
  run->logger.reset();
  // `logger` (local) is released before `tree` goes out of scope.
}

json RunManager::describe(const Run & run, bool full_trace) const
{
  std::lock_guard<std::mutex> lk(run.mtx);
  json out = {
    {"run_id", run.id},
    {"state", run.state},
    {"started_at", run.started_at},
  };
  json snap;
  if (run.logger) {
    snap = run.logger->snapshot(full_trace);
    out["elapsed_s"] = nowSec() - run.started_at;
  } else {
    snap = run.final_trace;
    out["finished_at"] = run.finished_at;
    out["elapsed_s"] = run.finished_at - run.started_at;
    if (!full_trace && snap.contains("trace") && snap["trace"].size() > 40) {
      auto & tr = snap["trace"];
      snap["trace"] = json(tr.end() - 40, tr.end());
      snap["trace_truncated"] = true;
    }
  }
  out.update(snap);
  if (!run.error.empty()) {
    out["error"] = run.error;
  }
  out["notes"] = run.notes;
  return out;
}

json RunManager::status(const std::string & run_id, bool full_trace) const
{
  std::shared_ptr<Run> run;
  {
    std::lock_guard<std::mutex> rl(runs_mtx_);
    if (run_id.empty()) {
      if (!runs_.empty()) {
        run = runs_.back();
      }
    } else {
      for (const auto & r : runs_) {
        if (r->id == run_id) {
          run = r;
        }
      }
    }
  }
  return run ? describe(*run, full_trace) : json(nullptr);
}

json RunManager::list() const
{
  std::lock_guard<std::mutex> rl(runs_mtx_);
  json out = json::array();
  for (const auto & r : runs_) {
    std::lock_guard<std::mutex> lk(r->mtx);
    out.push_back({{"run_id", r->id}, {"state", r->state}, {"started_at", r->started_at}});
  }
  return out;
}

std::string RunManager::nodesModel(bool include_builtin)
{
  std::lock_guard<std::mutex> lk(api_mtx_);
  reloadPlacesIfChanged();
  auto xml = filterBuiltins(factory_, BT::writeTreeNodesModelXML(factory_, include_builtin));

  // The imported places (GoHome, GoToStorage, ...) are registered subtrees, which BT.CPP
  // does not put in the model. The LLM cannot call what it cannot see, so list them. Only
  // the imports are listed, never subtrees a submitted tree happened to register.
  const std::vector<std::string> places(imported_.begin(), imported_.end());
  if (!places.empty()) {
    auto close = xml.rfind("</TreeNodesModel>");
    while (close != std::string::npos && close > 0 && xml[close - 1] == ' ') {
      --close;   // insert at the start of the closing tag's line, so entries line up
    }
    if (close != std::string::npos) {
      std::string entries;
      for (const auto & id : places) {
        entries += "        <SubTree ID=\"" + id + "\"/>\n";
      }
      xml.insert(close, entries);
    }
  }
  return xml;
}

}  // namespace bt_engine
