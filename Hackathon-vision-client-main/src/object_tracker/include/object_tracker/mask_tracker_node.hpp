#pragma once

#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/image.hpp>
#include <sensor_msgs/msg/camera_info.hpp>
#include <sensor_msgs/image_encodings.hpp>
#include <geometry_msgs/msg/point_stamped.hpp>
#include <geometry_msgs/msg/transform_stamped.hpp>
#include <geometry_msgs/msg/vector3_stamped.hpp>
#include <cv_bridge/cv_bridge.h>
#include <image_transport/image_transport.hpp>
#include <message_filters/subscriber.h>
#include <message_filters/synchronizer.h>
#include <message_filters/sync_policies/approximate_time.h>
#include <tf2_ros/buffer.h>
#include <tf2_ros/transform_listener.h>
#include <tf2_ros/transform_broadcaster.h>
#include <tf2_geometry_msgs/tf2_geometry_msgs.hpp>
#include <opencv2/opencv.hpp>
#include <opencv2/features2d.hpp>
#include <opencv2/video/tracking.hpp>
#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <deque>
#include <functional>
#include <optional>
#include <string>

#include "object_tracker/mask_tracker_algorithms.hpp"

// Tracker for non-planar targets: a mask from the upstream VLM builds an ORB model, KLT points follow the object
// between detections, a similarity transform (not a homography) only keeps the region and rejects outliers, and
// the output is the mask's geometric center moved by that transform, at the object's measured depth.
//
// Header-only so mask_tracker_node (masks from mask_topic) and mask_tracker_vlm_node (built-in VLM client) share it.

using ImageMsg = sensor_msgs::msg::Image;
using Point3 = std::array<double, 3>;
using SyncPolicy = message_filters::sync_policies::ApproximateTime<ImageMsg, ImageMsg>;

using object_tracker::CleanedMask;
using object_tracker::DepthSample;

// Per-frame outcome; each failure value maps to one early exit of the pipeline
enum class TrackStatus {
    OK,
    WAITING_INTRINSICS,
    IMAGE_ERROR,
    NO_MODEL,
    KLT_FEW_POINTS,
    NO_FEATURES,
    FEW_MATCHES,
    NO_TRANSFORM,
    FEW_INLIERS,
    LOW_INLIER_RATIO,
    HIGH_REPROJ_ERROR,
    BAD_SCALE,
    NO_DEPTH,
    TF_FAIL,
    SEARCH_IDLE,
    JUMP_UNCONFIRMED,
};
constexpr size_t kTrackStatusCount = static_cast<size_t>(TrackStatus::JUMP_UNCONFIRMED) + 1;

// What the published output is based on (independent of this frame's TrackStatus)
enum class TrackState {
    LOST,
    CONFIRMING,
    TRACKING,
    HOLDING,
};
constexpr size_t kTrackStateCount = static_cast<size_t>(TrackState::HOLDING) + 1;

// Where this frame's detection came from
enum class TrackSource {
    KLT,
    ORB_ROI,
    ORB_FULL,
};
constexpr size_t kTrackSourceCount = static_cast<size_t>(TrackSource::ORB_FULL) + 1;

// Mask init events; counted separately so they never replace a frame's TrackStatus
enum class InitEvent {
    MASK_RECEIVED,
    MASK_FRAME_MISSING,
    MASK_BAD_SIZE,
    MASK_TOO_SMALL,
    MASK_FEW_FEATURES,
    HANDOFF_OK,
    HANDOFF_TIMEOUT,
    HANDOFF_REPLACED,
};
constexpr size_t kInitEventCount = static_cast<size_t>(InitEvent::HANDOFF_REPLACED) + 1;

inline const char *to_string(TrackStatus status) {
    switch (status) {
        case TrackStatus::OK: return "OK";
        case TrackStatus::WAITING_INTRINSICS: return "WAITING_INTRINSICS";
        case TrackStatus::IMAGE_ERROR: return "IMAGE_ERROR";
        case TrackStatus::NO_MODEL: return "NO_MODEL";
        case TrackStatus::KLT_FEW_POINTS: return "KLT_FEW_POINTS";
        case TrackStatus::NO_FEATURES: return "NO_FEATURES";
        case TrackStatus::FEW_MATCHES: return "FEW_MATCHES";
        case TrackStatus::NO_TRANSFORM: return "NO_TRANSFORM";
        case TrackStatus::FEW_INLIERS: return "FEW_INLIERS";
        case TrackStatus::LOW_INLIER_RATIO: return "LOW_INLIER_RATIO";
        case TrackStatus::HIGH_REPROJ_ERROR: return "HIGH_REPROJ_ERROR";
        case TrackStatus::BAD_SCALE: return "BAD_SCALE";
        case TrackStatus::NO_DEPTH: return "NO_DEPTH";
        case TrackStatus::TF_FAIL: return "TF_FAIL";
        case TrackStatus::SEARCH_IDLE: return "SEARCH_IDLE";
        case TrackStatus::JUMP_UNCONFIRMED: return "JUMP_UNCONFIRMED";
    }
    return "UNKNOWN";
}

inline const char *to_string(TrackState state) {
    switch (state) {
        case TrackState::LOST: return "LOST";
        case TrackState::CONFIRMING: return "CONFIRMING";
        case TrackState::TRACKING: return "TRACKING";
        case TrackState::HOLDING: return "HOLDING";
    }
    return "UNKNOWN";
}

inline const char *to_string(TrackSource source) {
    switch (source) {
        case TrackSource::KLT: return "KLT";
        case TrackSource::ORB_ROI: return "ORB_ROI";
        case TrackSource::ORB_FULL: return "ORB_FULL";
    }
    return "UNKNOWN";
}

inline const char *to_string(InitEvent event) {
    switch (event) {
        case InitEvent::MASK_RECEIVED: return "MASK_RECEIVED";
        case InitEvent::MASK_FRAME_MISSING: return "MASK_FRAME_MISSING";
        case InitEvent::MASK_BAD_SIZE: return "MASK_BAD_SIZE";
        case InitEvent::MASK_TOO_SMALL: return "MASK_TOO_SMALL";
        case InitEvent::MASK_FEW_FEATURES: return "MASK_FEW_FEATURES";
        case InitEvent::HANDOFF_OK: return "HANDOFF_OK";
        case InitEvent::HANDOFF_TIMEOUT: return "HANDOFF_TIMEOUT";
        case InitEvent::HANDOFF_REPLACED: return "HANDOFF_REPLACED";
    }
    return "UNKNOWN";
}

// Object model built from a mask. Reference coordinates are pixels of the frame the mask belongs to.
struct MaskModel {
    std::vector<cv::Point2f> keypoints_ref;
    cv::Mat descriptors;
    std::vector<cv::Point2f> polygon_ref;  // outer contour of the cleaned mask
    cv::Point2f center_ref;                // mask centroid (nearest mask pixel when the centroid is outside)
    std::optional<double> d0;              // median depth inside the mask; none when it had no in-range depth
    double source_stamp_s = 0.0;
};

// A KLT point: where it is on the model and where it was last seen
struct TrackPoint {
    cv::Point2f ref;
    cv::Point2f pos;
};

struct Detection {
    TrackStatus status = TrackStatus::OK;
    TrackSource source = TrackSource::ORB_FULL;
    cv::Mat S;                   // 2x3 similarity, reference -> current frame
    double scale = 0.0;
    int n_candidates = 0;        // ORB: ratio-test matches, KLT: points passing the forward-backward check
    int n_inliers = 0;
    double inlier_ratio = 0.0;
    double reproj_error = 0.0;   // mean inlier transfer error under S (px)
    std::vector<TrackPoint> inliers;
    std::vector<cv::Point2f> region;           // model polygon in the current frame
    std::vector<cv::Point2f> depth_rejected;   // debug: KLT points dropped by the depth gate
    std::vector<cv::Point2f> region_rejected;  // debug: KLT points dropped outside the region
};

// 3D point of this frame in the camera optical frame
struct Output3D {
    TrackStatus status = TrackStatus::OK;  // OK or NO_DEPTH
    Point3 cam_pt{};
    cv::Point2f pixel;                     // image position of the output point
    bool depth_fallback = false;           // Z is depth_fallback.value_m, not a measurement
};

// Recent frame kept so a delayed mask can be applied to the frame it was computed on
struct CachedFrame {
    double stamp_s = 0.0;
    cv::Mat gray;
    cv::Mat depth;
    std::string depth_encoding;
    std::string frame_id;  // color optical frame
};

// Candidate model from a mask, waiting to be found in the current frame
struct PendingHandoff {
    MaskModel model;
    double start_stamp_s = 0.0;  // newest frame stamp when the mask arrived
    double mask_stamp_s = 0.0;
};

// Last output we trust, kept while HOLDING
struct TrustedTrack {
    Point3 world_pt{};  // filtered, in world_frame
    std::vector<cv::Point2f> region;
    double stamp_s = 0.0;  // image stamp
};

class MaskTrackerNode : public rclcpp::Node {
public:
    // subscribe_mask_topic = false: masks only come in through handle_mask() (mask_tracker_vlm_node)
    explicit MaskTrackerNode(const std::string &node_name = "mask_tracker_node", bool subscribe_mask_topic = true)
        : Node(node_name) {
        this->declare_parameter<std::string>("color_topic", "/camera_duck/camera/color/image_rect_raw");
        this->declare_parameter<std::string>("depth_topic", "/camera_duck/camera/aligned_depth_to_color/image_raw");
        this->declare_parameter<std::string>("camera_info_topic", "/camera_duck/camera/color/camera_info");
        this->declare_parameter<std::string>("output_topic", "/tracked_object/point");
        this->declare_parameter<std::string>("world_frame", "map");
        this->declare_parameter<std::string>("mask_topic", "/tracked_object/init_mask");
        this->declare_parameter<double>("init.cache_s", 3.0);
        this->declare_parameter<double>("init.stamp_tolerance_s", 0.005);
        this->declare_parameter<int>("init.mask_erode_px", 4);
        this->declare_parameter<int>("init.min_mask_px", 400);
        this->declare_parameter<double>("init.handoff_timeout_s", 2.0);
        this->declare_parameter<int>("orb.n_features_model", 500);
        this->declare_parameter<int>("orb.n_features", 500);
        this->declare_parameter<int>("orb.n_features_full", 1000);
        this->declare_parameter<int>("orb.fast_threshold", 20);
        this->declare_parameter<double>("ratio_test", 0.8);
        this->declare_parameter<int>("min_matches", 10);
        this->declare_parameter<int>("min_inliers", 8);
        this->declare_parameter<double>("ransac_reproj_thresh", 4.0);
        this->declare_parameter<double>("gate.min_inlier_ratio", 0.3);
        this->declare_parameter<double>("gate.max_reproj_error_px", 3.0);
        this->declare_parameter<double>("gate.max_scale_ratio", 3.0);
        this->declare_parameter<double>("gate.max_scale_change", 1.2);
        this->declare_parameter<double>("depth_gate_m", 0.04);
        this->declare_parameter<int>("min_depth_points", 5);
        this->declare_parameter<std::string>("output.center_mode", "mask_center");
        this->declare_parameter<int>("output.center_depth_window", 5);
        this->declare_parameter<bool>("size.enable", true);
        this->declare_parameter<std::string>("size.topic", "/tracked_object/size");
        this->declare_parameter<double>("depth_min_m", 0.07);
        this->declare_parameter<double>("depth_max_m", 0.5);
        this->declare_parameter<bool>("depth_fallback.enable", true);
        this->declare_parameter<double>("depth_fallback.value_m", 0.5);
        this->declare_parameter<double>("depth_fallback.max_region_area_px", 0.0);
        this->declare_parameter<double>("roi.expand_ratio", 0.5);
        this->declare_parameter<int>("roi.min_size_px", 160);
        this->declare_parameter<int>("klt.max_points", 100);
        this->declare_parameter<int>("klt.min_points", 10);
        this->declare_parameter<int>("klt.refill_below", 40);
        this->declare_parameter<double>("klt.fb_max_px", 1.0);
        this->declare_parameter<int>("klt.win_size", 21);
        this->declare_parameter<int>("klt.max_level", 3);
        this->declare_parameter<double>("klt.region_margin_px", 10.0);
        this->declare_parameter<double>("search.full_after_s", 0.5);
        this->declare_parameter<int>("search.full_interval", 5);
        this->declare_parameter<double>("tf_timeout_s", 0.05);
        this->declare_parameter<bool>("pose_filter.enable", true);
        this->declare_parameter<double>("pose_filter.alpha", 0.3);
        this->declare_parameter<double>("pose_filter.max_jump_m", 0.15);
        this->declare_parameter<bool>("hold.enable", true);
        this->declare_parameter<double>("hold.timeout_s", 0.0);
        this->declare_parameter<int>("hold.confirm_frames", 3);
        this->declare_parameter<bool>("hold.publish", true);
        this->declare_parameter<bool>("debug.enable", true);
        this->declare_parameter<bool>("debug.img", false);
        this->declare_parameter<std::string>("debug.image_topic", "/tracked_object/debug_image");
        this->declare_parameter<double>("debug.image_rate_hz", 5.0);
        this->declare_parameter<bool>("debug.window", false);

        const std::string color_topic = this->get_parameter("color_topic").as_string();
        const std::string depth_topic = this->get_parameter("depth_topic").as_string();
        const std::string info_topic = this->get_parameter("camera_info_topic").as_string();
        const std::string output_topic = this->get_parameter("output_topic").as_string();
        const std::string mask_topic = this->get_parameter("mask_topic").as_string();
        world_frame_ = this->get_parameter("world_frame").as_string();
        init_cache_s_ = this->get_parameter("init.cache_s").as_double();
        init_stamp_tolerance_s_ = this->get_parameter("init.stamp_tolerance_s").as_double();
        init_mask_erode_px_ = std::max(0, static_cast<int>(this->get_parameter("init.mask_erode_px").as_int()));
        init_min_mask_px_ = static_cast<int>(this->get_parameter("init.min_mask_px").as_int());
        init_handoff_timeout_s_ = this->get_parameter("init.handoff_timeout_s").as_double();
        ratio_test_ = this->get_parameter("ratio_test").as_double();
        min_matches_ = static_cast<int>(this->get_parameter("min_matches").as_int());
        min_inliers_ = static_cast<int>(this->get_parameter("min_inliers").as_int());
        ransac_reproj_thresh_ = this->get_parameter("ransac_reproj_thresh").as_double();
        gate_min_inlier_ratio_ = this->get_parameter("gate.min_inlier_ratio").as_double();
        gate_max_reproj_error_px_ = this->get_parameter("gate.max_reproj_error_px").as_double();
        gate_max_scale_ratio_ = std::max(1.0, this->get_parameter("gate.max_scale_ratio").as_double());
        gate_max_scale_change_ = std::max(1.0, this->get_parameter("gate.max_scale_change").as_double());
        depth_gate_m_ = this->get_parameter("depth_gate_m").as_double();
        min_depth_points_ = std::max(1, static_cast<int>(this->get_parameter("min_depth_points").as_int()));
        const std::string center_mode = this->get_parameter("output.center_mode").as_string();
        if (center_mode != "mask_center" && center_mode != "features") {
            RCLCPP_WARN(this->get_logger(), "Unknown output.center_mode '%s', using mask_center", center_mode.c_str());
        }
        output_features_center_ = (center_mode == "features");
        // sample_depth_window keeps at most 64 values, so 7x7 is the largest window that is fully used
        output_center_depth_window_ =
            std::clamp(static_cast<int>(this->get_parameter("output.center_depth_window").as_int()), 1, 7);
        depth_min_m_ = this->get_parameter("depth_min_m").as_double();
        depth_max_m_ = this->get_parameter("depth_max_m").as_double();
        depth_fallback_enable_ = this->get_parameter("depth_fallback.enable").as_bool();
        depth_fallback_value_m_ = this->get_parameter("depth_fallback.value_m").as_double();
        depth_fallback_max_region_area_px_ = this->get_parameter("depth_fallback.max_region_area_px").as_double();
        roi_expand_ratio_ = std::max(0.0, this->get_parameter("roi.expand_ratio").as_double());
        roi_min_size_px_ = std::max(0, static_cast<int>(this->get_parameter("roi.min_size_px").as_int()));
        klt_max_points_ = std::max(0, static_cast<int>(this->get_parameter("klt.max_points").as_int()));
        // estimateAffinePartial2D needs at least 2 point pairs
        klt_min_points_ = std::max(2, static_cast<int>(this->get_parameter("klt.min_points").as_int()));
        klt_refill_below_ = static_cast<int>(this->get_parameter("klt.refill_below").as_int());
        klt_fb_max_px_ = this->get_parameter("klt.fb_max_px").as_double();
        klt_win_size_ = std::max(3, static_cast<int>(this->get_parameter("klt.win_size").as_int()));
        klt_max_level_ = std::max(0, static_cast<int>(this->get_parameter("klt.max_level").as_int()));
        klt_region_margin_px_ = this->get_parameter("klt.region_margin_px").as_double();
        search_full_after_s_ = this->get_parameter("search.full_after_s").as_double();
        search_full_interval_ = std::max(1, static_cast<int>(this->get_parameter("search.full_interval").as_int()));
        tf_timeout_s_ = this->get_parameter("tf_timeout_s").as_double();
        pose_filter_enable_ = this->get_parameter("pose_filter.enable").as_bool();
        hold_enable_ = this->get_parameter("hold.enable").as_bool();
        hold_timeout_s_ = this->get_parameter("hold.timeout_s").as_double();
        hold_confirm_frames_ = std::max(1, static_cast<int>(this->get_parameter("hold.confirm_frames").as_int()));
        hold_publish_ = this->get_parameter("hold.publish").as_bool();
        is_debug_mode_ = this->get_parameter("debug.enable").as_bool();
        image_debug_ = this->get_parameter("debug.img").as_bool();
        debug_image_rate_hz_ = this->get_parameter("debug.image_rate_hz").as_double();
        debug_window_ = this->get_parameter("debug.window").as_bool();

        const int fast_threshold = static_cast<int>(this->get_parameter("orb.fast_threshold").as_int());
        auto make_orb = [fast_threshold](int n_features) {
            return cv::ORB::create(std::max(1, n_features), 1.2f, 8, object_tracker::kOrbEdgeThreshold, 0, 2,
                                   cv::ORB::HARRIS_SCORE, 31, fast_threshold);
        };
        orb_model_ = make_orb(static_cast<int>(this->get_parameter("orb.n_features_model").as_int()));
        orb_ = make_orb(static_cast<int>(this->get_parameter("orb.n_features").as_int()));
        orb_full_ = make_orb(static_cast<int>(this->get_parameter("orb.n_features_full").as_int()));
        matcher_ = cv::BFMatcher::create(cv::NORM_HAMMING, false);

        tf_buffer_ = std::make_unique<tf2_ros::Buffer>(this->get_clock());
        tf_listener_ = std::make_shared<tf2_ros::TransformListener>(*tf_buffer_);
        tf_broadcaster_ = std::make_unique<tf2_ros::TransformBroadcaster>(*this);

        info_sub_ = this->create_subscription<sensor_msgs::msg::CameraInfo>(
            info_topic, rclcpp::SensorDataQoS(),
            std::bind(&MaskTrackerNode::camera_info_callback, this, std::placeholders::_1));
        color_sub_.subscribe(this, color_topic, rmw_qos_profile_sensor_data);
        depth_sub_.subscribe(this, depth_topic, rmw_qos_profile_sensor_data);
        sync_ = std::make_shared<message_filters::Synchronizer<SyncPolicy>>(SyncPolicy(10), color_sub_, depth_sub_);
        sync_->registerCallback(std::bind(&MaskTrackerNode::rgbd_callback, this,
                                          std::placeholders::_1, std::placeholders::_2));
        if (subscribe_mask_topic) {
            mask_sub_ = this->create_subscription<ImageMsg>(
                mask_topic, rclcpp::QoS(rclcpp::KeepLast(5)).reliable(),
                std::bind(&MaskTrackerNode::mask_callback, this, std::placeholders::_1));
        }

        point_pub_ = this->create_publisher<geometry_msgs::msg::PointStamped>(output_topic, 10);
        if (this->get_parameter("size.enable").as_bool()) {
            // one message per VLM answer; transient_local so a gripper node started later still gets the last one
            size_pub_ = this->create_publisher<geometry_msgs::msg::Vector3Stamped>(
                this->get_parameter("size.topic").as_string(), rclcpp::QoS(rclcpp::KeepLast(1)).reliable().transient_local());
        }
        if (image_debug_) {
            const std::string debug_topic = this->get_parameter("debug.image_topic").as_string();
            debug_image_pub_ = image_transport::create_publisher(this, debug_topic);
            RCLCPP_INFO(this->get_logger(), "Debug image on %s (and %s/compressed), up to %.1f Hz%s",
                        debug_topic.c_str(), debug_topic.c_str(), debug_image_rate_hz_,
                        debug_window_ ? ", plus a window" : "");
        }

        const std::string mask_source = subscribe_mask_topic ? "on " + mask_topic : "from the built-in VLM client";
        RCLCPP_INFO(this->get_logger(), "No model yet, waiting for a mask %s; color=%s depth=%s, frame cache %.1f s",
                    mask_source.c_str(), color_topic.c_str(), depth_topic.c_str(), init_cache_s_);
    }

    ~MaskTrackerNode() override = default;

protected:
    // Called in rgbd_callback before this frame is processed (executor thread)
    virtual void before_frame() {}

    // Called with every frame that was processed and cached, after its point was published (executor thread).
    // color is BGR and only valid during the call.
    virtual void on_frame(const cv::Mat & /*color*/, const std_msgs::msg::Header & /*header*/) {}

    void camera_info_callback(const sensor_msgs::msg::CameraInfo::SharedPtr msg) {
        if (msg->k[0] <= 0.0 || msg->k[4] <= 0.0) {
            return;
        }
        fx_ = msg->k[0];
        fy_ = msg->k[4];
        cx_ = msg->k[2];
        cy_ = msg->k[5];
        if (!has_intrinsics_) {
            RCLCPP_INFO(this->get_logger(), "Camera intrinsics: fx=%.1f fy=%.1f cx=%.1f cy=%.1f", fx_, fy_, cx_, cy_);
        }
        has_intrinsics_ = true;
    }

    // ---- Mask init ----

    void record_event(InitEvent event, double mask_stamp_s, const std::string &detail) {
        ++event_counts_[static_cast<size_t>(event)];
        RCLCPP_INFO(this->get_logger(), "Init event %s: mask_stamp=%.3f %s", to_string(event), mask_stamp_s,
                    detail.c_str());
    }

    // Ring buffer of the last init.cache_s seconds of processed frames (image stamps).
    // Memory = frames * (W * H gray + W * H * depth bytes per pixel); at 848x480 with 16UC1 depth that is
    // 0.41 MB + 0.81 MB = 1.22 MB per frame, so 3 s at 30 fps (90 frames) is about 110 MB.
    void cache_frame(const cv::Mat &gray, const cv::Mat &depth, const std::string &depth_encoding,
                     const std::string &frame_id, double stamp_s) {
        if (!frame_cache_.empty() && stamp_s < frame_cache_.back().stamp_s) {
            // stamps went backwards (e.g. a restarted bag); old frames can no longer match a mask
            frame_cache_.clear();
        }
        frame_cache_.push_back(CachedFrame{stamp_s, gray.clone(), depth.clone(), depth_encoding, frame_id});
        while (stamp_s - frame_cache_.front().stamp_s > init_cache_s_) {
            frame_cache_.pop_front();
        }
    }

    // Closest cached frame within init.stamp_tolerance_s of the mask stamp, or nullptr
    const CachedFrame *find_cached_frame(double stamp_s) const {
        const CachedFrame *best = nullptr;
        double best_diff = init_stamp_tolerance_s_;
        for (const auto &frame : frame_cache_) {
            const double diff = std::abs(frame.stamp_s - stamp_s);
            if (diff <= best_diff) {
                best = &frame;
                best_diff = diff;
            }
        }
        return best;
    }

    void mask_callback(const ImageMsg::ConstSharedPtr &msg) {
        const double mask_stamp_s = rclcpp::Time(msg->header.stamp).seconds();
        record_event(InitEvent::MASK_RECEIVED, mask_stamp_s, cv::format("size=%ux%u", msg->width, msg->height));

        cv::Mat mask;
        try {
            mask = cv_bridge::toCvShare(msg, sensor_msgs::image_encodings::MONO8)->image;
        } catch (cv_bridge::Exception &e) {
            RCLCPP_ERROR(this->get_logger(), "Mask cv_bridge exception (expected mono8): %s", e.what());
            return;
        }
        handle_mask(mask, mask_stamp_s);
    }

    // Builds a candidate model from a full-size mono8 mask (non-zero = object) whose stamp is the color frame it
    // was computed on; it is tried on the next processed frame (handoff), since only the image callback has a
    // current frame. Must run on the executor thread (it touches the frame cache and the pending candidate).
    void handle_mask(const cv::Mat &mask, double mask_stamp_s) {
        const CachedFrame *frame = find_cached_frame(mask_stamp_s);
        if (!frame) {
            record_event(InitEvent::MASK_FRAME_MISSING, mask_stamp_s,
                         frame_cache_.empty()
                             ? std::string("cache empty")
                             : cv::format("cache %zu frames [%.3f, %.3f], tolerance %.3f s", frame_cache_.size(),
                                          frame_cache_.front().stamp_s, frame_cache_.back().stamp_s,
                                          init_stamp_tolerance_s_));
            return;
        }
        if (mask.size() != frame->gray.size()) {
            record_event(InitEvent::MASK_BAD_SIZE, mask_stamp_s,
                         cv::format("mask %dx%d, frame %dx%d", mask.cols, mask.rows, frame->gray.cols, frame->gray.rows));
            return;
        }

        std::optional<MaskModel> candidate = build_model(*frame, mask, mask_stamp_s);
        if (!candidate) {
            return;  // the current model stays
        }
        if (pending_handoff_) {
            record_event(InitEvent::HANDOFF_REPLACED, mask_stamp_s,
                         cv::format("replaces mask_stamp=%.3f", pending_handoff_->mask_stamp_s));
        }
        pending_handoff_ = PendingHandoff{std::move(*candidate), frame_cache_.back().stamp_s, mask_stamp_s};
    }

    // Clean the mask on its source frame and build an ORB model inside it (reference = source frame pixels)
    std::optional<MaskModel> build_model(const CachedFrame &frame, const cv::Mat &mask, double mask_stamp_s) {
        const bool is_mm = object_tracker::is_depth_mm(frame.depth_encoding);
        const CleanedMask cleaned =
            object_tracker::clean_mask(mask, frame.depth, is_mm, depth_min_m_, depth_max_m_, depth_gate_m_);
        if (cleaned.pixels < init_min_mask_px_) {
            record_event(InitEvent::MASK_TOO_SMALL, mask_stamp_s,
                         cv::format("pixels=%d (min %d), depth_removed=%d", cleaned.pixels, init_min_mask_px_,
                                    cleaned.depth_removed));
            return std::nullopt;
        }
        const std::string gate_text = cleaned.d0 ? cv::format("d0=%.3fm", *cleaned.d0) : std::string("skipped");
        RCLCPP_INFO(this->get_logger(), "Mask cleaned: mask_stamp=%.3f pixels=%d depth_gate=%s removed=%d",
                    mask_stamp_s, cleaned.pixels, gate_text.c_str(), cleaned.depth_removed);
        publish_size(frame, mask, cleaned.d0, mask_stamp_s);

        MaskModel model;
        model.source_stamp_s = mask_stamp_s;
        model.d0 = cleaned.d0;
        model.polygon_ref = object_tracker::mask_polygon(cleaned.mask);
        model.center_ref = object_tracker::mask_center(cleaned.mask);
        // features only well inside the mask, where they are least likely to belong to the background
        std::vector<cv::KeyPoint> keypoints;
        orb_model_->detectAndCompute(frame.gray, object_tracker::erode_mask(cleaned.mask, init_mask_erode_px_),
                                     keypoints, model.descriptors);
        if (static_cast<int>(keypoints.size()) < min_matches_ || model.polygon_ref.size() < 3) {
            record_event(InitEvent::MASK_FEW_FEATURES, mask_stamp_s,
                         cv::format("features=%zu (min %d), current model kept", keypoints.size(), min_matches_));
            return std::nullopt;
        }
        for (const auto &kp : keypoints) {
            model.keypoints_ref.push_back(kp.pt);
        }
        RCLCPP_INFO(this->get_logger(), "Mask model candidate: mask_stamp=%.3f features=%zu polygon=%zu pts, "
                    "waiting for handoff", mask_stamp_s, keypoints.size(), model.polygon_ref.size());
        return model;
    }

    // Object size from the VLM bbox (bounding rect of the incoming mask, i.e. the bbox itself for a bbox-only
    // answer) at the median object depth d0: size = pixels * d0 / f. Published once per mask, not per frame.
    // Vector3Stamped: x = width (image u direction), y = height (image v direction), z = d0; all in metres.
    void publish_size(const CachedFrame &frame, const cv::Mat &mask, const std::optional<double> &d0,
                      double mask_stamp_s) {
        if (!size_pub_) {
            return;
        }
        if (!d0 || !has_intrinsics_) {
            RCLCPP_WARN(this->get_logger(), "Size not published for mask_stamp=%.3f: %s", mask_stamp_s,
                        d0 ? "no camera_info yet" : "no in-range depth in the mask");
            return;
        }
        const cv::Rect box = cv::boundingRect(mask);
        const bool at_edge = box.x <= 0 || box.y <= 0 || box.x + box.width >= mask.cols ||
                             box.y + box.height >= mask.rows;
        geometry_msgs::msg::Vector3Stamped msg;
        msg.header.stamp = rclcpp::Time(static_cast<int64_t>(std::llround(mask_stamp_s * 1e9)), RCL_ROS_TIME);
        msg.header.frame_id = frame.frame_id;
        msg.vector.x = box.width * *d0 / fx_;
        msg.vector.y = box.height * *d0 / fy_;
        msg.vector.z = *d0;
        size_pub_->publish(msg);
        RCLCPP_INFO(this->get_logger(), "Object size: mask_stamp=%.3f width=%.3fm height=%.3fm depth=%.3fm "
                    "bbox=%dx%d px%s", mask_stamp_s, msg.vector.x, msg.vector.y, *d0, box.width, box.height,
                    at_edge ? " (bbox touches the image edge, size is a lower bound)" : "");
    }

    // Look for the pending candidate in the whole current frame. While a candidate waits this is one extra
    // full-frame ORB pass per frame, which noticeably raises the processing time on a Raspberry Pi 5.
    std::optional<Detection> try_handoff(const cv::Mat &gray, double stamp_s) {
        if (!pending_handoff_) {
            return std::nullopt;
        }
        Detection det = detect_orb(pending_handoff_->model, gray, cv::Rect(cv::Point(0, 0), gray.size()),
                                   TrackSource::ORB_FULL);
        if (det.status == TrackStatus::OK) {
            return det;
        }
        if (stamp_s - pending_handoff_->start_stamp_s > init_handoff_timeout_s_) {
            record_event(InitEvent::HANDOFF_TIMEOUT, pending_handoff_->mask_stamp_s,
                         cv::format("waited %.2f s, last status %s", stamp_s - pending_handoff_->start_stamp_s,
                                    to_string(det.status)));
            pending_handoff_.reset();
        }
        return std::nullopt;
    }

    // Switch to the candidate just found in this frame and restart the KLT points from this detection.
    // The state change happens in handoff_track_state() once this frame's 3D output is known.
    void apply_handoff(const cv::Mat &gray, const cv::Mat &depth, bool is_mm, const Detection &det,
                       double stamp_s) {
        const double mask_stamp_s = pending_handoff_->mask_stamp_s;
        model_ = std::move(pending_handoff_->model);
        pending_handoff_.reset();

        clear_search_state();
        d_obj_ = model_->d0;
        prev_gray_ = gray;
        last_region_ = det.region;
        prev_scale_ = det.scale;
        seed_klt(gray, depth, is_mm, det.S, det.inliers);

        const double latency_ms = (stamp_s - mask_stamp_s) * 1000.0;
        handoff_latency_ms_sum_ += latency_ms;
        ++handoff_latency_count_;
        last_handoff_stamp_s_ = stamp_s;
        record_event(InitEvent::HANDOFF_OK, mask_stamp_s,
                     cv::format("latency=%.0f ms features=%zu inliers=%d klt_points=%zu", latency_ms,
                                model_->keypoints_ref.size(), det.n_inliers, tracks_.size()));
    }

    // ---- Detection ----

    // Gates shared by ORB and KLT; expects n_inliers, inlier_ratio, reproj_error and scale to be set
    void check_gates(int min_inliers, Detection &det) const {
        if (det.n_inliers < min_inliers) {
            det.status = TrackStatus::FEW_INLIERS;
        } else if (det.inlier_ratio < gate_min_inlier_ratio_) {
            det.status = TrackStatus::LOW_INLIER_RATIO;
        } else if (det.reproj_error > gate_max_reproj_error_px_) {
            det.status = TrackStatus::HIGH_REPROJ_ERROR;
        } else if (det.scale < 1.0 / gate_max_scale_ratio_ || det.scale > gate_max_scale_ratio_) {
            det.status = TrackStatus::BAD_SCALE;
        } else {
            det.status = TrackStatus::OK;
        }
    }

    // ORB in roi (the whole image for ORB_FULL) + ratio test + similarity RANSAC + gates. Depth is not checked here.
    Detection detect_orb(const MaskModel &model, const cv::Mat &gray, const cv::Rect &roi, TrackSource source) {
        Detection det;
        det.source = source;
        std::vector<cv::KeyPoint> scene_keypoints;
        cv::Mat scene_descriptors;
        const cv::Ptr<cv::ORB> &orb = (source == TrackSource::ORB_FULL) ? orb_full_ : orb_;
        orb->detectAndCompute(gray(roi), cv::noArray(), scene_keypoints, scene_descriptors);
        if (scene_descriptors.empty() || scene_keypoints.size() < 2) {
            det.status = TrackStatus::NO_FEATURES;
            return det;
        }

        std::vector<std::vector<cv::DMatch>> knn_matches;
        matcher_->knnMatch(model.descriptors, scene_descriptors, knn_matches, 2);
        const cv::Point2f roi_offset(static_cast<float>(roi.x), static_cast<float>(roi.y));
        std::vector<cv::Point2f> ref_pts, cur_pts;
        for (const auto &m : knn_matches) {
            if (m.size() == 2 && m[0].distance < ratio_test_ * m[1].distance) {
                ref_pts.push_back(model.keypoints_ref[m[0].queryIdx]);
                cur_pts.push_back(scene_keypoints[m[0].trainIdx].pt + roi_offset);
            }
        }
        det.n_candidates = static_cast<int>(ref_pts.size());
        if (det.n_candidates < std::max(2, min_matches_)) {
            det.status = TrackStatus::FEW_MATCHES;
            return det;
        }

        std::vector<uchar> inlier_mask;
        det.S = cv::estimateAffinePartial2D(ref_pts, cur_pts, inlier_mask, cv::RANSAC, ransac_reproj_thresh_);
        if (det.S.empty()) {
            det.status = TrackStatus::NO_TRANSFORM;
            return det;
        }
        std::vector<cv::Point2f> in_ref, in_cur;
        for (size_t i = 0; i < inlier_mask.size(); ++i) {
            if (inlier_mask[i]) {
                det.inliers.push_back(TrackPoint{ref_pts[i], cur_pts[i]});
                in_ref.push_back(ref_pts[i]);
                in_cur.push_back(cur_pts[i]);
            }
        }
        det.scale = object_tracker::similarity_scale(det.S);
        det.n_inliers = static_cast<int>(det.inliers.size());
        det.inlier_ratio = static_cast<double>(det.n_inliers) / det.n_candidates;
        det.reproj_error = object_tracker::mean_transfer_error(in_ref, in_cur, det.S);
        det.region = object_tracker::apply_transform(model.polygon_ref, det.S);
        check_gates(min_inliers_, det);
        return det;
    }

    // Forward-backward LK -> similarity RANSAC -> depth gate against d_obj -> region gate -> gates.
    // tracks_ is not modified here.
    Detection track_klt(const cv::Mat &gray, const cv::Mat &depth, bool is_mm) {
        Detection det;
        det.source = TrackSource::KLT;

        std::vector<cv::Point2f> prev_pts;
        prev_pts.reserve(tracks_.size());
        for (const auto &t : tracks_) {
            prev_pts.push_back(t.pos);
        }
        const cv::Size win(klt_win_size_, klt_win_size_);
        std::vector<cv::Point2f> next_pts, back_pts;
        std::vector<uchar> status_fwd, status_bwd;
        std::vector<float> err;
        cv::calcOpticalFlowPyrLK(prev_gray_, gray, prev_pts, next_pts, status_fwd, err, win, klt_max_level_);
        cv::calcOpticalFlowPyrLK(gray, prev_gray_, next_pts, back_pts, status_bwd, err, win, klt_max_level_);

        std::vector<cv::Point2f> ref_pts, cur_pts;
        for (size_t i = 0; i < prev_pts.size(); ++i) {
            if (status_fwd[i] && status_bwd[i] && cv::norm(back_pts[i] - prev_pts[i]) <= klt_fb_max_px_) {
                ref_pts.push_back(tracks_[i].ref);
                cur_pts.push_back(next_pts[i]);
            }
        }
        det.n_candidates = static_cast<int>(ref_pts.size());
        if (det.n_candidates < klt_min_points_) {
            // rejections below only remove points, so this cannot recover
            det.status = TrackStatus::KLT_FEW_POINTS;
            return det;
        }

        std::vector<uchar> inlier_mask;
        det.S = cv::estimateAffinePartial2D(ref_pts, cur_pts, inlier_mask, cv::RANSAC, ransac_reproj_thresh_);
        if (det.S.empty()) {
            det.status = TrackStatus::NO_TRANSFORM;
            return det;
        }
        det.scale = object_tracker::similarity_scale(det.S);
        det.region = object_tracker::apply_transform(model_->polygon_ref, det.S);

        std::vector<cv::Point2f> in_ref, in_cur;
        for (size_t i = 0; i < inlier_mask.size(); ++i) {
            if (!inlier_mask[i]) {
                continue;
            }
            const cv::Point2f &p = cur_pts[i];
            // depth gate: points without measured depth stay (they are just not used for the 3D output)
            if (d_obj_) {
                const std::optional<double> d = object_tracker::measured_depth_window(
                    depth, is_mm, static_cast<int>(std::lround(p.x)), static_cast<int>(std::lround(p.y)), 3);
                if (d && std::abs(*d - *d_obj_) > depth_gate_m_) {
                    det.depth_rejected.push_back(p);
                    continue;
                }
            }
            // region gate: the similarity only approximates a non-planar object, hence the margin
            if (cv::pointPolygonTest(det.region, p, true) < -klt_region_margin_px_) {
                det.region_rejected.push_back(p);
                continue;
            }
            det.inliers.push_back(TrackPoint{ref_pts[i], p});
            in_ref.push_back(ref_pts[i]);
            in_cur.push_back(p);
        }
        det.n_inliers = static_cast<int>(det.inliers.size());
        det.inlier_ratio = static_cast<double>(det.n_inliers) / det.n_candidates;
        det.reproj_error = object_tracker::mean_transfer_error(in_ref, in_cur, det.S);
        if (det.n_inliers < klt_min_points_) {
            det.status = TrackStatus::KLT_FEW_POINTS;
            return det;
        }
        check_gates(klt_min_points_, det);
        if (det.status == TrackStatus::OK && prev_scale_ &&
            std::max(det.scale / *prev_scale_, *prev_scale_ / det.scale) > gate_max_scale_change_) {
            det.status = TrackStatus::BAD_SCALE;
        }
        return det;
    }

    // Add KLT points: the given ORB inliers first, then goodFeaturesToTrack corners inside the model region moved
    // by S and shrunk by init.mask_erode_px, up to klt.max_points in total. Corners whose measured depth is far
    // from d_obj are skipped. Reference coordinates come from S^-1.
    void seed_klt(const cv::Mat &gray, const cv::Mat &depth, bool is_mm, const cv::Mat &S,
                  const std::vector<TrackPoint> &orb_inliers) {
        for (const auto &p : orb_inliers) {
            if (static_cast<int>(tracks_.size()) >= klt_max_points_) {
                break;
            }
            tracks_.push_back(p);
        }
        const int n_new = klt_max_points_ - static_cast<int>(tracks_.size());
        if (n_new <= 0 || S.empty()) {
            return;
        }

        const std::vector<cv::Point> polygon =
            object_tracker::to_int_polygon(object_tracker::apply_transform(model_->polygon_ref, S));
        const cv::Rect box = cv::boundingRect(polygon) & cv::Rect(cv::Point(0, 0), gray.size());
        if (box.width <= 0 || box.height <= 0) {
            return;
        }
        cv::Mat region_mask = cv::Mat::zeros(box.size(), CV_8UC1);
        cv::fillPoly(region_mask, std::vector<std::vector<cv::Point>>{polygon}, cv::Scalar(255), cv::LINE_8, 0, -box.tl());
        region_mask = object_tracker::erode_mask(region_mask, init_mask_erode_px_);
        for (const auto &t : tracks_) {
            // keep new corners away from existing points
            cv::circle(region_mask, cv::Point(t.pos) - box.tl(), 7, cv::Scalar(0), -1);
        }
        std::vector<cv::Point2f> corners;
        cv::goodFeaturesToTrack(gray(box), corners, n_new, 0.01, 7.0, region_mask);
        if (corners.empty()) {
            return;
        }
        for (auto &c : corners) {
            c += cv::Point2f(static_cast<float>(box.x), static_cast<float>(box.y));
        }
        cv::Mat S_inv;
        cv::invertAffineTransform(S, S_inv);
        const std::vector<cv::Point2f> refs = object_tracker::apply_transform(corners, S_inv);
        for (size_t i = 0; i < corners.size(); ++i) {
            if (d_obj_) {
                const std::optional<double> d = object_tracker::measured_depth_window(
                    depth, is_mm, static_cast<int>(std::lround(corners[i].x)),
                    static_cast<int>(std::lround(corners[i].y)), 3);
                if (d && std::abs(*d - *d_obj_) > depth_gate_m_) {
                    continue;
                }
            }
            tracks_.push_back(TrackPoint{refs[i], corners[i]});
        }
    }

    // Search near the last region (KLT / ROI) while TRACKING, or while HOLDING for at most search.full_after_s
    bool has_search_anchor(double stamp_s) const {
        return state_ == TrackState::TRACKING ||
               (state_ == TrackState::HOLDING && stamp_s - trusted_.stamp_s <= search_full_after_s_);
    }

    void clear_klt_points() {
        tracks_.clear();
    }

    // Everything tied to the last seen position; the model and the published output are kept
    void clear_search_state() {
        clear_klt_points();
        last_region_.clear();
        prev_scale_.reset();
    }

    // Per-frame flow with a model: KLT near the anchor, ORB in the ROI when KLT fails, and without an anchor
    // a full-frame ORB every search.full_interval frames (SEARCH_IDLE in between)
    Detection detect_frame(const cv::Mat &gray, const cv::Mat &depth, bool is_mm, double stamp_s) {
        debug_roi_.reset();
        const bool anchor = has_search_anchor(stamp_s);
        full_search_ = !anchor;
        if (full_search_) {
            if (!was_full_search_) {
                full_search_counter_ = 0;  // search right away when the anchor is lost
            }
            clear_search_state();
        }
        was_full_search_ = full_search_;

        Detection det;
        bool have_result = false;
        if (anchor && !tracks_.empty() && !prev_gray_.empty()) {
            det = track_klt(gray, depth, is_mm);
            if (det.status == TrackStatus::OK) {
                tracks_ = det.inliers;
                if (static_cast<int>(tracks_.size()) < klt_refill_below_) {
                    seed_klt(gray, depth, is_mm, det.S, {});
                }
                have_result = true;
            } else {
                clear_klt_points();
            }
        }

        if (!have_result) {
            std::optional<Detection> orb;
            const cv::Rect full_frame(cv::Point(0, 0), gray.size());
            if (anchor) {
                debug_roi_ = object_tracker::expand_roi(last_region_, gray.size(), roi_expand_ratio_, roi_min_size_px_);
                orb = debug_roi_ ? detect_orb(*model_, gray, *debug_roi_, TrackSource::ORB_ROI)
                                 : detect_orb(*model_, gray, full_frame, TrackSource::ORB_FULL);
            } else if (full_search_counter_++ % search_full_interval_ == 0) {
                orb = detect_orb(*model_, gray, full_frame, TrackSource::ORB_FULL);
            }
            if (orb) {
                det = std::move(*orb);
                if (det.status == TrackStatus::OK) {
                    clear_klt_points();
                    seed_klt(gray, depth, is_mm, det.S, det.inliers);
                }
            } else {
                det = Detection{};
                det.status = TrackStatus::SEARCH_IDLE;
            }
        }

        if (det.status == TrackStatus::OK) {
            last_region_ = det.region;
            prev_scale_ = det.scale;
        }
        prev_gray_ = gray;
        return det;
    }

    // ---- 3D output ----

    // Output point of this frame. Direction: the mask's geometric center moved by S (output.center_mode
    // "mask_center"), so it stays on the object's axis even when the texture is all on one side; "features" uses
    // the median of the inlier points instead (old behavior). Depth: measured at the center when it lies within
    // depth_gate_m of the inliers' median depth, else that median (center on a depth hole or on background).
    // With too few inliers with real depth, a default depth along the same ray when the object looks too far
    // rather than too near. Updates d_obj from the inliers' real depth only.
    Output3D compute_output(const Detection &det, const cv::Mat &depth, bool is_mm) {
        if (output_features_center_) {
            return compute_output_features(det, depth, is_mm);
        }
        Output3D out;
        std::vector<double> zs;
        int n_near = 0;
        int n_far = 0;
        for (const auto &p : det.inliers) {
            const DepthSample sample = object_tracker::sample_depth_window(
                depth, is_mm, static_cast<int>(std::lround(p.pos.x)), static_cast<int>(std::lround(p.pos.y)), 3,
                depth_min_m_, depth_max_m_);
            n_near += sample.n_near;
            n_far += sample.n_far;
            if (sample.median_m) {
                zs.push_back(*sample.median_m);
            }
        }
        const cv::Point2f center = object_tracker::apply_transform({model_->center_ref}, det.S).front();
        auto deproject = [&](double Z) {
            out.pixel = center;
            out.cam_pt = {(center.x - cx_) * Z / fx_, (center.y - cy_) * Z / fy_, Z};
        };

        if (static_cast<int>(zs.size()) >= min_depth_points_) {
            const double z_features = object_tracker::median_of(zs);
            double Z = z_features;
            const DepthSample at_center = object_tracker::sample_depth_window(
                depth, is_mm, static_cast<int>(std::lround(center.x)), static_cast<int>(std::lround(center.y)),
                output_center_depth_window_, depth_min_m_, depth_max_m_);
            if (at_center.median_m && std::abs(*at_center.median_m - z_features) <= depth_gate_m_) {
                Z = *at_center.median_m;
            }
            deproject(Z);
            d_obj_ = z_features;
            return out;
        }
        if (n_near > n_far) {
            // mostly too near or unreliable: a default far depth would be wrong
            out.status = TrackStatus::NO_DEPTH;
            return out;
        }
        const bool area_ok = depth_fallback_max_region_area_px_ <= 0.0 ||
                             (det.region.size() >= 3 && cv::contourArea(det.region) <= depth_fallback_max_region_area_px_);
        if (depth_fallback_enable_ && area_ok && !det.inliers.empty()) {
            // too far for D405: the point keeps the right ray direction, the distance is a guess
            deproject(depth_fallback_value_m_);
            out.depth_fallback = true;
            return out;
        }
        out.status = TrackStatus::NO_DEPTH;
        return out;
    }

    // output.center_mode "features": median camera-frame point of the inliers with real depth; with too few of
    // them, a default depth along the median pixel when the object looks too far rather than too near.
    // Leans toward the side with more texture.
    Output3D compute_output_features(const Detection &det, const cv::Mat &depth, bool is_mm) {
        Output3D out;
        std::vector<double> xs, ys, zs, us, vs;
        int n_near = 0;
        int n_far = 0;
        for (const auto &p : det.inliers) {
            us.push_back(p.pos.x);
            vs.push_back(p.pos.y);
            const DepthSample sample = object_tracker::sample_depth_window(
                depth, is_mm, static_cast<int>(std::lround(p.pos.x)), static_cast<int>(std::lround(p.pos.y)), 3,
                depth_min_m_, depth_max_m_);
            n_near += sample.n_near;
            n_far += sample.n_far;
            if (sample.median_m) {
                const double Z = *sample.median_m;
                xs.push_back((p.pos.x - cx_) * Z / fx_);
                ys.push_back((p.pos.y - cy_) * Z / fy_);
                zs.push_back(Z);
            }
        }

        if (static_cast<int>(zs.size()) >= min_depth_points_) {
            out.cam_pt = {object_tracker::median_of(xs), object_tracker::median_of(ys), object_tracker::median_of(zs)};
            out.pixel = cv::Point2f(static_cast<float>(fx_ * out.cam_pt[0] / out.cam_pt[2] + cx_),
                                    static_cast<float>(fy_ * out.cam_pt[1] / out.cam_pt[2] + cy_));
            d_obj_ = out.cam_pt[2];
            return out;
        }
        if (n_near > n_far) {
            // mostly too near or unreliable: a default far depth would be wrong
            out.status = TrackStatus::NO_DEPTH;
            return out;
        }
        const bool area_ok = depth_fallback_max_region_area_px_ <= 0.0 ||
                             (det.region.size() >= 3 && cv::contourArea(det.region) <= depth_fallback_max_region_area_px_);
        if (depth_fallback_enable_ && area_ok && !us.empty()) {
            // too far for D405: the point keeps the right ray direction, the distance is a guess
            out.pixel = cv::Point2f(static_cast<float>(object_tracker::median_of(us)),
                                    static_cast<float>(object_tracker::median_of(vs)));
            const double Z = depth_fallback_value_m_;
            out.cam_pt = {(out.pixel.x - cx_) * Z / fx_, (out.pixel.y - cy_) * Z / fy_, Z};
            out.depth_fallback = true;
            return out;
        }
        out.status = TrackStatus::NO_DEPTH;
        return out;
    }

    bool transform_to_world(const geometry_msgs::msg::PointStamped &in, geometry_msgs::msg::PointStamped &out) {
        try {
            out = tf_buffer_->transform(in, world_frame_, tf2::durationFromSec(tf_timeout_s_));
            return true;
        } catch (const tf2::TransformException &) {
            // fall back to the latest available transform (static camera mount)
        }
        try {
            auto tf_msg = tf_buffer_->lookupTransform(world_frame_, in.header.frame_id, tf2::TimePointZero);
            tf2::doTransform(in, out, tf_msg);
            out.header.stamp = in.header.stamp;
            return true;
        } catch (const tf2::TransformException &ex) {
            RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 2000,
                                 "TF %s -> %s failed: %s", in.header.frame_id.c_str(), world_frame_.c_str(), ex.what());
            return false;
        }
    }

    // ---- State machine (copied from orb_tracker_node) ----

    // snap_on_jump keeps the legacy outlier guard; the hold state machine confirms jumps instead
    void point_filter(const double raw[3], double filtered[3], bool snap_on_jump) {
        std::copy(raw, raw + 3, filtered);
        if (!pose_filter_enable_) {
            pose_filter_initialized_ = false;
            return;
        }

        // keep filter tunable without restart
        const double alpha = std::clamp(this->get_parameter("pose_filter.alpha").as_double(), 0.0, 1.0);
        const double max_jump = this->get_parameter("pose_filter.max_jump_m").as_double();

        if (!pose_filter_initialized_) {
            std::copy(raw, raw + 3, pose_filtered_);
            pose_filter_initialized_ = true;
        } else {
            const double dx = raw[0] - pose_filtered_[0];
            const double dy = raw[1] - pose_filtered_[1];
            const double dz = raw[2] - pose_filtered_[2];
            if (snap_on_jump && max_jump > 0.0 && std::sqrt(dx * dx + dy * dy + dz * dz) > max_jump) {
                // outlier guard: snap to measurement on big jumps
                std::copy(raw, raw + 3, pose_filtered_);
            } else {
                for (int i = 0; i < 3; ++i) {
                    pose_filtered_[i] = alpha * raw[i] + (1.0 - alpha) * pose_filtered_[i];
                }
            }
        }
        std::copy(pose_filtered_, pose_filtered_ + 3, filtered);
    }

    // true if b is within pose_filter.max_jump_m of a (always true when the limit is disabled)
    bool within_jump(const Point3 &a, const Point3 &b) {
        const double max_jump = this->get_parameter("pose_filter.max_jump_m").as_double();
        const double dx = b[0] - a[0];
        const double dy = b[1] - a[1];
        const double dz = b[2] - a[2];
        return max_jump <= 0.0 || std::sqrt(dx * dx + dy * dy + dz * dz) <= max_jump;
    }

    // Count consecutive consistent measurements; true once hold.confirm_frames is reached
    bool confirm_candidate(const Point3 &measurement) {
        if (candidate_count_ > 0 && within_jump(candidate_pt_, measurement)) {
            ++candidate_count_;
        } else {
            candidate_count_ = 1;
        }
        candidate_pt_ = measurement;
        return candidate_count_ >= hold_confirm_frames_;
    }

    void accept_track(const Point3 &world_pt, const Detection &det, double stamp_s) {
        trusted_.world_pt = world_pt;
        trusted_.region = det.region;
        trusted_.stamp_s = stamp_s;
        candidate_count_ = 0;
        state_ = TrackState::TRACKING;
    }

    // Enter TRACKING at the confirmed candidate, re-initializing the filter there
    Point3 start_track(const Detection &det, double stamp_s) {
        const Point3 start_pt = candidate_pt_;
        std::copy(start_pt.begin(), start_pt.end(), pose_filtered_);
        pose_filter_initialized_ = pose_filter_enable_;
        accept_track(start_pt, det, stamp_s);
        return start_pt;
    }

    void set_lost() {
        state_ = TrackState::LOST;
        trusted_ = TrustedTrack{};
        candidate_count_ = 0;
        pose_filter_initialized_ = false;
    }

    std::optional<Point3> hold_output() const {
        if (!hold_publish_) {
            return std::nullopt;
        }
        return trusted_.world_pt;
    }

    // true once the trusted point is older than hold.timeout_s; never when the timeout is <= 0
    bool hold_expired(double stamp_s) const {
        return hold_timeout_s_ > 0.0 && stamp_s - trusted_.stamp_s > hold_timeout_s_;
    }

    // Hold / confirm state machine. measurement is this frame's raw world point (only when status is OK).
    // May turn status into JUMP_UNCONFIRMED. Returns the world point to publish, if any.
    // Difference from orb_tracker_node: a SEARCH_IDLE frame did not look for the object, so it keeps a running
    // confirmation instead of breaking it (otherwise full search every N frames could never confirm).
    std::optional<Point3> update_track_state(TrackStatus &status, const Detection &det,
                                             const std::optional<Point3> &measurement, double stamp_s) {
        if (!hold_enable_) {
            // legacy behavior: publish every measurement, filter snaps on big jumps
            if (!measurement) {
                state_ = TrackState::LOST;
                return std::nullopt;
            }
            state_ = TrackState::TRACKING;
            Point3 filtered;
            point_filter(measurement->data(), filtered.data(), true);
            return filtered;
        }

        const bool has_track = (state_ == TrackState::TRACKING || state_ == TrackState::HOLDING);
        const bool idle = (status == TrackStatus::SEARCH_IDLE);

        if (!measurement) {
            if (has_track && !hold_expired(stamp_s)) {
                state_ = TrackState::HOLDING;
                if (!idle) {
                    candidate_count_ = 0;
                }
                return hold_output();
            }
            if (idle && state_ == TrackState::CONFIRMING) {
                return std::nullopt;
            }
            set_lost();
            return std::nullopt;
        }

        if (has_track) {
            if (within_jump(trusted_.world_pt, *measurement)) {
                Point3 filtered;
                point_filter(measurement->data(), filtered.data(), false);
                accept_track(filtered, det, stamp_s);
                return filtered;
            }
            if (confirm_candidate(*measurement)) {
                return start_track(det, stamp_s);
            }
            status = TrackStatus::JUMP_UNCONFIRMED;
            if (hold_expired(stamp_s)) {
                // trusted point expired while the jump is still unconfirmed; keep confirming the candidate
                trusted_ = TrustedTrack{};
                pose_filter_initialized_ = false;
                state_ = TrackState::CONFIRMING;
                return std::nullopt;
            }
            state_ = TrackState::HOLDING;
            return hold_output();
        }

        // LOST or CONFIRMING
        if (state_ == TrackState::LOST) {
            candidate_count_ = 0;
        }
        if (confirm_candidate(*measurement)) {
            return start_track(det, stamp_s);
        }
        state_ = TrackState::CONFIRMING;
        return std::nullopt;
    }

    // After a handoff the VLM mask is trusted: TRACKING right away (no CONFIRMING, no jump confirmation), or LOST
    // when this frame has no measurement, since the held point belongs to the old object
    std::optional<Point3> handoff_track_state(const std::optional<Point3> &measurement, const Detection &det,
                                              double stamp_s) {
        if (!measurement) {
            set_lost();
            return std::nullopt;
        }
        std::copy(measurement->begin(), measurement->end(), pose_filtered_);
        pose_filter_initialized_ = pose_filter_enable_;
        accept_track(*measurement, det, stamp_s);
        return measurement;
    }

    void publish_point(const Point3 &pt, const std_msgs::msg::Header &image_header) {
        geometry_msgs::msg::PointStamped msg;
        msg.header.stamp = image_header.stamp;
        msg.header.frame_id = world_frame_;
        msg.point.x = pt[0];
        msg.point.y = pt[1];
        msg.point.z = pt[2];
        point_pub_->publish(msg);

        if (is_debug_mode_) {
            geometry_msgs::msg::TransformStamped t;
            t.header = msg.header;
            t.child_frame_id = "tracked_object";
            t.transform.translation.x = pt[0];
            t.transform.translation.y = pt[1];
            t.transform.translation.z = pt[2];
            t.transform.rotation.w = 1.0;
            tf_broadcaster_->sendTransform(t);
        }
    }

    // ---- Stats ----

    // Debug only: throttled failure reason, and every 2 s the per-status / state / source frame counts,
    // depth fallback use, processing time and mask events
    void report_status(TrackStatus status, TrackState state, std::optional<TrackSource> source,
                       std::optional<double> process_ms, bool depth_fallback) {
        if (!is_debug_mode_ || status == TrackStatus::WAITING_INTRINSICS) {
            return;
        }
        if (status != TrackStatus::OK && status != TrackStatus::SEARCH_IDLE && status != TrackStatus::NO_MODEL) {
            RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 1000, "Track failed: %s", to_string(status));
        }

        ++status_counts_[static_cast<size_t>(status)];
        ++state_counts_[static_cast<size_t>(state)];
        if (source) {
            ++source_counts_[static_cast<size_t>(*source)];
        }
        if (process_ms) {
            process_ms_sum_ += *process_ms;
            process_ms_max_ = std::max(process_ms_max_, *process_ms);
            ++process_count_;
        }
        if (depth_fallback) {
            ++depth_fallback_count_;
        }
        const auto now = std::chrono::steady_clock::now();
        if (now - stats_start_ < std::chrono::seconds(2)) {
            return;
        }

        auto breakdown = [](const auto &counts, size_t n, auto name) {
            std::string text;
            for (size_t i = 0; i < n; ++i) {
                if (counts[i] > 0) {
                    text += cv::format(" %s=%d", name(i), counts[i]);
                }
            }
            return text;
        };
        int total = 0;
        for (int c : status_counts_) {
            total += c;
        }
        const std::string status_text = breakdown(status_counts_, kTrackStatusCount,
                                                  [](size_t i) { return to_string(static_cast<TrackStatus>(i)); });
        const std::string state_text = breakdown(state_counts_, kTrackStateCount,
                                                 [](size_t i) { return to_string(static_cast<TrackState>(i)); });
        const std::string source_text = breakdown(source_counts_, kTrackSourceCount,
                                                  [](size_t i) { return to_string(static_cast<TrackSource>(i)); });
        std::string init_text = breakdown(event_counts_, kInitEventCount,
                                          [](size_t i) { return to_string(static_cast<InitEvent>(i)); });
        init_text += handoff_latency_count_ > 0
                         ? cv::format(" handoff_latency_avg=%.0fms", handoff_latency_ms_sum_ / handoff_latency_count_)
                         : std::string(" handoff_latency_avg=-");
        const double avg_ms = process_count_ > 0 ? process_ms_sum_ / process_count_ : 0.0;
        const int n_ok = status_counts_[static_cast<size_t>(TrackStatus::OK)];
        RCLCPP_INFO(this->get_logger(),
                    "Track stats: %d frames, success %.1f%% |%s | state:%s | source:%s | depth_fb=%d"
                    " | time: avg %.1f ms, max %.1f ms | init:%s",
                    total, 100.0 * n_ok / total, status_text.c_str(), state_text.c_str(), source_text.c_str(),
                    depth_fallback_count_, avg_ms, process_ms_max_, init_text.c_str());
        status_counts_.fill(0);
        state_counts_.fill(0);
        source_counts_.fill(0);
        event_counts_.fill(0);
        process_ms_sum_ = 0.0;
        process_ms_max_ = 0.0;
        process_count_ = 0;
        depth_fallback_count_ = 0;
        handoff_latency_ms_sum_ = 0.0;
        handoff_latency_count_ = 0;
        stats_start_ = now;
    }

    // ---- Main callback ----

    void rgbd_callback(const ImageMsg::ConstSharedPtr &color_msg, const ImageMsg::ConstSharedPtr &depth_msg) {
        before_frame();
        if (!has_intrinsics_) {
            RCLCPP_INFO_THROTTLE(this->get_logger(), *this->get_clock(), 2000, "Waiting for camera_info...");
            report_status(TrackStatus::WAITING_INTRINSICS, state_, std::nullopt, std::nullopt, false);
            return;
        }

        // image errors are failed frames too, so a HOLDING track can ride through them
        TrackStatus status = TrackStatus::OK;
        cv::Mat color, depth;
        try {
            color = cv_bridge::toCvShare(color_msg, sensor_msgs::image_encodings::BGR8)->image;
            depth = cv_bridge::toCvShare(depth_msg)->image;
        } catch (cv_bridge::Exception &e) {
            RCLCPP_ERROR(this->get_logger(), "cv_bridge exception: %s", e.what());
            status = TrackStatus::IMAGE_ERROR;
        }
        if (status == TrackStatus::OK && depth.size() != color.size()) {
            RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 5000,
                                 "Depth %dx%d != color %dx%d, is align_depth enabled?",
                                 depth.cols, depth.rows, color.cols, color.rows);
            status = TrackStatus::IMAGE_ERROR;
        }

        const double stamp_s = rclcpp::Time(color_msg->header.stamp).seconds();
        const bool is_mm = object_tracker::is_depth_mm(depth_msg->encoding);
        Detection det;
        cv::Mat gray;
        bool searched = false;       // a detection ran this frame (not idle, not without a model)
        bool handoff_found = false;
        std::optional<double> process_ms;
        if (status == TrackStatus::OK) {
            const auto t_start = std::chrono::steady_clock::now();
            cv::cvtColor(color, gray, cv::COLOR_BGR2GRAY);
            if (model_) {
                det = detect_frame(gray, depth, is_mm, stamp_s);
                status = det.status;
                searched = (status != TrackStatus::SEARCH_IDLE);
            } else {
                status = TrackStatus::NO_MODEL;
                full_search_ = false;
            }
            // a pending mask candidate found in this frame replaces the current model's result
            if (std::optional<Detection> handoff = try_handoff(gray, stamp_s)) {
                apply_handoff(gray, depth, is_mm, *handoff, stamp_s);
                det = std::move(*handoff);
                status = det.status;
                searched = true;
                handoff_found = true;
                full_search_ = false;
            }
            process_ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t_start).count();
            cache_frame(gray, depth, depth_msg->encoding, color_msg->header.frame_id, stamp_s);
        }
        const bool found = (status == TrackStatus::OK);

        Output3D out;
        std::optional<Point3> measurement;
        if (found) {
            out = compute_output(det, depth, is_mm);
            if (out.status != TrackStatus::OK) {
                status = out.status;
            } else {
                geometry_msgs::msg::PointStamped cam_msg, world_msg;
                cam_msg.header = color_msg->header;
                cam_msg.point.x = out.cam_pt[0];
                cam_msg.point.y = out.cam_pt[1];
                cam_msg.point.z = out.cam_pt[2];
                if (transform_to_world(cam_msg, world_msg)) {
                    measurement = Point3{world_msg.point.x, world_msg.point.y, world_msg.point.z};
                } else {
                    status = TrackStatus::TF_FAIL;
                }
            }
        }
        const bool has_output_point = found && out.status == TrackStatus::OK;

        const std::optional<Point3> output = handoff_found ? handoff_track_state(measurement, det, stamp_s)
                                                           : update_track_state(status, det, measurement, stamp_s);
        if (output) {
            publish_point(*output, color_msg->header);
        }

        if (process_ms) {
            // the frame is in the cache now, so a mask computed on it can be matched later
            on_frame(color, color_msg->header);
        }

        const std::optional<TrackSource> source = searched ? std::optional<TrackSource>(det.source) : std::nullopt;
        report_status(status, state_, source, process_ms, has_output_point && out.depth_fallback);
        if (!is_debug_mode_) {
            return;
        }

        if (status == TrackStatus::OK && state_ == TrackState::TRACKING && output) {
            RCLCPP_INFO(this->get_logger(), "Target %s inliers %d/%d cam:(%.3f, %.3f, %.3f) %s:(%.3f, %.3f, %.3f)%s",
                        to_string(det.source), det.n_inliers, det.n_candidates, out.cam_pt[0], out.cam_pt[1],
                        out.cam_pt[2], world_frame_.c_str(), (*output)[0], (*output)[1], (*output)[2],
                        out.depth_fallback ? " (depth fallback)" : "");
        }

        if (image_debug_ && !color.empty() && debug_image_due()) {
            draw_debug(color, color_msg->header, det, out, status, found, has_output_point, searched, process_ms,
                       stamp_s);
        }
    }


    // Debug image goes to debug.image_topic (image_transport, so .../compressed exists for viewing over WiFi).
    // Drawing is skipped unless someone subscribes (or debug.window is on), and limited to debug.image_rate_hz.
    bool debug_image_due() {
        if (!debug_window_ && debug_image_pub_.getNumSubscribers() == 0) {
            return false;
        }
        const auto now = std::chrono::steady_clock::now();
        if (debug_image_rate_hz_ > 0.0 &&
            now - last_debug_image_at_ < std::chrono::duration<double>(1.0 / debug_image_rate_hz_)) {
            return false;
        }
        last_debug_image_at_ = now;
        return true;
    }

    void publish_debug_image(const cv::Mat &vis, const std_msgs::msg::Header &header) {
        if (debug_image_pub_.getNumSubscribers() > 0) {
            debug_image_pub_.publish(cv_bridge::CvImage(header, sensor_msgs::image_encodings::BGR8, vis).toImageMsg());
        }
        if (debug_window_) {
            // desktop only; needs a display
            cv::imshow("Mask Tracker", vis);
            cv::waitKey(1);
        }
    }

    void draw_debug(const cv::Mat &color, const std_msgs::msg::Header &header, const Detection &det,
                    const Output3D &out, TrackStatus status, bool found, bool has_output_point, bool searched,
                    std::optional<double> process_ms, double stamp_s) {
        const cv::Scalar green(0, 255, 0), yellow(0, 255, 255), orange(0, 165, 255), red(0, 0, 255);
        const cv::Scalar cyan(255, 255, 0), blue(255, 0, 0), magenta(255, 0, 255);
        const cv::Scalar state_color = state_ == TrackState::TRACKING   ? green
                                     : state_ == TrackState::HOLDING    ? yellow
                                     : state_ == TrackState::CONFIRMING ? orange
                                                                        : red;
        // TRACKING regions are colored by source; other states keep their state color
        const cv::Scalar source_color = det.source == TrackSource::ORB_ROI ? cyan
                                      : det.source == TrackSource::KLT     ? blue
                                                                           : green;
        cv::Mat vis = color.clone();
        if (debug_roi_) {
            cv::rectangle(vis, *debug_roi_, cyan, 1);
        }
        auto draw_region = [&vis](const std::vector<cv::Point2f> &region, const cv::Scalar &c) {
            if (region.size() >= 3) {
                cv::polylines(vis, object_tracker::to_int_polygon(region), true, c, 2);
            }
        };
        if (state_ == TrackState::HOLDING) {
            // last trusted region; an unconfirmed jump candidate is drawn in orange
            draw_region(trusted_.region, yellow);
            if (found) {
                draw_region(det.region, orange);
            }
        } else if (found) {
            draw_region(det.region, state_ == TrackState::TRACKING ? source_color : state_color);
        }
        for (const auto &p : det.inliers) {
            cv::circle(vis, p.pos, 2, green, -1);
        }
        for (const auto &p : det.depth_rejected) {
            cv::circle(vis, p, 2, red, -1);
        }
        for (const auto &p : det.region_rejected) {
            cv::circle(vis, p, 2, orange, -1);
        }
        if (has_output_point) {
            // magenta marks the default depth so it is not mistaken for a measurement
            const cv::Scalar point_color = out.depth_fallback ? magenta : red;
            cv::circle(vis, out.pixel, 6, point_color, -1);
            cv::putText(vis, cv::format(out.depth_fallback ? "Z=%.3fm FB" : "Z=%.3fm", out.cam_pt[2]),
                        out.pixel + cv::Point2f(8, -8), cv::FONT_HERSHEY_SIMPLEX, 0.6, point_color, 2);
        }

        // line 1: state / status / source; line 2: metrics in fixed-width columns so they do not shift
        const int font = cv::FONT_HERSHEY_SIMPLEX;
        const double font_scale = 0.6;
        const int thickness = 2;
        cv::putText(vis, cv::format("%s %s %s%s", to_string(state_), to_string(status),
                                    searched ? to_string(det.source) : "-", full_search_ ? " FULL_SEARCH" : ""),
                    cv::Point(10, 25), font, font_scale, state_color, thickness);
        const std::array<std::pair<std::string, const char *>, 7> metrics = {{
            {cv::format("pts=%zu", tracks_.size()), "pts=000"},
            {cv::format("inl=%d", det.n_inliers), "inl=000"},
            {cv::format("r=%.2f", det.inlier_ratio), "r=0.00"},
            {cv::format("e=%.2fpx", det.reproj_error), "e=00.00px"},
            {cv::format("sc=%.2f", det.scale), "sc=00.00"},
            {d_obj_ ? cv::format("d=%.3fm", *d_obj_) : std::string("d=-"), "d=0.000m"},
            {cv::format("t=%.1fms", process_ms.value_or(0.0)), "t=000.0ms"},
        }};
        int column_x = 10;
        for (const auto &[text, widest] : metrics) {
            cv::putText(vis, text, cv::Point(column_x, 50), font, font_scale, state_color, thickness);
            column_x += cv::getTextSize(widest, font, font_scale, thickness, nullptr).width + 15;
        }
        // top right: waiting candidate, or a recent switch to a new target
        std::string init_text;
        if (pending_handoff_) {
            init_text = cv::format("HANDOFF %.1fs", stamp_s - pending_handoff_->start_stamp_s);
        } else if (last_handoff_stamp_s_ && stamp_s - *last_handoff_stamp_s_ < 1.0) {
            init_text = "NEW TARGET";
        }
        if (!init_text.empty()) {
            const int text_width = cv::getTextSize(init_text, font, font_scale, thickness, nullptr).width;
            cv::putText(vis, init_text, cv::Point(vis.cols - text_width - 10, 25), font, font_scale, magenta, thickness);
        }
        publish_debug_image(vis, header);
    }

    // ROS interfaces
    rclcpp::Subscription<sensor_msgs::msg::CameraInfo>::SharedPtr info_sub_;
    message_filters::Subscriber<ImageMsg> color_sub_;
    message_filters::Subscriber<ImageMsg> depth_sub_;
    std::shared_ptr<message_filters::Synchronizer<SyncPolicy>> sync_;
    rclcpp::Subscription<ImageMsg>::SharedPtr mask_sub_;
    rclcpp::Publisher<geometry_msgs::msg::PointStamped>::SharedPtr point_pub_;
    rclcpp::Publisher<geometry_msgs::msg::Vector3Stamped>::SharedPtr size_pub_;  // null when size.enable is false
    std::unique_ptr<tf2_ros::Buffer> tf_buffer_;
    std::shared_ptr<tf2_ros::TransformListener> tf_listener_;
    std::unique_ptr<tf2_ros::TransformBroadcaster> tf_broadcaster_;
    std::string world_frame_;

    // Model and detection
    cv::Ptr<cv::ORB> orb_model_;  // model building (orb.n_features_model)
    cv::Ptr<cv::ORB> orb_;        // ROI search (orb.n_features)
    cv::Ptr<cv::ORB> orb_full_;   // full-frame search (orb.n_features_full)
    cv::Ptr<cv::BFMatcher> matcher_;
    std::optional<MaskModel> model_;
    double ratio_test_ = 0.8;
    int min_matches_ = 10;
    int min_inliers_ = 8;
    double ransac_reproj_thresh_ = 4.0;
    double gate_min_inlier_ratio_ = 0.3;
    double gate_max_reproj_error_px_ = 3.0;
    double gate_max_scale_ratio_ = 3.0;
    double gate_max_scale_change_ = 1.2;

    // Mask init
    double init_cache_s_ = 3.0;
    double init_stamp_tolerance_s_ = 0.005;
    int init_mask_erode_px_ = 4;
    int init_min_mask_px_ = 400;
    double init_handoff_timeout_s_ = 2.0;
    std::deque<CachedFrame> frame_cache_;
    std::optional<PendingHandoff> pending_handoff_;
    std::optional<double> last_handoff_stamp_s_;

    // Search and KLT
    double roi_expand_ratio_ = 0.5;
    int roi_min_size_px_ = 160;
    int klt_max_points_ = 100;
    int klt_min_points_ = 10;
    int klt_refill_below_ = 40;
    double klt_fb_max_px_ = 1.0;
    int klt_win_size_ = 21;
    int klt_max_level_ = 3;
    double klt_region_margin_px_ = 10.0;
    double search_full_after_s_ = 0.5;
    int search_full_interval_ = 5;
    cv::Mat prev_gray_;
    std::vector<TrackPoint> tracks_;
    std::vector<cv::Point2f> last_region_;  // model polygon at the last successful detection
    std::optional<double> prev_scale_;      // similarity scale at the last successful detection
    std::optional<cv::Rect> debug_roi_;     // ROI searched in the current frame, if any
    bool full_search_ = false;              // no search anchor in the current frame
    bool was_full_search_ = false;
    int full_search_counter_ = 0;

    // Depth / intrinsics
    double depth_gate_m_ = 0.04;
    int min_depth_points_ = 5;
    bool output_features_center_ = false;  // output.center_mode == "features" (old behavior)
    int output_center_depth_window_ = 5;
    double depth_min_m_ = 0.07;
    double depth_max_m_ = 0.5;
    bool depth_fallback_enable_ = true;
    double depth_fallback_value_m_ = 0.5;
    double depth_fallback_max_region_area_px_ = 0.0;
    std::optional<double> d_obj_;           // object reference depth (real depth only)
    double tf_timeout_s_ = 0.05;
    bool has_intrinsics_ = false;
    double fx_ = 0.0, fy_ = 0.0, cx_ = 0.0, cy_ = 0.0;

    // Filter
    bool pose_filter_enable_ = true;
    bool pose_filter_initialized_ = false;
    double pose_filtered_[3] = {0.0, 0.0, 0.0};

    // Hold / confirm state machine
    bool hold_enable_ = true;
    double hold_timeout_s_ = 0.0;  // <= 0: HOLDING never expires
    int hold_confirm_frames_ = 3;
    bool hold_publish_ = true;
    TrackState state_ = TrackState::LOST;
    TrustedTrack trusted_;
    Point3 candidate_pt_{};
    int candidate_count_ = 0;

    bool is_debug_mode_ = true;
    bool image_debug_ = false;

    // Debug image output
    image_transport::Publisher debug_image_pub_;
    double debug_image_rate_hz_ = 5.0;
    bool debug_window_ = false;
    std::chrono::steady_clock::time_point last_debug_image_at_{};

    // Stats (debug only)
    std::array<int, kTrackStatusCount> status_counts_{};
    std::array<int, kTrackStateCount> state_counts_{};
    std::array<int, kTrackSourceCount> source_counts_{};
    std::array<int, kInitEventCount> event_counts_{};
    double process_ms_sum_ = 0.0;
    double process_ms_max_ = 0.0;
    int process_count_ = 0;
    int depth_fallback_count_ = 0;
    double handoff_latency_ms_sum_ = 0.0;
    int handoff_latency_count_ = 0;
    std::chrono::steady_clock::time_point stats_start_ = std::chrono::steady_clock::now();
};
