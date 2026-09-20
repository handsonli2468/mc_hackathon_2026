#pragma once

#include <memory>
#include <set>
#include <string>

#include <behaviortree_cpp/bt_factory.h>

#include "bt_engine/ros_context.hpp"

namespace bt_engine
{

// Registers the robot node palette. This list IS the contract with the LLM:
// GET /nodes exports it, and XML using anything else is rejected.
void registerRobotNodes(BT::BehaviorTreeFactory & factory, std::shared_ptr<RosContext> ctx);

// The BT.CPP built-ins an LLM may use; everything else BT.CPP registers is hidden from
// the palette and rejected at load time.
const std::set<std::string> & allowedBuiltins();

// Strips the hidden built-ins out of a TreeNodesModel XML, for GET /nodes.
std::string filterBuiltins(const BT::BehaviorTreeFactory & factory, const std::string & model_xml);

// Extra structural checks BT.CPP does not do at load time. Throws on error.
void checkTreeStructure(
  BT::Tree & tree, const RosContext & ctx, const BT::BehaviorTreeFactory & factory);

}  // namespace bt_engine
