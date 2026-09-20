#include "bt_engine/register_nodes.hpp"

#include <set>
#include <stdexcept>
#include <string>
#include <vector>

#include <tinyxml2.h>

#include "bt_engine/ros_nodes.hpp"
#include "bt_engine/search_nodes.hpp"
#include "robot_interfaces/action/navigate_to_point.hpp"
#include "robot_interfaces/action/rotate_in_place.hpp"
#include "robot_interfaces/action/set_gripper.hpp"

namespace bt_engine
{

void registerRobotNodes(BT::BehaviorTreeFactory & factory, std::shared_ptr<RosContext> ctx)
{
  namespace act = robot_interfaces::action;
  using std::string;

  // Actions (long-running, ROS 2 action servers)
  factory.registerNodeType<NavigateToPointLeaf<act::NavigateToPoint>>(
    "NavigateToPoint", ctx, string("navigate_to_point"));
  factory.registerNodeType<RotateInPlaceLeaf<act::RotateInPlace>>(
    "RotateInPlace", ctx, string("rotate_in_place"));
  // The gripper takes a 0 (open) .. 100 (closed) position.
  factory.registerNodeType<GripperLeaf<act::SetGripper>>(
    "SetGripper", ctx, string("set_gripper"));

  // Conditions (quick checks)
  factory.registerNodeType<IsObjectFoundCondition>("IsObjectFound", ctx);

  factory.registerNodeType<VisualizeObjectLeaf>("VisualizeObject", ctx);
  factory.registerNodeType<PatrolLeaf>("Patrol", ctx);
  factory.registerNodeType<NavigateToDetectedObjectLeaf>("NavigateToDetectedObject", ctx);
}

// The built-ins an LLM should see. BT.CPP refuses to unregister a builtin, so instead the
// palette is filtered on the way out (filterBuiltins) and a tree using anything else is
// rejected in checkTreeStructure. Scripting and blackboard nodes are deliberately absent:
// trees are generated fresh each run, so there is no state for them to carry.
const std::set<std::string> & allowedBuiltins()
{
  static const std::set<std::string> keep = {
    // Controls
    "Sequence", "Fallback", "ReactiveSequence", "ReactiveFallback",
    // Decorators
    "Repeat", "RetryUntilSuccessful", "Timeout", "Delay",
    "Inverter", "ForceSuccess", "ForceFailure", "KeepRunningUntilFailure",
    // Structural
    "SubTree",
  };
  return keep;
}

std::string filterBuiltins(const BT::BehaviorTreeFactory & factory, const std::string & model_xml)
{
  const std::set<std::string> & keep = allowedBuiltins();
  const std::set<std::string> builtins = factory.builtinNodes();

  tinyxml2::XMLDocument doc;
  if (doc.Parse(model_xml.c_str()) != tinyxml2::XML_SUCCESS) {
    return model_xml;  // never worth failing /nodes over
  }
  auto * root = doc.RootElement();
  auto * model = root ? root->FirstChildElement("TreeNodesModel") : nullptr;
  if (model == nullptr) {
    return model_xml;
  }
  std::vector<tinyxml2::XMLElement *> drop;
  for (auto * e = model->FirstChildElement(); e != nullptr; e = e->NextSiblingElement()) {
    const char * id = e->Attribute("ID");
    if (id != nullptr && builtins.count(id) > 0 && keep.count(id) == 0) {
      drop.push_back(e);
    }
  }
  for (auto * e : drop) {
    model->DeleteChild(e);
  }
  tinyxml2::XMLPrinter printer;
  doc.Print(&printer);
  return printer.CStr();
}

void checkTreeStructure(
  BT::Tree & tree, const RosContext & ctx, const BT::BehaviorTreeFactory & factory)
{
  const std::set<std::string> builtins = factory.builtinNodes();
  tree.applyVisitor([&ctx, &builtins](BT::TreeNode * node) {
      // A builtin hidden from the palette must not sneak into a tree either.
      if (builtins.count(node->registrationName()) > 0 &&
      allowedBuiltins().count(node->registrationName()) == 0)
      {
        throw BT::RuntimeError(
          "node '", node->fullPath(), "' uses '", node->registrationName(),
          "', which is not in the palette; see GET /nodes");
      }
      // Required input ports (no default) must be set in the XML, otherwise the
      // node would only fail once it gets ticked.
      const BT::NodeConfig & config = static_cast<const BT::TreeNode *>(node)->config();
      if (const auto * manifest = config.manifest) {
        for (const auto & [port_name, info] : manifest->ports) {
          if (info.direction() != BT::PortDirection::OUTPUT &&
          info.defaultValue().empty() &&
          config.input_ports.count(port_name) == 0)
          {
            throw BT::RuntimeError(
              "node '", node->fullPath(), "' (", node->registrationName(),
              ") is missing required port '", port_name, "'");
          }
        }
      }
      // A speed profile name is one of a fixed set, so catch a wrong one before moving.
      {
        const auto it = config.input_ports.find("speed");
        if (it != config.input_ports.end() && !it->second.empty() && it->second.front() != '{' &&
          !ctx.speedProfile(it->second))
        {
          throw BT::RuntimeError(
            "node '", node->fullPath(), "' has speed='", it->second,
            "'; use one of: ", ctx.speedProfileList());
        }
      }
      if (node->registrationName() == "SetGripper") {
        const auto it = config.input_ports.find("position");
        if (it != config.input_ports.end() && !it->second.empty() && it->second.front() != '{') {
          try {
            const int position = std::stoi(it->second);
            if (position < 0 || position > 100) {
              throw BT::RuntimeError(
                "node '", node->fullPath(), "' has position='", it->second,
                "'; it must be between 0 (open) and 100 (closed)");
            }
          } catch (const std::invalid_argument &) {
            throw BT::RuntimeError(
              "node '", node->fullPath(), "' has position='", it->second,
              "', which is not a number (0 = open .. 100 = closed)");
          } catch (const std::out_of_range &) {
            throw BT::RuntimeError(
              "node '", node->fullPath(), "' has position='", it->second,
              "'; it must be between 0 (open) and 100 (closed)");
          }
        }
      }
    });
}

}  // namespace bt_engine
