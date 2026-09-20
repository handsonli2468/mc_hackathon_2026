#include "bt_engine/http_api.hpp"

using json = nlohmann::json;

namespace bt_engine
{

namespace
{

void reply(httplib::Response & res, const json & body, int code = 200)
{
  res.status = code;
  res.set_content(body.dump(2), "application/json");
}

// Accepts either raw XML or JSON {"xml": "..."}.
bool extractXml(const httplib::Request & req, httplib::Response & res, std::string & xml)
{
  const auto first = req.body.find_first_not_of(" \t\r\n");
  if (first != std::string::npos && req.body[first] == '{') {
    auto j = json::parse(req.body, nullptr, false);
    if (j.is_discarded() || !j.contains("xml") || !j["xml"].is_string()) {
      reply(res, {{"ok", false}, {"error", "JSON body must be {\"xml\": \"<root>...\"}"}}, 400);
      return false;
    }
    xml = j["xml"].get<std::string>();
  } else {
    xml = req.body;
  }
  if (xml.empty()) {
    reply(res, {{"ok", false}, {"error", "empty body; send BT XML"}}, 400);
    return false;
  }
  return true;
}

}  // namespace

void setupRoutes(
  httplib::Server & svr, RunManager & runs, const std::string & web_dir,
  const std::string & token)
{
  // Node palette page (and anything else in web/).
  svr.set_mount_point("/", web_dir);

  svr.set_default_headers({
      {"Access-Control-Allow-Origin", "*"},
      {"Access-Control-Allow-Headers", "Content-Type, X-BT-Token"},
    });

  // With a token set (exposed to the internet), the API needs it; the page does not,
  // so a browser can load it and ask for the token.
  if (!token.empty()) {
    svr.set_pre_routing_handler(
      [token](const httplib::Request & req, httplib::Response & res) {
        const bool open_path = req.method == "OPTIONS" || req.path == "/" ||
        req.path == "/index.html" || req.path == "/health";
        if (open_path) {
          return httplib::Server::HandlerResponse::Unhandled;
        }
        const auto given = req.get_header_value("X-BT-Token");
        if (given == token || req.get_param_value("token") == token) {
          return httplib::Server::HandlerResponse::Unhandled;
        }
        reply(res, {{"ok", false}, {"error", "missing or wrong token"}}, 401);
        return httplib::Server::HandlerResponse::Handled;
      });
  }
  svr.Options(R"(.*)", [](const httplib::Request &, httplib::Response & res) {
      res.status = 204;
    });

  // Every reply is JSON, including the ones httplib generates itself.
  svr.set_error_handler([](const httplib::Request &, httplib::Response & res) {
      if (res.body.empty() || res.get_header_value("Content-Type").rfind("application/json", 0) != 0) {
        reply(res, {{"ok", false}, {"error", "no such endpoint"}}, res.status);
      }
    });
  svr.set_exception_handler(
    [](const httplib::Request &, httplib::Response & res, std::exception_ptr ep) {
      std::string what = "internal error";
      try {
        std::rethrow_exception(ep);
      } catch (const std::exception & e) {
        what = e.what();
      } catch (...) {
      }
      reply(res, {{"ok", false}, {"error", what}}, 500);
    });

  svr.Get("/health", [](const httplib::Request &, httplib::Response & res) {
      reply(res, {{"ok", true}});
    });

  svr.Get("/nodes", [&runs](const httplib::Request & req, httplib::Response & res) {
      const bool builtin = req.get_param_value("builtin") != "0";
      res.set_content(runs.nodesModel(builtin), "application/xml");
    });

  svr.Post("/validate", [&runs](const httplib::Request & req, httplib::Response & res) {
      std::string xml;
      if (extractXml(req, res, xml)) {
        auto out = runs.validate(xml);
        reply(res, out, out["ok"].get<bool>() ? 200 : 422);
      }
    });

  svr.Post("/execute", [&runs](const httplib::Request & req, httplib::Response & res) {
      std::string xml;
      if (extractXml(req, res, xml)) {
        auto out = runs.start(xml);
        reply(res, out, out["ok"].get<bool>() ? 202 : 422);
      }
    });

  svr.Post("/cancel", [&runs](const httplib::Request &, httplib::Response & res) {
      reply(res, runs.cancel());
    });

  svr.Get("/runs", [&runs](const httplib::Request &, httplib::Response & res) {
      reply(res, runs.list());
    });

  auto status_handler = [&runs](const httplib::Request & req, httplib::Response & res) {
      const std::string id = req.matches.size() > 1 ? req.matches[1].str() : "";
      auto out = runs.status(id, req.get_param_value("trace") == "full");
      if (out.is_null()) {
        reply(res, {{"ok", false}, {"error", "no such run"}}, 404);
      } else {
        reply(res, out);
      }
    };
  svr.Get("/status", status_handler);
  svr.Get(R"(/status/([\w\-]+))", status_handler);
}

}  // namespace bt_engine
