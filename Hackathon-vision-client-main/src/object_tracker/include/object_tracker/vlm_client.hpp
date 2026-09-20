#pragma once

#include <rclcpp/rclcpp.hpp>
#include <std_msgs/msg/header.hpp>
#include <opencv2/opencv.hpp>
#include <nlohmann/json.hpp>
#include <zmq.hpp>
#include <array>
#include <atomic>
#include <chrono>
#include <functional>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <vector>

// Client side of docs/vlm_transport.md: send the latest frame as a JPEG to the VLM server over ZeroMQ and turn the
// returned object region into a full-size mono8 mask, stamped with the frame it belongs to. The server returns a
// bbox (and a mask only when VLM_RETURN_MASK=1); a bbox-only answer becomes a filled bbox for the tracker to cut
// out with depth.
//
// Used by vlm_bridge_node (masks published on mask_topic) and mask_tracker_vlm_node (masks handed to the tracker
// in-process). Networking runs in its own thread, so a stalled link never blocks tracking.
// Not implemented (needs tracker state, see docs section 6): "send after N lost frames".

namespace object_tracker {

// Counted per 2 s window
enum class VlmEvent {
    SENT,
    FOUND,
    NOT_FOUND,
    NO_QUERY,
    ERROR,
    TIMEOUT,
    DECODE_FAIL,
};
constexpr size_t kVlmEventCount = static_cast<size_t>(VlmEvent::DECODE_FAIL) + 1;

inline const char *to_string(VlmEvent event) {
    switch (event) {
        case VlmEvent::SENT: return "SENT";
        case VlmEvent::FOUND: return "FOUND";
        case VlmEvent::NOT_FOUND: return "NOT_FOUND";
        case VlmEvent::NO_QUERY: return "NO_QUERY";
        case VlmEvent::ERROR: return "ERROR";
        case VlmEvent::TIMEOUT: return "TIMEOUT";
        case VlmEvent::DECODE_FAIL: return "DECODE_FAIL";
    }
    return "UNKNOWN";
}

class VlmClient {
public:
    // Called on the network thread with a full-size mono8 mask (non-zero = object) and the header of the frame
    // that was sent
    using MaskCallback = std::function<void(const cv::Mat &mask, const std_msgs::msg::Header &header)>;

    // Declares and reads the vlm.* parameters on node. frame_source only names where frames come from in warnings.
    // The network thread starts right away unless vlm.enable is false.
    VlmClient(rclcpp::Node &node, const std::string &frame_source, bool debug, MaskCallback on_mask)
        : logger_(node.get_logger()), clock_(node.get_clock()), frame_source_(frame_source), is_debug_mode_(debug),
          on_mask_(std::move(on_mask)) {
        node.declare_parameter<bool>("vlm.enable", true);
        node.declare_parameter<std::string>("vlm.endpoint", "tcp://192.168.50.125:5555");
        node.declare_parameter<double>("vlm.timeout_s", 6.0);
        node.declare_parameter<double>("vlm.refresh_period_s", 8.0);
        node.declare_parameter<int>("vlm.upload_max_width", 640);
        node.declare_parameter<int>("vlm.jpeg_quality", 80);
        node.declare_parameter<double>("vlm.ping_period_s", 2.0);
        node.declare_parameter<double>("vlm.no_query_backoff_s", 5.0);
        node.declare_parameter<double>("vlm.error_backoff_s", 2.0);
        node.declare_parameter<double>("vlm.max_frame_age_s", 1.0);

        enabled_ = node.get_parameter("vlm.enable").as_bool();
        endpoint_ = node.get_parameter("vlm.endpoint").as_string();
        timeout_s_ = node.get_parameter("vlm.timeout_s").as_double();
        refresh_period_s_ = node.get_parameter("vlm.refresh_period_s").as_double();
        upload_max_width_ = std::max(1, static_cast<int>(node.get_parameter("vlm.upload_max_width").as_int()));
        jpeg_quality_ = std::clamp(static_cast<int>(node.get_parameter("vlm.jpeg_quality").as_int()), 1, 100);
        ping_period_s_ = node.get_parameter("vlm.ping_period_s").as_double();
        no_query_backoff_s_ = node.get_parameter("vlm.no_query_backoff_s").as_double();
        error_backoff_s_ = node.get_parameter("vlm.error_backoff_s").as_double();
        max_frame_age_s_ = node.get_parameter("vlm.max_frame_age_s").as_double();

        if (!enabled_) {
            RCLCPP_WARN(logger_, "vlm.enable is false: no requests are sent");
            return;
        }
        network_thread_ = std::thread(&VlmClient::network_loop, this);
    }

    ~VlmClient() {
        running_ = false;
        if (network_thread_.joinable()) {
            network_thread_.join();
        }
    }

    VlmClient(const VlmClient &) = delete;
    VlmClient &operator=(const VlmClient &) = delete;

    // Latest BGR frame; the next request uses it. The client keeps a reference, so the caller must not write to
    // the image afterwards (pass a copy if it is reused).
    void submit_frame(const cv::Mat &bgr, const std_msgs::msg::Header &header) {
        std::lock_guard<std::mutex> lock(frame_mutex_);
        latest_image_ = bgr;
        latest_header_ = header;
        latest_received_at_ = std::chrono::steady_clock::now();
    }

    bool enabled() const { return enabled_; }
    const std::string &endpoint() const { return endpoint_; }
    double refresh_period_s() const { return refresh_period_s_; }
    double timeout_s() const { return timeout_s_; }

private:
    void record_event(VlmEvent event) {
        std::lock_guard<std::mutex> lock(stats_mutex_);
        ++event_counts_[static_cast<size_t>(event)];
    }

    void record_latency(double encode_ms, double rtt_ms, double server_ms) {
        std::lock_guard<std::mutex> lock(stats_mutex_);
        encode_ms_sum_ += encode_ms;
        rtt_ms_sum_ += rtt_ms;
        server_ms_sum_ += server_ms;
        rtt_ms_max_ = std::max(rtt_ms_max_, rtt_ms);
        ++latency_count_;
    }

    // One line every 2 s, same style as the trackers' "Track stats"
    void report_stats() {
        if (!is_debug_mode_) {
            return;
        }
        const auto now = std::chrono::steady_clock::now();
        if (now - stats_start_ < std::chrono::seconds(2)) {
            return;
        }
        std::lock_guard<std::mutex> lock(stats_mutex_);
        std::string breakdown;
        int total = 0;
        for (size_t i = 0; i < kVlmEventCount; ++i) {
            total += event_counts_[i];
            if (event_counts_[i] > 0) {
                breakdown += cv::format(" %s=%d", to_string(static_cast<VlmEvent>(i)), event_counts_[i]);
            }
        }
        if (total > 0) {
            std::string latency = " no completed request";
            if (latency_count_ > 0) {
                const double rtt = rtt_ms_sum_ / latency_count_;
                const double server = server_ms_sum_ / latency_count_;
                latency = cv::format(" encode %.0f ms | rtt avg %.0f ms max %.0f ms | server %.0f ms | network %.0f ms",
                                     encode_ms_sum_ / latency_count_, rtt, rtt_ms_max_, server, rtt - server);
            }
            RCLCPP_INFO(logger_, "VLM stats:%s | query_version=%d |%s", breakdown.c_str(), query_version_,
                        latency.c_str());
        }
        event_counts_.fill(0);
        encode_ms_sum_ = rtt_ms_sum_ = server_ms_sum_ = rtt_ms_max_ = 0.0;
        latency_count_ = 0;
        stats_start_ = now;
    }

    // Send triggers that need no tracker state (docs section 6, simplified):
    // nothing received yet, refresh period elapsed, after a timeout, or the query version changed.
    // NO_QUERY backs off for vlm.no_query_backoff_s.
    bool should_send(std::chrono::steady_clock::time_point now) const {
        if (now < next_send_at_) {
            return false;
        }
        if (!got_mask_ || query_version_changed_) {
            return true;
        }
        return std::chrono::duration<double>(now - last_sent_at_).count() >= refresh_period_s_;
    }

    void network_loop() {
        zmq::context_t context(1);
        zmq::socket_t socket(context, zmq::socket_type::dealer);
        socket.set(zmq::sockopt::linger, 0);
        socket.set(zmq::sockopt::immediate, 1);  // fail instead of queueing while disconnected
        socket.set(zmq::sockopt::sndhwm, 1);     // at most one request in flight
        socket.set(zmq::sockopt::tcp_keepalive, 1);
        socket.set(zmq::sockopt::tcp_keepalive_idle, 5);
        socket.set(zmq::sockopt::tcp_keepalive_intvl, 2);
        socket.set(zmq::sockopt::reconnect_ivl, 500);
        socket.connect(endpoint_);

        while (running_ && rclcpp::ok()) {
            const auto now = std::chrono::steady_clock::now();
            if (!pending_ && should_send(now)) {
                send_detect(socket);
            } else if (!pending_ && ping_period_s_ > 0.0 &&
                       std::chrono::duration<double>(now - last_ping_at_).count() >= ping_period_s_) {
                send_ping(socket);
            }
            if (pending_ && std::chrono::duration<double>(now - pending_->sent_at).count() > timeout_s_) {
                RCLCPP_WARN(logger_, "Request %u timed out after %.1f s", pending_->request_id, timeout_s_);
                record_event(VlmEvent::TIMEOUT);
                pending_.reset();
                next_send_at_ = now;  // retry right away
            }

            zmq::pollitem_t items[] = {{socket.handle(), 0, ZMQ_POLLIN, 0}};
            zmq::poll(items, 1, std::chrono::milliseconds(50));
            if (items[0].revents & ZMQ_POLLIN) {
                receive(socket);
            }
            report_stats();
        }
    }

    // The frame a request was built from; the response is applied to it
    struct SentFrame {
        std_msgs::msg::Header header;
        cv::Size original_size;
        cv::Size upload_size;
        uint32_t request_id = 0;
        std::chrono::steady_clock::time_point sent_at;
    };

    // JPEG of the latest frame, scaled to vlm.upload_max_width. Fails when no frame arrived yet, or when the
    // latest one arrived more than vlm.max_frame_age_s ago (camera stalled or frames lost): the tracker only caches
    // the last init.cache_s seconds, so a mask for a stale frame would only end as MASK_FRAME_MISSING.
    bool encode_latest(std::vector<uchar> &jpeg, SentFrame &frame, double &encode_ms) {
        cv::Mat image;
        {
            std::lock_guard<std::mutex> lock(frame_mutex_);
            if (latest_image_.empty()) {
                RCLCPP_WARN_THROTTLE(logger_, *clock_, 5000, "Waiting for %s...", frame_source_.c_str());
                return false;
            }
            const double age_s =
                std::chrono::duration<double>(std::chrono::steady_clock::now() - latest_received_at_).count();
            if (max_frame_age_s_ > 0.0 && age_s > max_frame_age_s_) {
                RCLCPP_WARN_THROTTLE(logger_, *clock_, 5000,
                                     "No new frame on %s for %.1f s (last stamp=%.3f), not sending; "
                                     "is the camera still publishing?",
                                     frame_source_.c_str(), age_s, rclcpp::Time(latest_header_.stamp).seconds());
                return false;
            }
            image = latest_image_;
            frame.header = latest_header_;
        }
        const auto t_start = std::chrono::steady_clock::now();
        frame.original_size = image.size();
        cv::Mat upload = image;
        if (image.cols > upload_max_width_) {
            const double scale = static_cast<double>(upload_max_width_) / image.cols;
            cv::resize(image, upload, cv::Size(), scale, scale, cv::INTER_AREA);
        }
        frame.upload_size = upload.size();
        const std::vector<int> params{cv::IMWRITE_JPEG_QUALITY, jpeg_quality_};
        if (!cv::imencode(".jpg", upload, jpeg, params)) {
            return false;
        }
        encode_ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t_start).count();
        return true;
    }

    void send_detect(zmq::socket_t &socket) {
        std::vector<uchar> jpeg;
        SentFrame frame;
        double encode_ms = 0.0;
        if (!encode_latest(jpeg, frame, encode_ms)) {
            next_send_at_ = std::chrono::steady_clock::now() + std::chrono::milliseconds(200);
            return;
        }
        frame.request_id = ++request_id_;
        frame.sent_at = std::chrono::steady_clock::now();

        const nlohmann::json header = {
            {"protocol_version", kProtocolVersion},
            {"type", "detect"},
            {"request_id", frame.request_id},
            {"client_stamp", rclcpp::Time(frame.header.stamp).seconds()},
            {"width", frame.upload_size.width},
            {"height", frame.upload_size.height},
            {"known_query_version", query_version_},
        };
        // ZMQ_IMMEDIATE makes a send fail instead of queueing while disconnected, so never send blocking:
        // a blocking send would sit in the network thread until the server comes back.
        const std::string header_text = header.dump();
        if (!socket.send(zmq::buffer(header_text), zmq::send_flags::sndmore | zmq::send_flags::dontwait)) {
            RCLCPP_WARN_THROTTLE(logger_, *clock_, 5000, "Not connected to %s, retrying", endpoint_.c_str());
            next_send_at_ = std::chrono::steady_clock::now() + std::chrono::milliseconds(500);
            return;
        }
        if (!send_body(socket, jpeg)) {
            RCLCPP_WARN(logger_, "Could not send the image of request %u", frame.request_id);
            next_send_at_ = std::chrono::steady_clock::now() + std::chrono::milliseconds(500);
            return;
        }
        last_encode_ms_ = encode_ms;
        last_sent_at_ = frame.sent_at;
        query_version_changed_ = false;
        pending_ = frame;
        record_event(VlmEvent::SENT);
        RCLCPP_INFO(logger_, "Sent request %u: %dx%d, %zu KB, stamp=%.3f", frame.request_id,
                    frame.upload_size.width, frame.upload_size.height, jpeg.size() / 1024,
                    rclcpp::Time(frame.header.stamp).seconds());
    }

    // Second part of a multipart message; the first part is already queued, so retry briefly before giving up
    bool send_body(zmq::socket_t &socket, const std::vector<uchar> &body) {
        for (int attempt = 0; attempt < 20; ++attempt) {
            if (socket.send(zmq::buffer(body.data(), body.size()), zmq::send_flags::dontwait)) {
                return true;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        return false;
    }

    // Round-trip time without the VLM, and an early look at query_version (docs 4.3)
    void send_ping(zmq::socket_t &socket) {
        const nlohmann::json header = {
            {"protocol_version", kProtocolVersion},
            {"type", "ping"},
            {"request_id", ++request_id_},
            {"known_query_version", query_version_},
        };
        // dontwait: while disconnected the ping is simply skipped
        socket.send(zmq::buffer(header.dump()), zmq::send_flags::dontwait);
        last_ping_at_ = std::chrono::steady_clock::now();
    }

    void receive(zmq::socket_t &socket) {
        std::vector<zmq::message_t> frames;
        while (true) {
            zmq::message_t part;
            if (!socket.recv(part, zmq::recv_flags::none)) {
                return;
            }
            const bool more = part.more();
            frames.push_back(std::move(part));
            if (!more) {
                break;
            }
        }
        if (frames.empty()) {
            return;
        }

        nlohmann::json header;
        try {
            header = nlohmann::json::parse(frames[0].to_string());
        } catch (const nlohmann::json::exception &e) {
            RCLCPP_WARN(logger_, "Bad response header: %s", e.what());
            record_event(VlmEvent::DECODE_FAIL);
            return;
        }
        const int version = header.value("query_version", query_version_);
        if (version != query_version_) {
            // learning the first version (-1 = unknown) is not a description change; resending would waste an
            // inference (about 2 s on the GPU)
            if (query_version_ >= 0) {
                RCLCPP_INFO(logger_, "Query version %d -> %d, sending a new image", query_version_, version);
                query_version_changed_ = true;
            }
            query_version_ = version;
        }
        const std::string status = header.value("status", std::string("ERROR"));
        if (status == "PONG") {
            return;
        }

        const uint32_t response_id = header.value("request_id", 0u);
        if (!pending_ || response_id != pending_->request_id) {
            // a late response from a request we already gave up on (docs section 5)
            const std::string waiting = pending_ ? std::to_string(pending_->request_id) : std::string("nothing");
            RCLCPP_INFO(logger_, "Dropping response %u (waiting for %s)", response_id, waiting.c_str());
            return;
        }
        const SentFrame frame = *pending_;
        pending_.reset();
        const double rtt_ms =
            std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - frame.sent_at).count();
        record_latency(last_encode_ms_, rtt_ms, header.value("server_ms", 0));

        if (status == "FOUND") {
            if (deliver_mask(header, frames, frame)) {
                record_event(VlmEvent::FOUND);
                got_mask_ = true;
            } else {
                record_event(VlmEvent::DECODE_FAIL);
            }
        } else if (status == "NOT_FOUND") {
            record_event(VlmEvent::NOT_FOUND);
            RCLCPP_INFO(logger_, "Request %u: target not in the image", response_id);
        } else if (status == "NO_QUERY") {
            record_event(VlmEvent::NO_QUERY);
            RCLCPP_WARN(logger_, "Server has no query yet, backing off %.1f s", no_query_backoff_s_);
            next_send_at_ = std::chrono::steady_clock::now() +
                            std::chrono::milliseconds(static_cast<int>(no_query_backoff_s_ * 1000));
        } else {
            // e.g. "model loading", which the server answers right away: back off instead of resending in a loop
            record_event(VlmEvent::ERROR);
            RCLCPP_WARN(logger_, "Server error: %s, retrying in %.1f s",
                        header.value("error", std::string("(no message)")).c_str(), error_backoff_s_);
            next_send_at_ = std::chrono::steady_clock::now() +
                            std::chrono::milliseconds(static_cast<int>(error_backoff_s_ * 1000));
        }
    }

    // bbox [x1, y1, x2, y2] (x2, y2 exclusive) and the optional mask are in upload coordinates; scale them back
    // and paste into a full-size mono8 mask. Without a mask (the server default) the whole bbox is used:
    // the tracker's mask cleaning then cuts the object out with depth (median depth gate + largest region),
    // which is what docs section 5 asks the client to do.
    bool deliver_mask(const nlohmann::json &header, const std::vector<zmq::message_t> &frames,
                      const SentFrame &frame) {
        std::array<int, 4> bbox{0, 0, 0, 0};
        try {
            const auto &values = header.at("bbox");
            if (!values.is_array() || values.size() != 4) {
                RCLCPP_WARN(logger_, "FOUND without a valid bbox");
                return false;
            }
            for (size_t i = 0; i < 4; ++i) {
                bbox[i] = values[i].get<int>();
            }
        } catch (const nlohmann::json::exception &e) {
            RCLCPP_WARN(logger_, "Bad bbox: %s", e.what());
            return false;
        }
        const int box_w = bbox[2] - bbox[0];
        const int box_h = bbox[3] - bbox[1];
        if (box_w <= 0 || box_h <= 0) {
            RCLCPP_WARN(logger_, "Empty bbox [%d, %d, %d, %d]", bbox[0], bbox[1], bbox[2], bbox[3]);
            return false;
        }

        const bool has_mask = header.value("has_mask", false);
        cv::Mat mask_roi;
        if (has_mask) {
            if (frames.size() < 2) {
                RCLCPP_WARN(logger_, "has_mask is true but the mask frame is missing");
                return false;
            }
            const std::vector<uchar> png(static_cast<const uchar *>(frames[1].data()),
                                         static_cast<const uchar *>(frames[1].data()) + frames[1].size());
            mask_roi = cv::imdecode(png, cv::IMREAD_GRAYSCALE);
            if (mask_roi.empty()) {
                RCLCPP_WARN(logger_, "Could not decode the mask PNG");
                return false;
            }
            if (mask_roi.size() != cv::Size(box_w, box_h)) {
                RCLCPP_WARN(logger_, "Mask %dx%d does not match bbox %dx%d", mask_roi.cols, mask_roi.rows, box_w,
                            box_h);
                return false;
            }
        } else {
            mask_roi = cv::Mat(box_h, box_w, CV_8UC1, cv::Scalar(255));
        }

        // docs 4.4: original = upload * (original width / upload width)
        const double scale = static_cast<double>(frame.original_size.width) / frame.upload_size.width;
        const cv::Point tl(static_cast<int>(std::lround(bbox[0] * scale)), static_cast<int>(std::lround(bbox[1] * scale)));
        const cv::Point br(static_cast<int>(std::lround(bbox[2] * scale)), static_cast<int>(std::lround(bbox[3] * scale)));
        const cv::Rect target(tl, br);
        if (target.width <= 0 || target.height <= 0) {
            RCLCPP_WARN(logger_, "bbox collapses after scaling");
            return false;
        }
        cv::Mat scaled;
        cv::resize(mask_roi, scaled, target.size(), 0, 0, cv::INTER_NEAREST);
        const cv::Rect clipped = target & cv::Rect(cv::Point(0, 0), frame.original_size);
        if (clipped.width <= 0 || clipped.height <= 0) {
            RCLCPP_WARN(logger_, "bbox is outside the image");
            return false;
        }
        cv::Mat mask = cv::Mat::zeros(frame.original_size, CV_8UC1);
        scaled(cv::Rect(clipped.tl() - target.tl(), clipped.size())).copyTo(mask(clipped));

        // the mask belongs to the frame that was sent, not to now
        on_mask_(mask, frame.header);
        RCLCPP_INFO(logger_,
                    "Mask published for request %u: stamp=%.3f bbox=[%d, %d, %d, %d] (%s) pixels=%d candidates=%d score=%.2f",
                    frame.request_id, rclcpp::Time(frame.header.stamp).seconds(), clipped.x, clipped.y,
                    clipped.x + clipped.width, clipped.y + clipped.height, has_mask ? "server mask" : "bbox only",
                    cv::countNonZero(mask), header.value("num_candidates", 1), header.value("score", -1.0));
        return true;
    }

    static constexpr int kProtocolVersion = 1;

    rclcpp::Logger logger_;
    rclcpp::Clock::SharedPtr clock_;
    std::string frame_source_;
    bool is_debug_mode_ = true;
    MaskCallback on_mask_;

    bool enabled_ = true;
    std::string endpoint_;
    double timeout_s_ = 6.0;
    double refresh_period_s_ = 8.0;
    int upload_max_width_ = 640;
    int jpeg_quality_ = 80;
    double ping_period_s_ = 2.0;
    double no_query_backoff_s_ = 5.0;
    double error_backoff_s_ = 2.0;
    double max_frame_age_s_ = 1.0;

    std::mutex frame_mutex_;
    cv::Mat latest_image_;
    std_msgs::msg::Header latest_header_;
    std::chrono::steady_clock::time_point latest_received_at_{};

    std::thread network_thread_;
    std::atomic<bool> running_{true};
    std::optional<SentFrame> pending_;  // network thread only
    uint32_t request_id_ = 0;
    int query_version_ = -1;
    bool query_version_changed_ = false;
    bool got_mask_ = false;
    double last_encode_ms_ = 0.0;
    std::chrono::steady_clock::time_point last_sent_at_{};
    std::chrono::steady_clock::time_point last_ping_at_{};
    std::chrono::steady_clock::time_point next_send_at_{};

    std::mutex stats_mutex_;
    std::array<int, kVlmEventCount> event_counts_{};
    double encode_ms_sum_ = 0.0;
    double rtt_ms_sum_ = 0.0;
    double server_ms_sum_ = 0.0;
    double rtt_ms_max_ = 0.0;
    int latency_count_ = 0;
    std::chrono::steady_clock::time_point stats_start_ = std::chrono::steady_clock::now();
};

}  // namespace object_tracker
