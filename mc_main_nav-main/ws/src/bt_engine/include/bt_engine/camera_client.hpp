#pragma once

#include <chrono>
#include <mutex>
#include <string>
#include <vector>

#include <httplib.h>
#include <nlohmann/json.hpp>

namespace bt_engine
{

// Where the camera team's HTTP service lives. All of it is set from ROS parameters
// (camera.base_url, camera.start_path, ...) so their endpoints can change without a rebuild:
//   ros2 param set /bt_engine camera.base_url http://192.168.50.77:8000
struct CameraConfig
{
  std::string base_url;                     // e.g. http://192.168.50.125:8080
  std::string start_path = "/api/query";    // POST {"<start_field>": "..."} -> start looking
  std::string start_field = "text";         // the JSON field their endpoint expects
  std::string status_path = "/api/status";  // GET -> has it been seen?
  std::string status_query_param;           // if set, appended as ?<param>=<object_name>
  double max_age_s = 8.0;                   // ignore sightings older than this (0 = any age).
  // The VLM runs on an edge device, so a fresh sighting can still be seconds old: set this
  // to about three times its cycle time, low enough that a removed object stops reading FOUND.
  std::string token;                        // optional, sent as X-Camera-Token
  int timeout_ms = 3000;
};

struct CameraReply
{
  bool ok = false;      // did we get a sane answer at all
  bool found = false;   // only meaningful for status()
  std::string message;  // human-readable, ends up in the run's failure notes
};

// Small HTTP client for the camera service. Deliberately tolerant about the reply
// shape, because the exact contract is the camera team's to define: any of
// visualized / found / detected / visible / present (bool), or status: "found",
// is understood. A reply we cannot read is reported rather than guessed at.
class CameraClient
{
public:
  explicit CameraClient(CameraConfig config)
  : config_(std::move(config)) {}

  const CameraConfig & config() const {return config_;}

  CameraReply start(const std::string & object_name)
  {
    if (config_.base_url.empty()) {
      return {false, false, "camera.base_url is not set (ros2 param set /bt_engine camera.base_url ...)"};
    }
    nlohmann::json body;
    body[config_.start_field] = object_name;
    auto cli = makeClient();
    auto res = cli->Post(config_.start_path, headers(), body.dump(), "application/json");
    if (!res) {
      return {false, false, "cannot reach the camera service at " + config_.base_url +
              config_.start_path + " (" + httplib::to_string(res.error()) + ")"};
    }
    if (res->status < 200 || res->status >= 300) {
      return {false, false, "camera service answered " + std::to_string(res->status) +
              " to " + config_.start_path + ": " + firstLine(res->body)};
    }
    return {true, false, "asked the camera to look for '" + object_name + "'"};
  }

  CameraReply status(const std::string & object_name)
  {
    if (config_.base_url.empty()) {
      return {false, false, "camera.base_url is not set"};
    }
    auto cli = makeClient();
    std::string path = config_.status_path;
    if (!config_.status_query_param.empty()) {
      path += "?" + config_.status_query_param + "=" + encode(object_name);
    }
    auto res = cli->Get(path, headers());
    if (!res) {
      return {false, false, "cannot reach the camera service (" +
              std::string(httplib::to_string(res.error())) + ")"};
    }
    if (res->status < 200 || res->status >= 300) {
      return {false, false, "camera service answered " + std::to_string(res->status) +
              " to " + config_.status_path};
    }
    return readFound(res->body, config_.max_age_s, object_name);
  }

  // Turn a status reply body into found / not found. Deliberately tolerant, because the
  // shape is the camera team's to choose; `max_age_s` guards against a stale sighting
  // still reading as FOUND after the camera has stopped looking, and `expect` guards
  // against reading a FOUND that belongs to somebody else's query.
  static CameraReply readFound(
    const std::string & body, double max_age_s = 0.0, const std::string & expect = "")
  {
    nlohmann::json j;
    try {
      j = nlohmann::json::parse(body);
    } catch (const std::exception &) {
      return {false, false, "camera service reply is not JSON: " + firstLine(body)};
    }
    if (j.is_boolean()) {
      return {true, j.get<bool>(), ""};
    }
    // The VLM server's shape: one GLOBAL query, and a `target` that says which query its
    // answer belongs to. The query changes the instant VisualizeObject posts, but the next
    // inference takes seconds, so `target` (and `recent`) still describe the PREVIOUS
    // object until it catches up. Answering from that would report someone else's cup.
    if (j.contains("target") && j["target"].is_object()) {
      const auto & target = j["target"];
      if (j.contains("query") && j["query"].is_object() &&
        j["query"].contains("query_version") && target.contains("query_version") &&
        j["query"]["query_version"] != target["query_version"])
      {
        return {true, false, ""};   // the answer is for an older query; not seen yet
      }
      if (!expect.empty() && target.contains("text") && target["text"].is_string() &&
        target["text"].get<std::string>() != expect)
      {
        return {true, false, ""};   // the camera is looking for something else entirely
      }
      if (target.contains("found") && target["found"].is_boolean()) {
        const bool found = target["found"].get<bool>();
        if (found && max_age_s > 0.0 && !freshEnough(j, max_age_s)) {
          return {true, false, ""};   // stale: the camera has gone quiet
        }
        return {true, found, ""};
      }
    }
    for (const char * key : {"visualized", "found", "detected", "visible", "present"}) {
      if (j.contains(key) && j[key].is_boolean()) {
        return {true, j[key].get<bool>(), ""};
      }
    }
    if (j.contains("status") && j["status"].is_string()) {
      return {true, isFound(j["status"].get<std::string>()), ""};
    }
    // A list of recent sightings, newest last: what the VLM server returns.
    if (j.contains("recent") && j["recent"].is_array() && !j["recent"].empty()) {
      const auto & last = j["recent"].back();
      if (last.contains("status") && last["status"].is_string()) {
        if (max_age_s > 0.0 && last.contains("client_stamp") &&
          last["client_stamp"].is_number())
        {
          const double age = nowSeconds() - last["client_stamp"].get<double>();
          if (age > max_age_s) {
            return {true, false, ""};   // the camera has gone quiet; treat as not seen
          }
        }
        return {true, isFound(last["status"].get<std::string>()), ""};
      }
    }
    if (j.contains("recent") && j["recent"].is_array() && j["recent"].empty()) {
      return {true, false, ""};         // asked, nothing seen yet
    }
    return {false, false, "cannot tell from the camera reply whether the object was seen: " +
            firstLine(body)};
  }

  // Newest sighting young enough to still mean something.
  static bool freshEnough(const nlohmann::json & j, double max_age_s)
  {
    if (!j.contains("recent") || !j["recent"].is_array() || j["recent"].empty()) {
      return true;   // no timestamps to judge by: take the answer at face value
    }
    const auto & last = j["recent"].back();
    if (!last.contains("client_stamp") || !last["client_stamp"].is_number()) {
      return true;
    }
    return nowSeconds() - last["client_stamp"].get<double>() <= max_age_s;
  }

  static bool isFound(std::string s)
  {
    for (auto & c : s) {
      c = static_cast<char>(tolower(c));
    }
    return s == "found" || s == "visualized" || s == "detected" || s == "ok";
  }

  static double nowSeconds()
  {
    return std::chrono::duration<double>(
      std::chrono::system_clock::now().time_since_epoch()).count();
  }

private:
  std::unique_ptr<httplib::Client> makeClient() const
  {
    auto cli = std::make_unique<httplib::Client>(config_.base_url);
    cli->set_connection_timeout(0, config_.timeout_ms * 1000);
    cli->set_read_timeout(0, config_.timeout_ms * 1000);
    cli->set_follow_location(true);
    return cli;
  }

  httplib::Headers headers() const
  {
    if (config_.token.empty()) {
      return {};
    }
    return {{"X-Camera-Token", config_.token}};
  }

  static std::string encode(const std::string & s)
  {
    std::string out;
    for (unsigned char c : s) {
      if (isalnum(c) || c == '-' || c == '_' || c == '.' || c == '~') {
        out += static_cast<char>(c);
      } else {
        char buf[4];
        snprintf(buf, sizeof(buf), "%%%02X", c);
        out += buf;
      }
    }
    return out;
  }

  static std::string firstLine(const std::string & s)
  {
    const auto cut = s.find('\n');
    std::string line = cut == std::string::npos ? s : s.substr(0, cut);
    return line.size() > 200 ? line.substr(0, 200) + "..." : line;
  }

  CameraConfig config_;
};

}  // namespace bt_engine
