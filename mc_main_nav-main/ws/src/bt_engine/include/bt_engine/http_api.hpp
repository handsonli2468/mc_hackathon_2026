#pragma once

#include <httplib.h>

#include "bt_engine/run_manager.hpp"

namespace bt_engine
{

// `web_dir` holds the node-palette page served at "/". When `token` is not empty,
// every API call must carry it (header `X-BT-Token` or `?token=`); the page itself stays open.
void setupRoutes(
  httplib::Server & svr, RunManager & runs, const std::string & web_dir,
  const std::string & token = "");

}  // namespace bt_engine
