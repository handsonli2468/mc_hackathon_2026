#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/image.hpp>
#include <sensor_msgs/msg/camera_info.hpp>
#include <sensor_msgs/image_encodings.hpp>
#include <geometry_msgs/msg/point_stamped.hpp>
#include <geometry_msgs/msg/transform_stamped.hpp>
#include <cv_bridge/cv_bridge.h>
#include <image_transport/image_transport.hpp>
#include <message_filters/subscriber.h>
#include <message_filters/synchronizer.h>
#include <message_filters/sync_policies/approximate_time.h>
#include <tf2_ros/buffer.h>
#include <tf2_ros/transform_listener.h>
#include <tf2_ros/transform_broadcaster.h>
#include <tf2_geometry_msgs/tf2_geometry_msgs.hpp>
#include <ament_index_cpp/get_package_share_directory.hpp>
#include <opencv2/opencv.hpp>
#include <opencv2/features2d.hpp>
#include <opencv2/calib3d.hpp>
#include <opencv2/video/tracking.hpp>
#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <deque>
#include <filesystem>
#include <limits>
#include <optional>
#include <string>

using ImageMsg = sensor_msgs::msg::Image;
using Point3 = std::array<double, 3>;

constexpr int kOrbEdgeThreshold = 31;
using SyncPolicy = message_filters::sync_policies::ApproximateTime<ImageMsg, ImageMsg>;

// Per-frame outcome; each failure value maps to one early exit of the pipeline
enum class TrackStatus {
    OK,
    WAITING_INTRINSICS,
    IMAGE_ERROR,
    NO_TARGET,
    NO_FEATURES,
    FEW_MATCHES,
    NO_HOMOGRAPHY,
    FEW_INLIERS,
    DEGENERATE_H,
    NON_CONVEX,
    SMALL_AREA,
    LOW_INLIER_RATIO,
    HIGH_REPROJ_ERROR,
    BAD_SHAPE,
    KLT_FEW_POINTS,
    CENTER_OUTSIDE,
    NO_DEPTH,
    TF_FAIL,
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
    ORB_FULL,
    ORB_ROI,
    KLT,
};
constexpr size_t kTrackSourceCount = static_cast<size_t>(TrackSource::KLT) + 1;

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

enum class TargetOrigin {
    IMAGE_FILE,
    MASK,
};

const char *to_string(TrackStatus status) {
    switch (status) {
        case TrackStatus::OK: return "OK";
        case TrackStatus::WAITING_INTRINSICS: return "WAITING_INTRINSICS";
        case TrackStatus::IMAGE_ERROR: return "IMAGE_ERROR";
        case TrackStatus::NO_TARGET: return "NO_TARGET";
        case TrackStatus::NO_FEATURES: return "NO_FEATURES";
        case TrackStatus::FEW_MATCHES: return "FEW_MATCHES";
        case TrackStatus::NO_HOMOGRAPHY: return "NO_HOMOGRAPHY";
        case TrackStatus::FEW_INLIERS: return "FEW_INLIERS";
        case TrackStatus::DEGENERATE_H: return "DEGENERATE_H";
        case TrackStatus::NON_CONVEX: return "NON_CONVEX";
        case TrackStatus::SMALL_AREA: return "SMALL_AREA";
        case TrackStatus::LOW_INLIER_RATIO: return "LOW_INLIER_RATIO";
        case TrackStatus::HIGH_REPROJ_ERROR: return "HIGH_REPROJ_ERROR";
        case TrackStatus::BAD_SHAPE: return "BAD_SHAPE";
        case TrackStatus::KLT_FEW_POINTS: return "KLT_FEW_POINTS";
        case TrackStatus::CENTER_OUTSIDE: return "CENTER_OUTSIDE";
        case TrackStatus::NO_DEPTH: return "NO_DEPTH";
        case TrackStatus::TF_FAIL: return "TF_FAIL";
        case TrackStatus::JUMP_UNCONFIRMED: return "JUMP_UNCONFIRMED";
    }
    return "UNKNOWN";
}

const char *to_string(TrackState state) {
    switch (state) {
        case TrackState::LOST: return "LOST";
        case TrackState::CONFIRMING: return "CONFIRMING";
        case TrackState::TRACKING: return "TRACKING";
        case TrackState::HOLDING: return "HOLDING";
    }
    return "UNKNOWN";
}

const char *to_string(TrackSource source) {
    switch (source) {
        case TrackSource::ORB_FULL: return "ORB_FULL";
        case TrackSource::ORB_ROI: return "ORB_ROI";
        case TrackSource::KLT: return "KLT";
    }
    return "UNKNOWN";
}

const char *to_string(InitEvent event) {
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

const char *to_string(TargetOrigin origin) {
    switch (origin) {
        case TargetOrigin::IMAGE_FILE: return "file";
        case TargetOrigin::MASK: return "mask";
    }
    return "unknown";
}

struct Detection {
    TrackStatus status = TrackStatus::OK;
    TrackSource source = TrackSource::ORB_FULL;
    std::vector<cv::Point2f> corners;  // TL, TR, BR, BL in scene pixels, valid from NON_CONVEX on
    cv::Point2f center;
    int n_matches = 0;          // ORB: ratio-test matches, KLT: points surviving the forward-backward check
    int n_inliers = 0;
    double inlier_ratio = 0.0;
    double reproj_error = 0.0;  // mean inlier reprojection error under H (px)
    double side_ratio = 0.0;    // max opposite-side length ratio of the box, valid from NON_CONVEX on
    // Valid from FEW_INLIERS on; kept for frame-to-frame tracking
    cv::Mat H;
    std::vector<cv::Point2f> inlier_target_pts;
    std::vector<cv::Point2f> inlier_scene_pts;
};

// What detection matches against. Model coordinates are target image pixels (file) or pixels of the
// frame the mask was computed on (mask).
struct TargetModel {
    std::vector<cv::KeyPoint> keypoints;
    cv::Mat descriptors;
    std::vector<cv::Point2f> corners;  // TL, TR, BR, BL in model coordinates
    cv::Point2f center;                // published point, in model coordinates
    cv::Rect2f bounds;                 // file: (0, 0, w, h); mask: bounding rect of the cleaned mask
    TargetOrigin origin = TargetOrigin::IMAGE_FILE;
    double source_stamp_s = 0.0;       // stamp of the frame the mask belongs to (0 for file)
};

// Recent frame kept so a delayed mask can be applied to the frame it was computed on
struct CachedFrame {
    double stamp_s = 0.0;
    cv::Mat gray;
    cv::Mat depth;
    std::string depth_encoding;
};

// Candidate model from a mask, waiting to be found in the current frame
struct PendingHandoff {
    TargetModel model;
    double start_stamp_s = 0.0;  // newest frame stamp when the mask arrived
    double mask_stamp_s = 0.0;
};

// Last output we trust, kept while HOLDING
struct TrustedTrack {
    Point3 world_pt{};  // filtered, in world_frame
    std::vector<cv::Point2f> corners;
    cv::Point2f center;
    cv::Mat H;
    double stamp_s = 0.0;  // image stamp
};

// Larger of the two opposite-side length ratios of a TL, TR, BR, BL quad
double max_opposite_side_ratio(const std::vector<cv::Point2f> &quad) {
    auto ratio = [](double a, double b) {
        const double shorter = std::min(a, b);
        return shorter > 0.0 ? std::max(a, b) / shorter : std::numeric_limits<double>::infinity();
    };
    const double top = cv::norm(quad[1] - quad[0]);
    const double bottom = cv::norm(quad[2] - quad[3]);
    const double left = cv::norm(quad[3] - quad[0]);
    const double right = cv::norm(quad[2] - quad[1]);
    return std::max(ratio(top, bottom), ratio(left, right));
}

class OrbTrackerNode : public rclcpp::Node {
public:
    OrbTrackerNode() : Node("orb_tracker_node") {
        this->declare_parameter<std::string>("target_image_path", "target.png");
        this->declare_parameter<std::string>("color_topic", "/camera_duck/camera/color/image_rect_raw");
        this->declare_parameter<std::string>("depth_topic", "/camera_duck/camera/aligned_depth_to_color/image_raw");
        this->declare_parameter<std::string>("camera_info_topic", "/camera_duck/camera/color/camera_info");
        this->declare_parameter<std::string>("output_topic", "/tracked_object/point");
        this->declare_parameter<std::string>("world_frame", "map");
        this->declare_parameter<int>("orb.n_features", 1000);
        this->declare_parameter<int>("orb.n_features_full", 0);
        this->declare_parameter<int>("orb.fast_threshold", 20);
        this->declare_parameter<double>("detect_scale", 1.0);
        this->declare_parameter<double>("ratio_test", 0.75);
        this->declare_parameter<int>("min_matches", 15);
        this->declare_parameter<int>("min_inliers", 10);
        this->declare_parameter<double>("ransac_reproj_thresh", 5.0);
        this->declare_parameter<double>("min_area_px", 400.0);
        this->declare_parameter<double>("gate.min_inlier_ratio", 0.3);
        this->declare_parameter<double>("gate.max_reproj_error_px", 3.0);
        this->declare_parameter<double>("gate.max_side_ratio", 3.0);
        this->declare_parameter<bool>("roi.enable", true);
        this->declare_parameter<double>("roi.expand_ratio", 0.5);
        this->declare_parameter<int>("roi.min_size_px", 160);
        this->declare_parameter<bool>("roi.full_frame_fallback", true);
        this->declare_parameter<bool>("klt.enable", true);
        this->declare_parameter<int>("klt.redetect_interval", 10);
        this->declare_parameter<int>("klt.min_points", 12);
        this->declare_parameter<int>("klt.max_points", 150);
        this->declare_parameter<bool>("klt.extra_corners", true);
        this->declare_parameter<double>("klt.fb_max_px", 1.0);
        this->declare_parameter<int>("klt.win_size", 21);
        this->declare_parameter<int>("klt.max_level", 3);
        this->declare_parameter<int>("depth_window", 7);
        this->declare_parameter<double>("depth_min_m", 0.07);
        this->declare_parameter<double>("depth_max_m", 0.5);
        this->declare_parameter<bool>("depth_fallback.enable", true);
        this->declare_parameter<double>("depth_fallback.value_m", 0.5);
        this->declare_parameter<double>("depth_fallback.max_box_area_px", 0.0);
        this->declare_parameter<double>("tf_timeout_s", 0.05);
        this->declare_parameter<bool>("pose_filter.enable", true);
        this->declare_parameter<double>("pose_filter.alpha", 0.3);
        this->declare_parameter<double>("pose_filter.max_jump_m", 0.15);
        this->declare_parameter<bool>("hold.enable", true);
        this->declare_parameter<double>("hold.timeout_s", 0.0);
        this->declare_parameter<double>("hold.full_search_after_s", 0.5);
        this->declare_parameter<int>("hold.confirm_frames", 3);
        this->declare_parameter<bool>("hold.publish", true);
        this->declare_parameter<bool>("init.enable", false);
        this->declare_parameter<std::string>("mask_topic", "/tracked_object/init_mask");
        this->declare_parameter<double>("init.cache_s", 3.0);
        this->declare_parameter<double>("init.stamp_tolerance_s", 0.005);
        this->declare_parameter<double>("init.depth_gate_m", 0.04);
        this->declare_parameter<int>("init.min_mask_px", 400);
        this->declare_parameter<int>("init.mask_erode_px", 4);
        this->declare_parameter<double>("init.handoff_timeout_s", 2.0);
        this->declare_parameter<bool>("debug.enable", false);
        this->declare_parameter<bool>("debug.img", false);
        this->declare_parameter<std::string>("debug.image_topic", "/tracked_object/debug_image");
        this->declare_parameter<double>("debug.image_rate_hz", 5.0);
        this->declare_parameter<bool>("debug.window", false);

        const std::string target_path = resolve_target_path(this->get_parameter("target_image_path").as_string());
        const std::string color_topic = this->get_parameter("color_topic").as_string();
        const std::string depth_topic = this->get_parameter("depth_topic").as_string();
        const std::string info_topic = this->get_parameter("camera_info_topic").as_string();
        const std::string output_topic = this->get_parameter("output_topic").as_string();
        world_frame_ = this->get_parameter("world_frame").as_string();
        n_features_ = this->get_parameter("orb.n_features").as_int();
        // <= 0 means full-frame search uses the same budget as the target / ROI
        n_features_full_ = this->get_parameter("orb.n_features_full").as_int();
        if (n_features_full_ <= 0) {
            n_features_full_ = n_features_;
        }
        detect_scale_ = std::clamp(this->get_parameter("detect_scale").as_double(), 0.1, 1.0);
        ratio_test_ = this->get_parameter("ratio_test").as_double();
        min_matches_ = this->get_parameter("min_matches").as_int();
        min_inliers_ = this->get_parameter("min_inliers").as_int();
        ransac_reproj_thresh_ = this->get_parameter("ransac_reproj_thresh").as_double();
        min_area_px_ = this->get_parameter("min_area_px").as_double();
        gate_min_inlier_ratio_ = this->get_parameter("gate.min_inlier_ratio").as_double();
        gate_max_reproj_error_px_ = this->get_parameter("gate.max_reproj_error_px").as_double();
        gate_max_side_ratio_ = this->get_parameter("gate.max_side_ratio").as_double();
        roi_enable_ = this->get_parameter("roi.enable").as_bool();
        roi_expand_ratio_ = std::max(0.0, this->get_parameter("roi.expand_ratio").as_double());
        roi_min_size_px_ = std::max(0, static_cast<int>(this->get_parameter("roi.min_size_px").as_int()));
        roi_full_frame_fallback_ = this->get_parameter("roi.full_frame_fallback").as_bool();
        klt_enable_ = this->get_parameter("klt.enable").as_bool();
        klt_redetect_interval_ = std::max(1, static_cast<int>(this->get_parameter("klt.redetect_interval").as_int()));
        // findHomography needs at least 4 point pairs
        klt_min_points_ = std::max(4, static_cast<int>(this->get_parameter("klt.min_points").as_int()));
        klt_max_points_ = std::max(0, static_cast<int>(this->get_parameter("klt.max_points").as_int()));
        klt_extra_corners_ = this->get_parameter("klt.extra_corners").as_bool();
        klt_fb_max_px_ = this->get_parameter("klt.fb_max_px").as_double();
        klt_win_size_ = std::max(3, static_cast<int>(this->get_parameter("klt.win_size").as_int()));
        klt_max_level_ = std::max(0, static_cast<int>(this->get_parameter("klt.max_level").as_int()));
        depth_window_ = std::max(1, static_cast<int>(this->get_parameter("depth_window").as_int()) | 1);
        depth_min_m_ = this->get_parameter("depth_min_m").as_double();
        depth_max_m_ = this->get_parameter("depth_max_m").as_double();
        depth_fallback_enable_ = this->get_parameter("depth_fallback.enable").as_bool();
        depth_fallback_value_m_ = this->get_parameter("depth_fallback.value_m").as_double();
        depth_fallback_max_box_area_px_ = this->get_parameter("depth_fallback.max_box_area_px").as_double();
        tf_timeout_s_ = this->get_parameter("tf_timeout_s").as_double();
        pose_filter_enable_ = this->get_parameter("pose_filter.enable").as_bool();
        hold_enable_ = this->get_parameter("hold.enable").as_bool();
        hold_timeout_s_ = this->get_parameter("hold.timeout_s").as_double();
        hold_full_search_after_s_ = this->get_parameter("hold.full_search_after_s").as_double();
        hold_confirm_frames_ = std::max(1, static_cast<int>(this->get_parameter("hold.confirm_frames").as_int()));
        hold_publish_ = this->get_parameter("hold.publish").as_bool();
        init_enable_ = this->get_parameter("init.enable").as_bool();
        const std::string mask_topic = this->get_parameter("mask_topic").as_string();
        init_cache_s_ = this->get_parameter("init.cache_s").as_double();
        init_stamp_tolerance_s_ = this->get_parameter("init.stamp_tolerance_s").as_double();
        init_depth_gate_m_ = this->get_parameter("init.depth_gate_m").as_double();
        init_min_mask_px_ = static_cast<int>(this->get_parameter("init.min_mask_px").as_int());
        init_mask_erode_px_ = std::max(0, static_cast<int>(this->get_parameter("init.mask_erode_px").as_int()));
        init_handoff_timeout_s_ = this->get_parameter("init.handoff_timeout_s").as_double();
        is_debug_mode_ = this->get_parameter("debug.enable").as_bool();
        image_debug_ = this->get_parameter("debug.img").as_bool();
        debug_image_rate_hz_ = this->get_parameter("debug.image_rate_hz").as_double();
        debug_window_ = this->get_parameter("debug.window").as_bool();

        const int fast_threshold = this->get_parameter("orb.fast_threshold").as_int();
        orb_ = cv::ORB::create(n_features_, 1.2f, 8, kOrbEdgeThreshold, 0, 2, cv::ORB::HARRIS_SCORE, 31, fast_threshold);
        // full-frame search keeps only the strongest n features over the whole image, so a low-contrast
        // target loses them to a busy background; give it a larger budget than the ROI search
        orb_full_ = n_features_full_ == n_features_
                        ? orb_
                        : cv::ORB::create(n_features_full_, 1.2f, 8, kOrbEdgeThreshold, 0, 2, cv::ORB::HARRIS_SCORE,
                                          31, fast_threshold);
        matcher_ = cv::BFMatcher::create(cv::NORM_HAMMING, false);
        if (!target_path.empty()) {
            model_ = load_target(target_path);
        } else if (!init_enable_) {
            throw std::runtime_error("target_image_path is empty; set a target image or enable init.enable to wait for a mask");
        }

        tf_buffer_ = std::make_unique<tf2_ros::Buffer>(this->get_clock());
        tf_listener_ = std::make_shared<tf2_ros::TransformListener>(*tf_buffer_);
        tf_broadcaster_ = std::make_unique<tf2_ros::TransformBroadcaster>(*this);

        info_sub_ = this->create_subscription<sensor_msgs::msg::CameraInfo>(
            info_topic, rclcpp::SensorDataQoS(),
            std::bind(&OrbTrackerNode::camera_info_callback, this, std::placeholders::_1));

        color_sub_.subscribe(this, color_topic, rmw_qos_profile_sensor_data);
        depth_sub_.subscribe(this, depth_topic, rmw_qos_profile_sensor_data);
        sync_ = std::make_shared<message_filters::Synchronizer<SyncPolicy>>(SyncPolicy(10), color_sub_, depth_sub_);
        sync_->registerCallback(std::bind(&OrbTrackerNode::rgbd_callback, this,
                                          std::placeholders::_1, std::placeholders::_2));

        point_pub_ = this->create_publisher<geometry_msgs::msg::PointStamped>(output_topic, 10);
        if (image_debug_) {
            const std::string debug_topic = this->get_parameter("debug.image_topic").as_string();
            debug_image_pub_ = image_transport::create_publisher(this, debug_topic);
            RCLCPP_INFO(this->get_logger(), "Debug image on %s (and %s/compressed), up to %.1f Hz%s",
                        debug_topic.c_str(), debug_topic.c_str(), debug_image_rate_hz_,
                        debug_window_ ? ", plus a window" : "");
        }

        if (init_enable_) {
            mask_sub_ = this->create_subscription<ImageMsg>(
                mask_topic, rclcpp::QoS(rclcpp::KeepLast(5)).reliable(),
                std::bind(&OrbTrackerNode::mask_callback, this, std::placeholders::_1));
            RCLCPP_INFO(this->get_logger(), "Mask init enabled: %s, frame cache %.1f s", mask_topic.c_str(), init_cache_s_);
        }

        if (model_) {
            RCLCPP_INFO(this->get_logger(), "Tracking target '%s' (%zu keypoints), color=%s depth=%s",
                        target_path.c_str(), model_->keypoints.size(), color_topic.c_str(), depth_topic.c_str());
        } else {
            RCLCPP_INFO(this->get_logger(), "No target yet, waiting for a mask; color=%s depth=%s",
                        color_topic.c_str(), depth_topic.c_str());
        }
    }

private:
    std::string resolve_target_path(const std::string &path) {
        if (path.empty() || std::filesystem::path(path).is_absolute() || std::filesystem::exists(path)) {
            return path;
        }
        try {
            return ament_index_cpp::get_package_share_directory("object_tracker") + "/targets/" + path;
        } catch (const std::exception &) {
            return path;
        }
    }

    TargetModel load_target(const std::string &path) {
        cv::Mat target = cv::imread(path, cv::IMREAD_GRAYSCALE);
        if (target.empty()) {
            throw std::runtime_error("Cannot read target image: " + path);
        }
        // ORB drops keypoints within edgeThreshold of the border, which kills most of a small crop.
        // Pad the target so the whole crop is usable, then shift keypoints back.
        cv::Mat padded;
        cv::copyMakeBorder(target, padded, kOrbEdgeThreshold, kOrbEdgeThreshold, kOrbEdgeThreshold, kOrbEdgeThreshold,
                           cv::BORDER_REFLECT_101);
        std::vector<cv::KeyPoint> keypoints;
        cv::Mat descriptors;
        orb_->detectAndCompute(padded, cv::noArray(), keypoints, descriptors);

        TargetModel model;
        const cv::Rect2f inside(0.0f, 0.0f, static_cast<float>(target.cols), static_cast<float>(target.rows));
        for (size_t i = 0; i < keypoints.size(); ++i) {
            cv::KeyPoint kp = keypoints[i];
            kp.pt -= cv::Point2f(kOrbEdgeThreshold, kOrbEdgeThreshold);
            if (inside.contains(kp.pt)) {
                model.keypoints.push_back(kp);
                model.descriptors.push_back(descriptors.row(static_cast<int>(i)));
            }
        }
        if (static_cast<int>(model.keypoints.size()) < min_matches_) {
            throw std::runtime_error("Target image has too few ORB features (" +
                                     std::to_string(model.keypoints.size()) + "), pick a more textured image");
        }
        const float w = static_cast<float>(target.cols);
        const float h = static_cast<float>(target.rows);
        model.bounds = inside;
        model.corners = {cv::Point2f(0, 0), cv::Point2f(w, 0), cv::Point2f(w, h), cv::Point2f(0, h)};
        model.center = cv::Point2f(w / 2.0f, h / 2.0f);
        model.origin = TargetOrigin::IMAGE_FILE;
        return model;
    }

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

    // ORB features inside roi (the whole image for ORB_FULL) + ratio test, then homography checks
    // Always returns a result; status tells which check stopped it (OK if all passed)
    Detection detect_orb(const TargetModel &model, const cv::Mat &gray, const cv::Rect &roi, TrackSource source) {
        Detection det;
        det.source = source;
        cv::Mat scene = gray(roi);
        if (detect_scale_ < 1.0) {
            cv::resize(gray(roi), scene, cv::Size(), detect_scale_, detect_scale_, cv::INTER_AREA);
        }

        std::vector<cv::KeyPoint> scene_keypoints;
        cv::Mat scene_descriptors;
        const cv::Ptr<cv::ORB> &orb = (source == TrackSource::ORB_FULL) ? orb_full_ : orb_;
        orb->detectAndCompute(scene, cv::noArray(), scene_keypoints, scene_descriptors);
        if (scene_descriptors.empty() || scene_keypoints.size() < 2) {
            det.status = TrackStatus::NO_FEATURES;
            return det;
        }

        std::vector<std::vector<cv::DMatch>> knn_matches;
        matcher_->knnMatch(model.descriptors, scene_descriptors, knn_matches, 2);

        // keypoints are in (scaled) roi coordinates; shift back to full image pixels
        const cv::Point2f roi_offset(static_cast<float>(roi.x), static_cast<float>(roi.y));
        std::vector<cv::Point2f> obj_pts, scene_pts;
        for (const auto &m : knn_matches) {
            if (m.size() == 2 && m[0].distance < ratio_test_ * m[1].distance) {
                obj_pts.push_back(model.keypoints[m[0].queryIdx].pt);
                scene_pts.push_back(scene_keypoints[m[0].trainIdx].pt / detect_scale_ + roi_offset);
            }
        }

        det.n_matches = static_cast<int>(obj_pts.size());
        evaluate_homography(model, obj_pts, scene_pts, min_matches_, min_inliers_, det);
        return det;
    }

    // RANSAC homography (model -> scene) + sanity checks + gates, shared by ORB and KLT
    // Expects det.n_matches == obj_pts.size(); sets det.status (OK if all passed)
    void evaluate_homography(const TargetModel &model, const std::vector<cv::Point2f> &obj_pts,
                             const std::vector<cv::Point2f> &scene_pts, int min_matches, int min_inliers,
                             Detection &det) {
        if (det.n_matches < min_matches) {
            det.status = TrackStatus::FEW_MATCHES;
            return;
        }

        std::vector<uchar> inlier_mask;
        cv::Mat H = cv::findHomography(obj_pts, scene_pts, cv::RANSAC, ransac_reproj_thresh_, inlier_mask);
        if (H.empty()) {
            det.status = TrackStatus::NO_HOMOGRAPHY;
            return;
        }
        det.H = H;
        for (size_t i = 0; i < inlier_mask.size(); ++i) {
            if (inlier_mask[i]) {
                det.inlier_target_pts.push_back(obj_pts[i]);
                det.inlier_scene_pts.push_back(scene_pts[i]);
            }
        }
        det.n_inliers = static_cast<int>(det.inlier_target_pts.size());
        det.inlier_ratio = static_cast<double>(det.n_inliers) / det.n_matches;
        if (det.n_inliers > 0) {
            std::vector<cv::Point2f> projected;
            cv::perspectiveTransform(det.inlier_target_pts, projected, H);
            double error_sum = 0.0;
            for (size_t i = 0; i < projected.size(); ++i) {
                error_sum += cv::norm(projected[i] - det.inlier_scene_pts[i]);
            }
            det.reproj_error = error_sum / projected.size();
        }
        if (det.n_inliers < min_inliers) {
            det.status = TrackStatus::FEW_INLIERS;
            return;
        }

        // reject mirrored / degenerate homographies
        const double det2x2 = H.at<double>(0, 0) * H.at<double>(1, 1) - H.at<double>(0, 1) * H.at<double>(1, 0);
        if (det2x2 <= 0.0) {
            det.status = TrackStatus::DEGENERATE_H;
            return;
        }

        cv::perspectiveTransform(model.corners, det.corners, H);
        det.side_ratio = max_opposite_side_ratio(det.corners);
        std::vector<cv::Point2f> center_in{model.center}, center_out;
        cv::perspectiveTransform(center_in, center_out, H);
        det.center = center_out[0];

        if (!cv::isContourConvex(det.corners)) {
            det.status = TrackStatus::NON_CONVEX;
            return;
        }
        if (cv::contourArea(det.corners) < min_area_px_) {
            det.status = TrackStatus::SMALL_AREA;
            return;
        }

        // stricter gates against background mismatches
        if (det.inlier_ratio < gate_min_inlier_ratio_) {
            det.status = TrackStatus::LOW_INLIER_RATIO;
            return;
        }
        if (det.reproj_error > gate_max_reproj_error_px_) {
            det.status = TrackStatus::HIGH_REPROJ_ERROR;
            return;
        }
        if (det.side_ratio > gate_max_side_ratio_) {
            det.status = TrackStatus::BAD_SHAPE;
            return;
        }
        det.status = TrackStatus::OK;
    }

    // Search window around the last box: bounding rect grown on each side, at least roi.min_size_px, clamped
    std::optional<cv::Rect> compute_roi(const std::vector<cv::Point2f> &box, const cv::Size &image_size) const {
        if (box.size() != 4) {
            return std::nullopt;
        }
        // float bounding box (cv::boundingRect2f is not available in OpenCV 4.5);
        // plain loop instead of std::minmax({...}), which triggers a -Wpsabi note on aarch64
        float min_x = box[0].x, max_x = box[0].x, min_y = box[0].y, max_y = box[0].y;
        for (const auto &p : box) {
            min_x = std::min(min_x, p.x);
            max_x = std::max(max_x, p.x);
            min_y = std::min(min_y, p.y);
            max_y = std::max(max_y, p.y);
        }
        const float min_margin = static_cast<float>(kOrbEdgeThreshold + 8);
        const float margin_x = std::max((max_x - min_x) * static_cast<float>(roi_expand_ratio_), min_margin);
        const float margin_y = std::max((max_y - min_y) * static_cast<float>(roi_expand_ratio_), min_margin);
        float x0 = min_x - margin_x, x1 = max_x + margin_x;
        float y0 = min_y - margin_y, y1 = max_y + margin_y;
        const float min_size = static_cast<float>(roi_min_size_px_);
        if (x1 - x0 < min_size) {
            const float cx = 0.5f * (x0 + x1);
            x0 = cx - 0.5f * min_size;
            x1 = cx + 0.5f * min_size;
        }
        if (y1 - y0 < min_size) {
            const float cy = 0.5f * (y0 + y1);
            y0 = cy - 0.5f * min_size;
            y1 = cy + 0.5f * min_size;
        }
        cv::Rect roi(cv::Point(static_cast<int>(std::floor(x0)), static_cast<int>(std::floor(y0))),
                     cv::Point(static_cast<int>(std::ceil(x1)), static_cast<int>(std::ceil(y1))));
        roi &= cv::Rect(cv::Point(0, 0), image_size);
        if (roi.width <= 0 || roi.height <= 0) {
            return std::nullopt;
        }
        return roi;
    }

    // ORB near the last box first; full frame when there is no box or (optionally) the ROI failed
    Detection detect_orb_with_roi(const cv::Mat &gray) {
        const cv::Rect full_frame(cv::Point(0, 0), gray.size());
        if (roi_enable_ && has_search_anchor()) {
            // an empty last_box_ gives no ROI, so this falls through to the full frame
            debug_roi_ = compute_roi(last_box_, gray.size());
            if (debug_roi_) {
                Detection det = detect_orb(*model_, gray, *debug_roi_, TrackSource::ORB_ROI);
                if (det.status == TrackStatus::OK || !roi_full_frame_fallback_) {
                    return det;
                }
            }
        }
        return detect_orb(*model_, gray, full_frame, TrackSource::ORB_FULL);
    }

    // Forward-backward pyramidal LK from the previous frame, then homography from target coords
    Detection track_klt(const cv::Mat &gray) {
        Detection det;
        det.source = TrackSource::KLT;

        const cv::Size win(klt_win_size_, klt_win_size_);
        std::vector<cv::Point2f> next_pts, back_pts;
        std::vector<uchar> status_fwd, status_bwd;
        std::vector<float> err;
        cv::calcOpticalFlowPyrLK(prev_gray_, gray, klt_scene_pts_, next_pts, status_fwd, err, win, klt_max_level_);
        cv::calcOpticalFlowPyrLK(gray, prev_gray_, next_pts, back_pts, status_bwd, err, win, klt_max_level_);

        std::vector<cv::Point2f> obj_pts, scene_pts;
        for (size_t i = 0; i < klt_scene_pts_.size(); ++i) {
            if (status_fwd[i] && status_bwd[i] && cv::norm(back_pts[i] - klt_scene_pts_[i]) <= klt_fb_max_px_) {
                obj_pts.push_back(klt_target_pts_[i]);
                scene_pts.push_back(next_pts[i]);
            }
        }
        klt_target_pts_ = obj_pts;
        klt_scene_pts_ = scene_pts;

        det.n_matches = static_cast<int>(obj_pts.size());
        if (det.n_matches < klt_min_points_) {
            det.status = TrackStatus::KLT_FEW_POINTS;
            return det;
        }
        evaluate_homography(*model_, obj_pts, scene_pts, klt_min_points_, klt_min_points_, det);
        if (det.status == TrackStatus::OK) {
            klt_target_pts_ = det.inlier_target_pts;
            klt_scene_pts_ = det.inlier_scene_pts;
        }
        return det;
    }

    // Re-seed KLT from an accepted ORB detection: its inliers, plus corners inside the box mapped by H^-1
    void reseed_klt(const TargetModel &model, const cv::Mat &gray, const Detection &det) {
        if (!klt_enable_) {
            return;
        }
        klt_target_pts_ = det.inlier_target_pts;
        klt_scene_pts_ = det.inlier_scene_pts;

        const int n_extra = klt_max_points_ - static_cast<int>(klt_scene_pts_.size());
        if (!klt_extra_corners_ || n_extra <= 0) {
            return;
        }
        cv::Mat mask = cv::Mat::zeros(gray.size(), CV_8UC1);
        std::vector<cv::Point> poly(det.corners.begin(), det.corners.end());
        cv::fillConvexPoly(mask, poly, cv::Scalar(255));
        std::vector<cv::Point2f> scene_corners;
        cv::goodFeaturesToTrack(gray, scene_corners, n_extra, 0.01, 7.0, mask);
        if (scene_corners.empty()) {
            return;
        }
        // planar assumption: map image corners back onto the target
        std::vector<cv::Point2f> target_pts;
        cv::perspectiveTransform(scene_corners, target_pts, det.H.inv());
        for (size_t i = 0; i < scene_corners.size(); ++i) {
            if (model.bounds.contains(target_pts[i])) {
                klt_target_pts_.push_back(target_pts[i]);
                klt_scene_pts_.push_back(scene_corners[i]);
            }
        }
    }

    // Per-frame source selection. KLT first while a track exists; ORB (ROI, then full frame) when KLT
    // failed, did not run, or redetect_interval frames passed since the last ORB success.
    Detection detect_frame(const cv::Mat &gray) {
        debug_roi_.reset();

        std::optional<Detection> klt;
        if (klt_enable_ && has_search_anchor() && !klt_target_pts_.empty() && !prev_gray_.empty()) {
            klt = track_klt(gray);
            if (klt->status != TrackStatus::OK) {
                clear_klt_points();
            }
        }
        const bool klt_ok = klt && klt->status == TrackStatus::OK;

        std::optional<Detection> orb;
        if (!klt_ok || frames_since_orb_ok_ >= klt_redetect_interval_) {
            orb = detect_orb_with_roi(gray);
        }

        Detection det;
        if (orb && orb->status == TrackStatus::OK) {
            det = *orb;
            reseed_klt(*model_, gray, det);
            frames_since_orb_ok_ = 0;
        } else {
            // KLT result bridges ORB failures; ORB is retried on the next frame
            det = klt_ok ? *klt : *orb;
            ++frames_since_orb_ok_;
        }
        if (det.status == TrackStatus::OK) {
            last_box_ = det.corners;
        }
        prev_gray_ = gray;
        return det;
    }

    void clear_klt_points() {
        klt_target_pts_.clear();
        klt_scene_pts_.clear();
    }

    // ---- Mask init: build a target from an upstream mask computed on a past frame ----

    void record_event(InitEvent event, double mask_stamp_s, const std::string &detail) {
        ++event_counts_[static_cast<size_t>(event)];
        RCLCPP_INFO(this->get_logger(), "Init event %s: mask_stamp=%.3f %s", to_string(event), mask_stamp_s,
                    detail.c_str());
    }

    // Ring buffer of the last init.cache_s seconds of processed frames (image stamps).
    // Memory = frames * (W * H gray + W * H * depth bytes per pixel); at 848x480 with 16UC1 depth that is
    // 0.41 MB + 0.81 MB = 1.22 MB per frame, so 3 s at 30 fps (90 frames) is about 110 MB.
    void cache_frame(const cv::Mat &gray, const cv::Mat &depth, const std::string &depth_encoding, double stamp_s) {
        if (!frame_cache_.empty() && stamp_s < frame_cache_.back().stamp_s) {
            // stamps went backwards (e.g. a restarted bag); old frames can no longer match a mask
            frame_cache_.clear();
        }
        frame_cache_.push_back(CachedFrame{stamp_s, gray.clone(), depth.clone(), depth_encoding});
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

        const std::optional<cv::Mat> cleaned = clean_mask(*frame, mask, mask_stamp_s);
        if (!cleaned) {
            return;
        }
        std::optional<TargetModel> candidate = build_mask_model(*frame, *cleaned, mask_stamp_s);
        if (!candidate) {
            return;  // the current model stays
        }
        if (pending_handoff_) {
            record_event(InitEvent::HANDOFF_REPLACED, mask_stamp_s,
                         cv::format("replaces mask_stamp=%.3f", pending_handoff_->mask_stamp_s));
        }
        pending_handoff_ = PendingHandoff{std::move(*candidate), frame_cache_.back().stamp_s, mask_stamp_s};
    }

    // Clean a mask on its source frame: drop pixels whose measured depth is far from the mask's median
    // in-range depth (usually background caught at the edges), then keep the largest connected region.
    // Returns nullopt (MASK_TOO_SMALL) when fewer than init.min_mask_px pixels remain.
    std::optional<cv::Mat> clean_mask(const CachedFrame &frame, const cv::Mat &mask, double mask_stamp_s) {
        cv::Mat cleaned = (mask != 0);
        const bool is_mm = (frame.depth_encoding == sensor_msgs::image_encodings::TYPE_16UC1 ||
                            frame.depth_encoding == sensor_msgs::image_encodings::MONO16);
        auto depth_at = [&](int x, int y) {
            return is_mm ? frame.depth.at<uint16_t>(y, x) * 0.001 : static_cast<double>(frame.depth.at<float>(y, x));
        };

        std::vector<double> in_range;
        for (int y = 0; y < cleaned.rows; ++y) {
            const uchar *row = cleaned.ptr<uchar>(y);
            for (int x = 0; x < cleaned.cols; ++x) {
                const double d = row[x] ? depth_at(x, y) : 0.0;
                if (row[x] && std::isfinite(d) && d >= depth_min_m_ && d <= depth_max_m_) {
                    in_range.push_back(d);
                }
            }
        }
        int n_depth_removed = 0;
        double d0 = 0.0;
        if (!in_range.empty()) {
            std::nth_element(in_range.begin(), in_range.begin() + in_range.size() / 2, in_range.end());
            d0 = in_range[in_range.size() / 2];
            for (int y = 0; y < cleaned.rows; ++y) {
                uchar *row = cleaned.ptr<uchar>(y);
                for (int x = 0; x < cleaned.cols; ++x) {
                    if (!row[x]) {
                        continue;
                    }
                    // any measured depth counts, so background beyond depth_max_m is removed too
                    const double d = depth_at(x, y);
                    if (std::isfinite(d) && d > 0.0 && std::abs(d - d0) > init_depth_gate_m_) {
                        row[x] = 0;
                        ++n_depth_removed;
                    }
                }
            }
        }
        // no in-range depth inside the mask (e.g. too far): the depth step is skipped

        cv::Mat labels, stats, centroids;
        const int n_labels = cv::connectedComponentsWithStats(cleaned, labels, stats, centroids, 8, CV_32S);
        int best_label = 0;
        int best_area = 0;
        for (int i = 1; i < n_labels; ++i) {
            const int area = stats.at<int>(i, cv::CC_STAT_AREA);
            if (area > best_area) {
                best_area = area;
                best_label = i;
            }
        }
        if (best_area < init_min_mask_px_) {
            record_event(InitEvent::MASK_TOO_SMALL, mask_stamp_s,
                         cv::format("pixels=%d (min %d), depth_removed=%d", best_area, init_min_mask_px_, n_depth_removed));
            return std::nullopt;
        }
        const std::string gate_text = in_range.empty() ? std::string("skipped") : cv::format("d0=%.3fm", d0);
        RCLCPP_INFO(this->get_logger(), "Mask cleaned: mask_stamp=%.3f pixels=%d depth_gate=%s removed=%d",
                    mask_stamp_s, best_area, gate_text.c_str(), n_depth_removed);
        return cv::Mat(labels == best_label);
    }

    // Candidate model from a cleaned mask; model coordinates are pixels of the mask's source frame
    std::optional<TargetModel> build_mask_model(const CachedFrame &frame, const cv::Mat &cleaned,
                                                double mask_stamp_s) {
        TargetModel model;
        model.origin = TargetOrigin::MASK;
        model.source_stamp_s = mask_stamp_s;

        std::vector<cv::Point> pixels;
        cv::findNonZero(cleaned, pixels);
        const cv::Rect box = cv::boundingRect(pixels);
        model.bounds = cv::Rect2f(box);
        const float x0 = static_cast<float>(box.x), y0 = static_cast<float>(box.y);
        const float x1 = static_cast<float>(box.x + box.width), y1 = static_cast<float>(box.y + box.height);
        model.corners = {cv::Point2f(x0, y0), cv::Point2f(x1, y0), cv::Point2f(x1, y1), cv::Point2f(x0, y1)};

        const cv::Moments m = cv::moments(cleaned, true);
        cv::Point2f center(static_cast<float>(m.m10 / m.m00), static_cast<float>(m.m01 / m.m00));
        const cv::Point center_px(static_cast<int>(std::lround(center.x)), static_cast<int>(std::lround(center.y)));
        if (!cv::Rect(cv::Point(0, 0), cleaned.size()).contains(center_px) || cleaned.at<uchar>(center_px) == 0) {
            // centroid outside the mask (e.g. a C shape): use the nearest mask pixel
            double best_dist = std::numeric_limits<double>::max();
            cv::Point2f nearest = center;
            for (const auto &p : pixels) {
                const double dist = cv::norm(cv::Point2f(p) - center);
                if (dist < best_dist) {
                    best_dist = dist;
                    nearest = cv::Point2f(p);
                }
            }
            center = nearest;
        }
        model.center = center;

        // features only well inside the mask, where they are least likely to belong to the background
        cv::Mat feature_mask = cleaned;
        if (init_mask_erode_px_ > 0) {
            const int k = 2 * init_mask_erode_px_ + 1;
            cv::erode(cleaned, feature_mask, cv::getStructuringElement(cv::MORPH_ELLIPSE, cv::Size(k, k)));
        }
        orb_->detectAndCompute(frame.gray, feature_mask, model.keypoints, model.descriptors);
        if (static_cast<int>(model.keypoints.size()) < min_matches_) {
            record_event(InitEvent::MASK_FEW_FEATURES, mask_stamp_s,
                         cv::format("features=%zu (min %d), current model kept", model.keypoints.size(), min_matches_));
            return std::nullopt;
        }
        RCLCPP_INFO(this->get_logger(), "Mask target candidate: mask_stamp=%.3f features=%zu box=%dx%d, waiting for handoff",
                    mask_stamp_s, model.keypoints.size(), box.width, box.height);
        return model;
    }

    // Look for the pending candidate in the whole current frame with the same gates as ORB detection.
    // While a candidate waits this is one extra full-frame ORB pass per frame, which noticeably raises the
    // processing time on a Raspberry Pi 5.
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

    // Switch to the candidate that was just found in this frame. The mask comes from the upstream VLM and is
    // trusted, so CONFIRMING and jump confirmation are skipped. Returns the point to publish, if any.
    std::optional<Point3> apply_handoff(const cv::Mat &gray, const Detection &det,
                                        const std::optional<Point3> &measurement, double stamp_s) {
        const double mask_stamp_s = pending_handoff_->mask_stamp_s;
        model_ = std::move(pending_handoff_->model);
        pending_handoff_.reset();

        // tracking data belongs to the old model; restart it from this detection
        clear_klt_points();
        prev_gray_ = gray;
        last_box_ = det.corners;
        frames_since_orb_ok_ = 0;
        reseed_klt(*model_, gray, det);

        const double latency_ms = (stamp_s - mask_stamp_s) * 1000.0;
        handoff_latency_ms_sum_ += latency_ms;
        ++handoff_latency_count_;
        last_handoff_stamp_s_ = stamp_s;
        record_event(InitEvent::HANDOFF_OK, mask_stamp_s,
                     cv::format("latency=%.0f ms features=%zu inliers=%d%s", latency_ms, model_->keypoints.size(),
                                det.n_inliers, measurement ? "" : " (no measurement, state reset to LOST)"));

        if (!measurement) {
            // the held point belongs to the old object; confirm the new one through the normal flow
            set_lost();
            return std::nullopt;
        }
        std::copy(measurement->begin(), measurement->end(), pose_filtered_);
        pose_filter_initialized_ = pose_filter_enable_;
        accept_track(*measurement, det, stamp_s);
        return measurement;
    }

    // Whether KLT / ROI may search near the last box. False when LOST, or when HOLDING for longer than
    // hold.full_search_after_s (the object may have moved anywhere). Uses the last processed frame's stamp,
    // so it gives the same answer at the end of a frame (clearing) and at the start of the next (search).
    bool has_search_anchor() const {
        if (state_ == TrackState::LOST) {
            return false;
        }
        return !(state_ == TrackState::HOLDING && last_frame_stamp_s_ - trusted_.stamp_s > hold_full_search_after_s_);
    }

    // Median of in-range depth values around (u, v), plus why the window may have none
    struct DepthSample {
        std::optional<double> median_m;
        int n_valid = 0;  // within [depth_min_m, depth_max_m]
        int n_near = 0;   // > 0 but < depth_min_m
        int n_far = 0;    // finite and > depth_max_m
    };

    DepthSample sample_depth(const cv::Mat &depth, const std::string &encoding, int u, int v) {
        const int half = depth_window_ / 2;
        const int x0 = std::max(0, u - half), x1 = std::min(depth.cols - 1, u + half);
        const int y0 = std::max(0, v - half), y1 = std::min(depth.rows - 1, v + half);
        const bool is_mm = (encoding == sensor_msgs::image_encodings::TYPE_16UC1 ||
                            encoding == sensor_msgs::image_encodings::MONO16);

        DepthSample sample;
        std::vector<double> values;
        values.reserve(depth_window_ * depth_window_);
        for (int y = y0; y <= y1; ++y) {
            for (int x = x0; x <= x1; ++x) {
                const double d = is_mm ? depth.at<uint16_t>(y, x) * 0.001 : depth.at<float>(y, x);
                // 0 (no data) and NaN are not counted
                if (!std::isfinite(d) || d <= 0.0) {
                    continue;
                }
                if (d < depth_min_m_) {
                    ++sample.n_near;
                } else if (d > depth_max_m_) {
                    ++sample.n_far;
                } else {
                    values.push_back(d);
                }
            }
        }
        sample.n_valid = static_cast<int>(values.size());
        if (values.empty()) {
            return sample;
        }
        std::nth_element(values.begin(), values.begin() + values.size() / 2, values.end());
        sample.median_m = values[values.size() / 2];
        return sample;
    }

    // Default depth for a detection without valid depth, when it looks too far rather than too near
    bool use_depth_fallback(const DepthSample &sample, const Detection &det) const {
        if (!depth_fallback_enable_ || sample.n_near > sample.n_far) {
            return false;
        }
        return depth_fallback_max_box_area_px_ <= 0.0 ||
               cv::contourArea(det.corners) <= depth_fallback_max_box_area_px_;
    }

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
        trusted_.corners = det.corners;
        trusted_.center = det.center;
        trusted_.H = det.H;
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

        if (!measurement) {
            if (has_track && !hold_expired(stamp_s)) {
                state_ = TrackState::HOLDING;
                candidate_count_ = 0;
                return hold_output();
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

    // Debug only: throttled failure reason, and every 2 s the per-status / state / source frame counts
    // plus processing time and depth fallback use. source and process_ms are only given for frames that ran detection.
    void report_status(TrackStatus status, TrackState state, std::optional<TrackSource> source,
                       int n_matches, int n_inliers, std::optional<double> process_ms, bool depth_fallback) {
        if (!is_debug_mode_ || status == TrackStatus::WAITING_INTRINSICS) {
            return;
        }
        if (status != TrackStatus::OK) {
            RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 1000,
                                 "Track failed: %s (matches %d, inliers %d)", to_string(status), n_matches, n_inliers);
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
        int total = 0;
        std::string breakdown;
        for (size_t i = 0; i < kTrackStatusCount; ++i) {
            if (status_counts_[i] > 0) {
                total += status_counts_[i];
                breakdown += cv::format(" %s=%d", to_string(static_cast<TrackStatus>(i)), status_counts_[i]);
            }
        }
        std::string state_breakdown;
        for (size_t i = 0; i < kTrackStateCount; ++i) {
            if (state_counts_[i] > 0) {
                state_breakdown += cv::format(" %s=%d", to_string(static_cast<TrackState>(i)), state_counts_[i]);
            }
        }
        std::string source_breakdown;
        for (size_t i = 0; i < kTrackSourceCount; ++i) {
            if (source_counts_[i] > 0) {
                source_breakdown += cv::format(" %s=%d", to_string(static_cast<TrackSource>(i)), source_counts_[i]);
            }
        }
        std::string init_breakdown;
        if (init_enable_) {
            init_breakdown = " | init:";
            for (size_t i = 0; i < kInitEventCount; ++i) {
                if (event_counts_[i] > 0) {
                    init_breakdown += cv::format(" %s=%d", to_string(static_cast<InitEvent>(i)), event_counts_[i]);
                }
            }
            init_breakdown += handoff_latency_count_ > 0
                                  ? cv::format(" handoff_latency_avg=%.0fms", handoff_latency_ms_sum_ / handoff_latency_count_)
                                  : std::string(" handoff_latency_avg=-");
            event_counts_.fill(0);
            handoff_latency_ms_sum_ = 0.0;
            handoff_latency_count_ = 0;
        }
        const double avg_ms = process_count_ > 0 ? process_ms_sum_ / process_count_ : 0.0;
        const int n_ok = status_counts_[static_cast<size_t>(TrackStatus::OK)];
        RCLCPP_INFO(this->get_logger(),
                    "Track stats: %d frames, success %.1f%% |%s | state:%s | source:%s | depth_fb=%d"
                    " | time: avg %.1f ms, max %.1f ms%s",
                    total, 100.0 * n_ok / total, breakdown.c_str(), state_breakdown.c_str(),
                    source_breakdown.c_str(), depth_fallback_count_, avg_ms, process_ms_max_, init_breakdown.c_str());
        depth_fallback_count_ = 0;
        status_counts_.fill(0);
        state_counts_.fill(0);
        source_counts_.fill(0);
        process_ms_sum_ = 0.0;
        process_ms_max_ = 0.0;
        process_count_ = 0;
        stats_start_ = now;
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
            cv::imshow("ORB Tracker", vis);
            cv::waitKey(1);
        }
    }

    void rgbd_callback(const ImageMsg::ConstSharedPtr &color_msg, const ImageMsg::ConstSharedPtr &depth_msg) {
        if (!has_intrinsics_) {
            RCLCPP_INFO_THROTTLE(this->get_logger(), *this->get_clock(), 2000, "Waiting for camera_info...");
            report_status(TrackStatus::WAITING_INTRINSICS, state_, std::nullopt, 0, 0, std::nullopt, false);
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
        Detection det;
        cv::Mat gray;
        bool ran_detection = false;
        bool handoff_found = false;
        std::optional<double> process_ms;
        if (status == TrackStatus::OK) {
            const auto t_start = std::chrono::steady_clock::now();
            cv::cvtColor(color, gray, cv::COLOR_BGR2GRAY);
            if (model_) {
                det = detect_frame(gray);
                status = det.status;
                ran_detection = true;
            } else {
                status = TrackStatus::NO_TARGET;
            }
            // a pending mask candidate found in this frame replaces the current model's result
            if (std::optional<Detection> handoff = try_handoff(gray, stamp_s)) {
                det = std::move(*handoff);
                status = det.status;
                ran_detection = true;
                handoff_found = true;
            }
            process_ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t_start).count();
            if (init_enable_) {
                cache_frame(gray, depth, depth_msg->encoding, stamp_s);
            }
        }
        const bool found = (status == TrackStatus::OK);

        std::optional<double> depth_m;
        bool depth_fallback = false;  // depth_m is depth_fallback.value_m, not a measurement
        std::optional<Point3> measurement;
        double cam_pt[3] = {0.0, 0.0, 0.0};

        if (found) {
            const int u = static_cast<int>(std::lround(det.center.x));
            const int v = static_cast<int>(std::lround(det.center.y));
            if (u >= 0 && v >= 0 && u < depth.cols && v < depth.rows) {
                const DepthSample sample = sample_depth(depth, depth_msg->encoding, u, v);
                if (sample.median_m) {
                    depth_m = sample.median_m;
                } else if (use_depth_fallback(sample, det)) {
                    // too far for D405: the point keeps the right ray direction, the distance is a guess
                    depth_m = depth_fallback_value_m_;
                    depth_fallback = true;
                } else {
                    status = TrackStatus::NO_DEPTH;
                }
            } else {
                status = TrackStatus::CENTER_OUTSIDE;
            }

            if (depth_m) {
                // deproject pixel to camera optical frame (pinhole, aligned depth shares color intrinsics)
                const double Z = *depth_m;
                cam_pt[0] = (det.center.x - cx_) * Z / fx_;
                cam_pt[1] = (det.center.y - cy_) * Z / fy_;
                cam_pt[2] = Z;

                geometry_msgs::msg::PointStamped cam_msg, world_msg;
                cam_msg.header = color_msg->header;
                cam_msg.point.x = cam_pt[0];
                cam_msg.point.y = cam_pt[1];
                cam_msg.point.z = cam_pt[2];

                if (transform_to_world(cam_msg, world_msg)) {
                    measurement = Point3{world_msg.point.x, world_msg.point.y, world_msg.point.z};
                } else {
                    status = TrackStatus::TF_FAIL;
                }
            }
        }

        const std::optional<Point3> output = handoff_found ? apply_handoff(gray, det, measurement, stamp_s)
                                                           : update_track_state(status, det, measurement, stamp_s);
        if (output) {
            publish_point(*output, color_msg->header);
        }
        last_frame_stamp_s_ = stamp_s;
        const bool full_search = !has_search_anchor();
        if (full_search) {
            // nothing reliable to search near; the next frame searches the full image
            clear_klt_points();
            last_box_.clear();
        }

        const std::optional<TrackSource> source = ran_detection ? std::optional<TrackSource>(det.source) : std::nullopt;
        report_status(status, state_, source, det.n_matches, det.n_inliers, process_ms, depth_fallback);
        if (!is_debug_mode_) {
            return;
        }

        if (status == TrackStatus::OK && state_ == TrackState::TRACKING && output) {
            RCLCPP_INFO(this->get_logger(),
                        "Target inliers %d/%d px:(%.0f, %.0f) cam:(%.3f, %.3f, %.3f) %s:(%.3f, %.3f, %.3f)%s",
                        det.n_inliers, det.n_matches, det.center.x, det.center.y,
                        cam_pt[0], cam_pt[1], cam_pt[2], world_frame_.c_str(),
                        (*output)[0], (*output)[1], (*output)[2], depth_fallback ? " (depth fallback)" : "");
        }

        if (image_debug_ && !color.empty() && debug_image_due()) {
            const cv::Scalar green(0, 255, 0), yellow(0, 255, 255), orange(0, 165, 255), red(0, 0, 255);
            const cv::Scalar cyan(255, 255, 0), blue(255, 0, 0);
            const cv::Scalar state_color = state_ == TrackState::TRACKING   ? green
                                         : state_ == TrackState::HOLDING    ? yellow
                                         : state_ == TrackState::CONFIRMING ? orange
                                                                            : red;
            // TRACKING boxes are colored by source; other states keep their state color
            const cv::Scalar source_color = det.source == TrackSource::ORB_ROI ? cyan
                                          : det.source == TrackSource::KLT     ? blue
                                                                               : green;
            cv::Mat vis = color.clone();
            if (ran_detection && debug_roi_) {
                cv::rectangle(vis, *debug_roi_, cyan, 1);
            }
            for (const auto &pt : klt_scene_pts_) {
                cv::circle(vis, pt, 2, blue, -1);
            }
            auto draw_box = [&vis](const std::vector<cv::Point2f> &corners, const cv::Point2f &center,
                                   const cv::Scalar &box_color) {
                std::vector<cv::Point> poly(corners.begin(), corners.end());
                cv::polylines(vis, poly, true, box_color, 3);
                cv::circle(vis, center, 5, cv::Scalar(0, 0, 255), -1);
            };
            if (state_ == TrackState::HOLDING) {
                // last trusted box; an unconfirmed jump candidate is drawn in orange
                draw_box(trusted_.corners, trusted_.center, yellow);
                if (found) {
                    draw_box(det.corners, det.center, orange);
                }
            } else if (found) {
                draw_box(det.corners, det.center, state_ == TrackState::TRACKING ? source_color : state_color);
            }
            if (found && depth_m) {
                // magenta marks the default depth so it is not mistaken for a measurement
                cv::putText(vis, cv::format(depth_fallback ? "Z=%.3fm FB" : "Z=%.3fm", *depth_m),
                            det.center + cv::Point2f(8, -8), cv::FONT_HERSHEY_SIMPLEX, 0.6,
                            depth_fallback ? cv::Scalar(255, 0, 255) : cv::Scalar(0, 0, 255), 2);
            }
            // line 1: state / status / source; line 2: metrics in fixed-width columns so they do not shift
            const int font = cv::FONT_HERSHEY_SIMPLEX;
            const double font_scale = 0.6;
            const int thickness = 2;
            cv::putText(vis, cv::format("%s %s %s%s %s", to_string(state_), to_string(status),
                                        ran_detection ? to_string(det.source) : "-",
                                        state_ == TrackState::HOLDING && full_search ? " FULL_SEARCH" : "",
                                        model_ ? to_string(model_->origin) : "none"),
                        cv::Point(10, 25), font, font_scale, state_color, thickness);
            // top right: waiting candidate, or a recent switch to a new target
            std::string init_text;
            if (pending_handoff_) {
                init_text = cv::format("HANDOFF %.1fs", stamp_s - pending_handoff_->start_stamp_s);
            } else if (last_handoff_stamp_s_ && stamp_s - *last_handoff_stamp_s_ < 1.0) {
                init_text = "NEW TARGET";
            }
            if (!init_text.empty()) {
                const int text_width = cv::getTextSize(init_text, font, font_scale, thickness, nullptr).width;
                cv::putText(vis, init_text, cv::Point(vis.cols - text_width - 10, 25), font, font_scale,
                            cv::Scalar(255, 0, 255), thickness);
            }
            const std::array<std::pair<std::string, const char *>, 7> metrics = {{
                {cv::format("m=%d", det.n_matches), "m=0000"},
                {cv::format("i=%d", det.n_inliers), "i=0000"},
                {cv::format("r=%.2f", det.inlier_ratio), "r=0.00"},
                {cv::format("e=%.2fpx", det.reproj_error), "e=00.00px"},
                {cv::format("s=%.2f", det.side_ratio), "s=00.00"},
                {cv::format("pts=%zu", klt_scene_pts_.size()), "pts=000"},
                {cv::format("t=%.1fms", process_ms.value_or(0.0)), "t=000.0ms"},
            }};
            int column_x = 10;
            for (const auto &[text, widest] : metrics) {
                cv::putText(vis, text, cv::Point(column_x, 50), font, font_scale, state_color, thickness);
                column_x += cv::getTextSize(widest, font, font_scale, thickness, nullptr).width + 15;
            }
            publish_debug_image(vis, color_msg->header);
        }
    }

    // ROS interfaces
    rclcpp::Subscription<sensor_msgs::msg::CameraInfo>::SharedPtr info_sub_;
    message_filters::Subscriber<ImageMsg> color_sub_;
    message_filters::Subscriber<ImageMsg> depth_sub_;
    std::shared_ptr<message_filters::Synchronizer<SyncPolicy>> sync_;
    rclcpp::Publisher<geometry_msgs::msg::PointStamped>::SharedPtr point_pub_;
    std::unique_ptr<tf2_ros::Buffer> tf_buffer_;
    std::shared_ptr<tf2_ros::TransformListener> tf_listener_;
    std::unique_ptr<tf2_ros::TransformBroadcaster> tf_broadcaster_;
    std::string world_frame_;

    // Target model
    cv::Ptr<cv::ORB> orb_;       // target and ROI search
    cv::Ptr<cv::ORB> orb_full_;  // full-frame search (orb.n_features_full)
    cv::Ptr<cv::BFMatcher> matcher_;
    std::optional<TargetModel> model_;  // empty until a mask arrives when target_image_path is ""

    // Mask init
    bool init_enable_ = false;
    double init_cache_s_ = 3.0;
    double init_stamp_tolerance_s_ = 0.005;
    double init_depth_gate_m_ = 0.04;
    int init_min_mask_px_ = 400;
    int init_mask_erode_px_ = 4;
    double init_handoff_timeout_s_ = 2.0;
    rclcpp::Subscription<ImageMsg>::SharedPtr mask_sub_;
    std::deque<CachedFrame> frame_cache_;
    std::optional<PendingHandoff> pending_handoff_;
    std::optional<double> last_handoff_stamp_s_;
    std::array<int, kInitEventCount> event_counts_{};
    double handoff_latency_ms_sum_ = 0.0;
    int handoff_latency_count_ = 0;

    // Detection params
    int n_features_ = 1000;
    int n_features_full_ = 1000;
    double detect_scale_ = 1.0;
    double ratio_test_ = 0.75;
    int min_matches_ = 15;
    int min_inliers_ = 10;
    double ransac_reproj_thresh_ = 5.0;
    double min_area_px_ = 400.0;
    double gate_min_inlier_ratio_ = 0.3;
    double gate_max_reproj_error_px_ = 3.0;
    double gate_max_side_ratio_ = 3.0;

    // ROI search
    bool roi_enable_ = true;
    double roi_expand_ratio_ = 0.5;
    int roi_min_size_px_ = 160;
    bool roi_full_frame_fallback_ = true;
    std::vector<cv::Point2f> last_box_;  // last box that passed detection, cleared without a search anchor
    std::optional<cv::Rect> debug_roi_;   // ROI searched in the current frame, if any

    // KLT frame-to-frame tracking
    bool klt_enable_ = true;
    int klt_redetect_interval_ = 10;
    int klt_min_points_ = 12;
    int klt_max_points_ = 150;
    bool klt_extra_corners_ = true;
    double klt_fb_max_px_ = 1.0;
    int klt_win_size_ = 21;
    int klt_max_level_ = 3;
    cv::Mat prev_gray_;
    std::vector<cv::Point2f> klt_target_pts_;  // target image coordinates
    std::vector<cv::Point2f> klt_scene_pts_;   // pixel coordinates in prev_gray_ (current frame after tracking)
    int frames_since_orb_ok_ = 0;

    // Depth / intrinsics
    int depth_window_ = 7;
    double depth_min_m_ = 0.07;
    double depth_max_m_ = 0.5;
    bool depth_fallback_enable_ = true;
    double depth_fallback_value_m_ = 0.5;
    double depth_fallback_max_box_area_px_ = 0.0;
    double tf_timeout_s_ = 0.05;
    bool has_intrinsics_ = false;
    double fx_ = 0.0, fy_ = 0.0, cx_ = 0.0, cy_ = 0.0;

    // Filter
    bool pose_filter_enable_ = true;
    bool pose_filter_initialized_ = false;
    double pose_filtered_[3] = {0.0, 0.0, 0.0};

    // Hold / confirm state machine
    bool hold_enable_ = true;
    double hold_timeout_s_ = 0.0;             // <= 0: HOLDING never expires
    double hold_full_search_after_s_ = 0.5;
    double last_frame_stamp_s_ = 0.0;         // image stamp of the last frame through the state machine
    int hold_confirm_frames_ = 3;
    bool hold_publish_ = true;
    TrackState state_ = TrackState::LOST;
    TrustedTrack trusted_;
    Point3 candidate_pt_{};
    int candidate_count_ = 0;

    bool is_debug_mode_ = false;
    bool image_debug_ = false;

    // Debug image output
    image_transport::Publisher debug_image_pub_;
    double debug_image_rate_hz_ = 5.0;
    bool debug_window_ = false;
    std::chrono::steady_clock::time_point last_debug_image_at_{};

    // Status stats (debug only)
    std::array<int, kTrackStatusCount> status_counts_{};
    std::array<int, kTrackStateCount> state_counts_{};
    std::array<int, kTrackSourceCount> source_counts_{};
    double process_ms_sum_ = 0.0;
    double process_ms_max_ = 0.0;
    int process_count_ = 0;
    int depth_fallback_count_ = 0;
    std::chrono::steady_clock::time_point stats_start_ = std::chrono::steady_clock::now();
};

int main(int argc, char **argv) {
    rclcpp::init(argc, argv);
    try {
        auto node = std::make_shared<OrbTrackerNode>();
        rclcpp::spin(node);
    } catch (const std::exception &e) {
        RCLCPP_FATAL(rclcpp::get_logger("orb_tracker_node"), "%s", e.what());
        rclcpp::shutdown();
        return 1;
    }
    rclcpp::shutdown();
    return 0;
}
