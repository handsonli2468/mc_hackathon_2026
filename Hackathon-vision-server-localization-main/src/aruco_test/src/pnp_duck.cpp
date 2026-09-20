#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/image.hpp>
#include <sensor_msgs/msg/camera_info.hpp>
#include <cv_bridge/cv_bridge.h>
#include <opencv2/opencv.hpp>
#include <opencv2/aruco.hpp>
#include <tf2_ros/transform_broadcaster.h>
#include <tf2_ros/transform_listener.h>
#include <tf2_ros/buffer.h>
#include <tf2_geometry_msgs/tf2_geometry_msgs.hpp>
#include <tf2_eigen/tf2_eigen.hpp>
#include <geometry_msgs/msg/transform_stamped.hpp>
#include <geometry_msgs/msg/pose_stamped.hpp>
#include <geometry_msgs/msg/pose_with_covariance_stamped.hpp>
#include <Eigen/Geometry>
#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>

#include "aruco_test/plane_lm.hpp"

// Localize a single robot by solving PnP on its marker, then transforming the pose into the world frame
// with the static camera TF (world -> camera).
// Published estimates:
//   pose_topic          : free 6-DoF PnP (IPPE_SQUARE), z is estimated
//   plane_lm.pose_topic : PnP result moved along its viewing ray onto z = target_height, then refined by
//                         3-DoF (x, y, yaw) LM on image reprojection error with the tag kept level
//   final_pose_topic    : the final localization output (/pose/global), PoseWithCovarianceStamped:
//                         plane LM result, raw PnP when the LM is disabled or not available for this
//                         frame, rotated by final_pose_yaw_offset_deg about z (how the tag is mounted
//                         on the robot). The covariance is a fixed guess from final_pose_cov.*
class PnpDuckNode : public rclcpp::Node {
public:
    PnpDuckNode() : Node("pnp_duck_node") {
        this->declare_parameter<std::string>("RGB_topic", "/camera/camera/color/image_raw");
        this->declare_parameter<std::string>("camera_info_topic", "/camera/camera/color/camera_info");
        this->declare_parameter<std::string>("pose_topic", "/duck/pose/pnp");
        this->declare_parameter<int>("robot.id", 1);
        this->declare_parameter<double>("robot.marker_size", 0.1);
        this->declare_parameter<double>("target_height", 0.2);
        this->declare_parameter<bool>("plane_lm.enable", true);
        this->declare_parameter<std::string>("plane_lm.pose_topic", "/pose/global/pnp_plane_lm");
        this->declare_parameter<std::string>("final_pose_topic", "/pose/global");
        // Fixed 1-sigma guesses for the final pose; x/y are dominated by the extrinsic and the table
        // size (mm level), not by the per-frame LM jitter (~0.1 mm). roll/pitch/z are assumed, not measured.
        // Tag mounting: yaw offset from the tag frame to the robot frame, about z
        this->declare_parameter<double>("final_pose_yaw_offset_deg", -90.0);
        this->declare_parameter<double>("final_pose_cov.sigma_xy_m", 0.005);
        this->declare_parameter<double>("final_pose_cov.sigma_z_m", 0.01);
        this->declare_parameter<double>("final_pose_cov.sigma_yaw_deg", 1.0);
        this->declare_parameter<double>("final_pose_cov.sigma_roll_pitch_deg", 5.0);
        this->declare_parameter<int>("plane_lm.max_iter", 15);
        this->declare_parameter<std::string>("world_frame", "map");
        this->declare_parameter<std::string>("camera_frame", "camera_color_optical_frame");
        this->declare_parameter<bool>("pose_filter.enable", false);
        this->declare_parameter<double>("pose_filter.alpha", 0.1);
        this->declare_parameter<double>("pose_filter.max_jump_m", 0.15);
        this->declare_parameter<bool>("debug.enable", false);
        this->declare_parameter<bool>("debug.img", false);
        this->declare_parameter<std::string>("debug.img_topic", "~/debug/image");
        this->declare_parameter<double>("camera_pose_refresh_s", 1.0);
        RGB_topic_ = this->get_parameter("RGB_topic").as_string();
        camera_info_topic_ = this->get_parameter("camera_info_topic").as_string();
        pose_topic_ = this->get_parameter("pose_topic").as_string();
        robot_id_ = this->get_parameter("robot.id").as_int();
        marker_size_ = this->get_parameter("robot.marker_size").as_double();
        target_height_ = this->get_parameter("target_height").as_double();
        plane_lm_enable_ = this->get_parameter("plane_lm.enable").as_bool();
        plane_lm_pose_topic_ = this->get_parameter("plane_lm.pose_topic").as_string();
        final_pose_topic_ = this->get_parameter("final_pose_topic").as_string();
        final_pose_yaw_offset_ = this->get_parameter("final_pose_yaw_offset_deg").as_double() * M_PI / 180.0;
        const double sigma_xy = this->get_parameter("final_pose_cov.sigma_xy_m").as_double();
        const double sigma_z = this->get_parameter("final_pose_cov.sigma_z_m").as_double();
        const double sigma_yaw = this->get_parameter("final_pose_cov.sigma_yaw_deg").as_double() * M_PI / 180.0;
        const double sigma_rp = this->get_parameter("final_pose_cov.sigma_roll_pitch_deg").as_double() * M_PI / 180.0;
        // Row-major 6x6 over (x, y, z, roll, pitch, yaw)
        final_pose_cov_.fill(0.0);
        final_pose_cov_[0] = sigma_xy * sigma_xy;
        final_pose_cov_[7] = sigma_xy * sigma_xy;
        final_pose_cov_[14] = sigma_z * sigma_z;
        final_pose_cov_[21] = sigma_rp * sigma_rp;
        final_pose_cov_[28] = sigma_rp * sigma_rp;
        final_pose_cov_[35] = sigma_yaw * sigma_yaw;
        plane_lm_max_iter_ = this->get_parameter("plane_lm.max_iter").as_int();
        world_frame_ = this->get_parameter("world_frame").as_string();
        camera_frame_ = this->get_parameter("camera_frame").as_string();
        pose_filter_enable_ = this->get_parameter("pose_filter.enable").as_bool();
        pose_filter_alpha_ = this->get_parameter("pose_filter.alpha").as_double();
        pose_filter_max_jump_m_ = this->get_parameter("pose_filter.max_jump_m").as_double();
        is_debug_mode_ = this->get_parameter("debug.enable").as_bool();
        image_debug_ = this->get_parameter("debug.img").as_bool();
        if (image_debug_) {
            // Debug image as a topic (view with rqt_image_view / RViz) instead of an OpenCV window
            debug_img_pub_ = this->create_publisher<sensor_msgs::msg::Image>(
                this->get_parameter("debug.img_topic").as_string(), 1);
        }
        camera_pose_refresh_s_ = this->get_parameter("camera_pose_refresh_s").as_double();

        tf_buffer_ = std::make_unique<tf2_ros::Buffer>(this->get_clock());
        tf_listener_ = std::make_shared<tf2_ros::TransformListener>(*tf_buffer_);
        tf_broadcaster_ = std::make_unique<tf2_ros::TransformBroadcaster>(*this);
        if (camera_pose_refresh_s_ > 0.0) {
            // Pick up a new extrinsic (e.g. from field_calib_node) without restarting
            camera_pose_timer_ = this->create_wall_timer(
                std::chrono::duration<double>(camera_pose_refresh_s_),
                std::bind(&PnpDuckNode::refresh_camera_pose, this));
        }

        camera_info_subscriber_ = this->create_subscription<sensor_msgs::msg::CameraInfo>(
            camera_info_topic_, 10,
            std::bind(&PnpDuckNode::camera_info_callback, this, std::placeholders::_1));

        RGB_subscriber_ = this->create_subscription<sensor_msgs::msg::Image>(
            RGB_topic_, 10,
            std::bind(&PnpDuckNode::RGB_img_callback, this, std::placeholders::_1));

        pose_pub_ = this->create_publisher<geometry_msgs::msg::PoseStamped>(pose_topic_, 10);
        if (plane_lm_enable_) {
            plane_lm_pose_pub_ = this->create_publisher<geometry_msgs::msg::PoseStamped>(plane_lm_pose_topic_, 10);
        }
        if (!final_pose_topic_.empty()) {
            final_pose_pub_ =
                this->create_publisher<geometry_msgs::msg::PoseWithCovarianceStamped>(final_pose_topic_, 10);
        }

        // TODO: compare DICT_4X4_100 / APRILTAG_36h11 / APRILTAG_16h5 accuracy (see TODO.md)
        dictionary_ = cv::aruco::getPredefinedDictionary(cv::aruco::DICT_APRILTAG_16h5);
        detector_params_ = cv::aruco::DetectorParameters::create();
        detector_params_->cornerRefinementMethod = cv::aruco::CORNER_REFINE_SUBPIX;
        detector_params_->polygonalApproxAccuracyRate = 0.05;
        detector_params_->adaptiveThreshWinSizeMin = 3;
        detector_params_->adaptiveThreshWinSizeMax = 23;
        detector_params_->adaptiveThreshWinSizeStep = 10;

        // Marker corners in marker frame, order required by SOLVEPNP_IPPE_SQUARE
        // top-left, top-right, bottom-right, bottom-left (same as detectMarkers output)
        const float h = static_cast<float>(marker_size_ / 2.0);
        marker_obj_points_ = {
            cv::Point3f(-h,  h, 0.0f),
            cv::Point3f( h,  h, 0.0f),
            cv::Point3f( h, -h, 0.0f),
            cv::Point3f(-h, -h, 0.0f)
        };
        for (const auto &p : marker_obj_points_) {
            marker_plane_points_.emplace_back(p.x, p.y);
        }
    }

private:
    // Separate EMA state per published estimate
    struct PoseFilterState {
        bool initialized = false;
        double pose[3] = {0.0, 0.0, 0.0};
    };

    void pose_filter(PoseFilterState &state, const double raw_pose[3], double filtered_pose[3]) {
        filtered_pose[0] = raw_pose[0];
        filtered_pose[1] = raw_pose[1];
        filtered_pose[2] = raw_pose[2];

        if (!pose_filter_enable_) {
            state.initialized = false;
            return;
        }

        // keep filter tunable without restart (alpha/max_jump can be updated at runtime)
        pose_filter_alpha_ = this->get_parameter("pose_filter.alpha").as_double();
        pose_filter_max_jump_m_ = this->get_parameter("pose_filter.max_jump_m").as_double();
        const double alpha = std::clamp(pose_filter_alpha_, 0.0, 1.0);

        if (!state.initialized) {
            state.pose[0] = raw_pose[0];
            state.pose[1] = raw_pose[1];
            state.pose[2] = raw_pose[2];
            state.initialized = true;
        } else {
            const double dx = raw_pose[0] - state.pose[0];
            const double dy = raw_pose[1] - state.pose[1];
            const double dz = raw_pose[2] - state.pose[2];
            const double dist = std::sqrt(dx * dx + dy * dy + dz * dz);

            if (pose_filter_max_jump_m_ > 0.0 && dist > pose_filter_max_jump_m_) {
                // outlier guard: snap to measurement on big jumps
                state.pose[0] = raw_pose[0];
                state.pose[1] = raw_pose[1];
                state.pose[2] = raw_pose[2];
            } else {
                state.pose[0] = alpha * raw_pose[0] + (1.0 - alpha) * state.pose[0];
                state.pose[1] = alpha * raw_pose[1] + (1.0 - alpha) * state.pose[1];
                state.pose[2] = alpha * raw_pose[2] + (1.0 - alpha) * state.pose[2];
            }
        }

        filtered_pose[0] = state.pose[0];
        filtered_pose[1] = state.pose[1];
        filtered_pose[2] = state.pose[2];
    }

    void camera_info_callback(const sensor_msgs::msg::CameraInfo::SharedPtr msg) {
        if (is_camera_info_received_) {
            return;
        }
        camera_matrix_ = cv::Mat(3, 3, CV_64F);
        for (int i = 0; i < 9; i++) {
            camera_matrix_.at<double>(i / 3, i % 3) = msg->k[i];
        }
        dist_coeffs_ = cv::Mat(msg->d, true).reshape(1, 1);
        is_camera_info_received_ = true;

        RCLCPP_INFO(this->get_logger(), "Camera info received: fx=%.2f fy=%.2f cx=%.2f cy=%.2f, distortion_model=%s",
            msg->k[0], msg->k[4], msg->k[2], msg->k[5], msg->distortion_model.c_str());
        if (!msg->d.empty() && msg->distortion_model != "plumb_bob" && msg->distortion_model != "rational_polynomial") {
            RCLCPP_WARN(this->get_logger(), "Distortion model '%s' is not handled by OpenCV PnP, results may be biased",
                msg->distortion_model.c_str());
        }
    }

    // Re-read world -> camera and rebuild only when the transform actually changed
    void refresh_camera_pose() {
        if (!is_camera_info_received_ || !is_camera_pose_initialized_) {
            return;
        }
        geometry_msgs::msg::TransformStamped tf_msg;
        try {
            tf_msg = tf_buffer_->lookupTransform(world_frame_, camera_frame_, tf2::TimePointZero);
        } catch (tf2::TransformException &) {
            return;
        }
        const Eigen::Isometry3d T_new = tf2::transformToEigen(tf_msg);
        const double moved = (T_new.translation() - T_cam_last_.translation()).norm();
        const double rotated = Eigen::AngleAxisd(T_new.rotation().transpose() * T_cam_last_.rotation()).angle();
        if (moved < 1e-6 && rotated < 1e-8) {
            return;
        }
        get_camera_pose();
        RCLCPP_INFO(this->get_logger(), "camera pose updated: moved %.1f mm, rotated %.3f deg",
            moved * 1000.0, rotated * 180.0 / M_PI);
    }

    void get_camera_pose() {
        try {
            auto tf_msg = tf_buffer_->lookupTransform(world_frame_, camera_frame_, tf2::TimePointZero);
            T_world_cam_ = tf2::transformToEigen(tf_msg);
            T_cam_last_ = T_world_cam_;

            // world -> camera pose for cv::projectPoints in plane LM
            const Eigen::Matrix3d R_cw = T_world_cam_.rotation().transpose();
            const Eigen::Vector3d t_cw = -R_cw * T_world_cam_.translation();
            cv::Mat R_cw_cv = (cv::Mat_<double>(3, 3) <<
                R_cw(0, 0), R_cw(0, 1), R_cw(0, 2),
                R_cw(1, 0), R_cw(1, 1), R_cw(1, 2),
                R_cw(2, 0), R_cw(2, 1), R_cw(2, 2));
            lm_cam_.K = camera_matrix_;
            lm_cam_.D = dist_coeffs_;
            cv::Rodrigues(R_cw_cv, lm_cam_.rvec_cw);
            lm_cam_.tvec_cw = (cv::Mat_<double>(3, 1) << t_cw(0), t_cw(1), t_cw(2));
            is_camera_pose_initialized_ = true;
            RCLCPP_INFO(this->get_logger(), "Camera position: X=%.3f, Y=%.3f, Z=%.3f",
                T_world_cam_.translation().x(), T_world_cam_.translation().y(), T_world_cam_.translation().z());
        }
        catch (tf2::TransformException &ex) {
            RCLCPP_ERROR(this->get_logger(), "Transform exception: %s", ex.what());
        }
    }

    // Move a PnP marker position along the camera viewing ray onto the plane z = target_height_.
    // PnP depth is the least reliable part of its pose, the ray direction is well constrained.
    bool project_onto_target_plane(const Eigen::Vector3d &p_world, double &X, double &Y) const {
        const Eigen::Vector3d c = T_world_cam_.translation();
        const double dz = p_world.z() - c.z();
        if (std::abs(dz) < 1e-9) {
            return false;
        }
        const double s = (target_height_ - c.z()) / dz;
        if (s <= 0.0) {
            return false;  // plane behind the camera along this ray
        }
        X = c.x() + s * (p_world.x() - c.x());
        Y = c.y() + s * (p_world.y() - c.y());
        return true;
    }

    static double wrap_angle(double a) {
        return std::atan2(std::sin(a), std::cos(a));
    }

    static Eigen::Isometry3d cv_pose_to_eigen(const cv::Mat &rvec, const cv::Mat &tvec) {
        cv::Mat R_cv;
        cv::Rodrigues(rvec, R_cv);
        Eigen::Isometry3d T = Eigen::Isometry3d::Identity();
        for (int r = 0; r < 3; r++) {
            for (int c = 0; c < 3; c++) {
                T.linear()(r, c) = R_cv.at<double>(r, c);
            }
            T.translation()(r) = tvec.at<double>(r);
        }
        return T;
    }

    double reprojection_rms(const std::vector<cv::Point2f> &img_points, const cv::Mat &rvec, const cv::Mat &tvec) const {
        std::vector<cv::Point2f> projected;
        cv::projectPoints(marker_obj_points_, rvec, tvec, camera_matrix_, dist_coeffs_, projected);
        double sum_sq = 0.0;
        for (size_t i = 0; i < projected.size(); i++) {
            const cv::Point2f d = projected[i] - img_points[i];
            sum_sq += d.x * d.x + d.y * d.y;
        }
        return std::sqrt(sum_sq / projected.size());
    }

    void publish_debug_image(const cv::Mat &bgr, const std_msgs::msg::Header &header) {
        if (debug_img_pub_) {
            debug_img_pub_->publish(*cv_bridge::CvImage(header, "bgr8", bgr).toImageMsg());
        }
    }

    void RGB_img_callback(const sensor_msgs::msg::Image::SharedPtr msg) {
        if (!is_camera_info_received_ || !is_camera_pose_initialized_) {
            RCLCPP_INFO(this->get_logger(), "Waiting for camera info and camera position...");
            if (is_camera_info_received_) {
                get_camera_pose();
            }
            return;
        }

        cv_bridge::CvImageConstPtr cv_ptr;
        try {
            cv_ptr = cv_bridge::toCvCopy(msg, sensor_msgs::image_encodings::BGR8);
        } catch (cv_bridge::Exception &e) {
            RCLCPP_ERROR(this->get_logger(), "cv_bridge exception: %s", e.what());
            return;
        }
        cv::Mat RGB_frame = cv_ptr->image;

        std::vector<int> marker_ids;
        std::vector<std::vector<cv::Point2f>> marker_corners, rejected_candidates;
        cv::aruco::detectMarkers(RGB_frame, dictionary_, marker_corners, marker_ids, detector_params_, rejected_candidates);

        int target_idx = -1;
        for (size_t i = 0; i < marker_ids.size(); i++) {
            if (marker_ids[i] == robot_id_) {
                target_idx = static_cast<int>(i);
                break;
            }
        }

        double raw_pose[3] = {0.0, 0.0, 0.0};
        double final_pose[3] = {0.0, 0.0, 0.0};
        double yaw_rad = 0.0;
        double yaw_deg = 0.0;
        double err_best = 0.0;
        double err_other = -1.0;
        cv::Mat rvec_best, tvec_best;
        bool is_pose_valid = false;
        double lm_raw_pose[3] = {0.0, 0.0, target_height_};
        double lm_final_pose[3] = {0.0, 0.0, 0.0};
        double lm_init[3] = {0.0, 0.0, 0.0};  // x, y, yaw after moving PnP onto the plane
        double lm_yaw_rad = 0.0;
        double lm_rms = 0.0;
        bool is_lm_valid = false;
        geometry_msgs::msg::PoseStamped lm_pose_msg;

        if (target_idx >= 0) {
            const auto &corners = marker_corners[target_idx];
            std::vector<cv::Mat> rvecs, tvecs;
            const int n_solutions = cv::solvePnPGeneric(
                marker_obj_points_, corners, camera_matrix_, dist_coeffs_, rvecs, tvecs,
                false, cv::SOLVEPNP_IPPE_SQUARE);

            // IPPE gives two candidate poses for a planar square (flip ambiguity).
            // Overhead camera: pick the one whose marker normal (+z) points closest to world +Z.
            int best = -1;
            double best_up = -2.0;
            Eigen::Isometry3d T_world_marker_best;
            for (int k = 0; k < n_solutions; k++) {
                const Eigen::Isometry3d T_world_marker = T_world_cam_ * cv_pose_to_eigen(rvecs[k], tvecs[k]);
                const double up = T_world_marker.linear()(2, 2);
                if (up > best_up) {
                    best_up = up;
                    best = k;
                    T_world_marker_best = T_world_marker;
                }
            }

            if (best >= 0) {
                rvec_best = rvecs[best];
                tvec_best = tvecs[best];
                err_best = reprojection_rms(corners, rvec_best, tvec_best);
                if (n_solutions > 1) {
                    err_other = reprojection_rms(corners, rvecs[1 - best], tvecs[1 - best]);
                }

                raw_pose[0] = T_world_marker_best.translation().x();
                raw_pose[1] = T_world_marker_best.translation().y();
                raw_pose[2] = T_world_marker_best.translation().z();
                pose_filter(pose_filter_state_, raw_pose, final_pose);

                // yaw from marker x-axis (left -> right) projected on the world XY plane
                const Eigen::Matrix3d &R = T_world_marker_best.linear();
                yaw_rad = std::atan2(R(1, 0), R(0, 0));
                yaw_deg = yaw_rad * 180.0 / CV_PI;

                geometry_msgs::msg::PoseStamped pose_msg;
                pose_msg.header.stamp = msg->header.stamp;
                pose_msg.header.frame_id = world_frame_;
                pose_msg.pose.position.x = final_pose[0];
                pose_msg.pose.position.y = final_pose[1];
                pose_msg.pose.position.z = final_pose[2];

                // roll = pitch = 0, yaw = yaw_rad
                const double half_yaw = yaw_rad * 0.5;
                pose_msg.pose.orientation.x = 0.0;
                pose_msg.pose.orientation.y = 0.0;
                pose_msg.pose.orientation.z = std::sin(half_yaw);
                pose_msg.pose.orientation.w = std::cos(half_yaw);

                pose_pub_->publish(pose_msg);
                is_pose_valid = true;

                // Plane-constrained refinement seeded by PnP: keep the viewing ray, fix the depth
                // with the known tag height, then refine (x, y, yaw) against the raw corners.
                if (plane_lm_enable_ &&
                    project_onto_target_plane(T_world_marker_best.translation(), lm_init[0], lm_init[1])) {
                    lm_init[2] = yaw_rad;
                    double plane_pose[3] = {lm_init[0], lm_init[1], lm_init[2]};
                    lm_rms = aruco_test::refine_plane_lm(corners, marker_plane_points_, target_height_, lm_cam_,
                                                         plane_lm_max_iter_, plane_pose);
                    lm_raw_pose[0] = plane_pose[0];
                    lm_raw_pose[1] = plane_pose[1];
                    lm_yaw_rad = plane_pose[2];
                    pose_filter(lm_pose_filter_state_, lm_raw_pose, lm_final_pose);

                    lm_pose_msg.header.stamp = msg->header.stamp;
                    lm_pose_msg.header.frame_id = world_frame_;
                    lm_pose_msg.pose.position.x = lm_final_pose[0];
                    lm_pose_msg.pose.position.y = lm_final_pose[1];
                    lm_pose_msg.pose.position.z = lm_final_pose[2];
                    lm_pose_msg.pose.orientation.z = std::sin(lm_yaw_rad * 0.5);
                    lm_pose_msg.pose.orientation.w = std::cos(lm_yaw_rad * 0.5);
                    plane_lm_pose_pub_->publish(lm_pose_msg);
                    is_lm_valid = true;
                }

                // Final localization output
                if (final_pose_pub_) {
                    const geometry_msgs::msg::PoseStamped &best = is_lm_valid ? lm_pose_msg : pose_msg;
                    geometry_msgs::msg::PoseWithCovarianceStamped final_msg;
                    final_msg.header = best.header;
                    final_msg.pose.pose = best.pose;
                    // Rotate about z into the robot frame (tag mounting); the position is unchanged
                    const double tag_yaw = 2.0 * std::atan2(best.pose.orientation.z, best.pose.orientation.w);
                    const double half_yaw = wrap_angle(tag_yaw + final_pose_yaw_offset_) * 0.5;
                    final_msg.pose.pose.orientation.x = 0.0;
                    final_msg.pose.pose.orientation.y = 0.0;
                    final_msg.pose.pose.orientation.z = std::sin(half_yaw);
                    final_msg.pose.pose.orientation.w = std::cos(half_yaw);
                    final_msg.pose.covariance = final_pose_cov_;
                    final_pose_pub_->publish(final_msg);
                }
            }
        }

        // ==========================================
        // Debug
        // ==========================================
        if (is_debug_mode_) {
            if (is_pose_valid) {
                RCLCPP_INFO(this->get_logger(),
                    "Target 3D raw:(%.3f, %.3f, %.3f) filtered:(%.3f, %.3f, %.3f), Yaw: %.3f rad %.3f deg, "
                    "reproj err: %.3f px (other solution: %.3f px)",
                    raw_pose[0], raw_pose[1], raw_pose[2],
                    final_pose[0], final_pose[1], final_pose[2],
                    yaw_rad, yaw_deg,
                    err_best, err_other
                );
                // broadcast TF for debugging
                geometry_msgs::msg::TransformStamped t;
                t.header.stamp = msg->header.stamp;
                t.header.frame_id = world_frame_;
                t.child_frame_id = "pnp_duck_" + std::to_string(robot_id_);
                t.transform.translation.x = final_pose[0];
                t.transform.translation.y = final_pose[1];
                t.transform.translation.z = final_pose[2];
                const double half_yaw = yaw_rad * 0.5;
                t.transform.rotation.x = 0.0;
                t.transform.rotation.y = 0.0;
                t.transform.rotation.z = std::sin(half_yaw);
                t.transform.rotation.w = std::cos(half_yaw);
                tf_broadcaster_->sendTransform(t);
            }
            if (is_lm_valid) {
                RCLCPP_INFO(this->get_logger(),
                    "PlaneLM init:(%.3f, %.3f, %.2f deg) raw:(%.3f, %.3f, %.3f) filtered:(%.3f, %.3f, %.3f), "
                    "Yaw: %.3f rad %.3f deg, reproj RMS: %.3f px",
                    lm_init[0], lm_init[1], lm_init[2] * 180.0 / CV_PI,
                    lm_raw_pose[0], lm_raw_pose[1], lm_raw_pose[2],
                    lm_final_pose[0], lm_final_pose[1], lm_final_pose[2],
                    lm_yaw_rad, lm_yaw_rad * 180.0 / CV_PI, lm_rms);
                geometry_msgs::msg::TransformStamped t;
                t.header = lm_pose_msg.header;
                t.child_frame_id = "pnp_plane_lm_duck_" + std::to_string(robot_id_);
                t.transform.translation.x = lm_final_pose[0];
                t.transform.translation.y = lm_final_pose[1];
                t.transform.translation.z = lm_final_pose[2];
                t.transform.rotation = lm_pose_msg.pose.orientation;
                tf_broadcaster_->sendTransform(t);
            }

            if (image_debug_) {
                if (!marker_ids.empty()) {
                    cv::aruco::drawDetectedMarkers(RGB_frame, marker_corners, marker_ids);
                }
                // draw rejected candidates (purple) for debugging
                if (!rejected_candidates.empty()) {
                    cv::aruco::drawDetectedMarkers(RGB_frame, rejected_candidates, cv::noArray(), cv::Scalar(255, 0, 255));
                }
                if (is_pose_valid) {
                    cv::drawFrameAxes(RGB_frame, camera_matrix_, dist_coeffs_, rvec_best, tvec_best,
                                      static_cast<float>(marker_size_));
                }
                // draw plane-LM fitted corners (yellow) to check the fit against the detection
                if (is_lm_valid) {
                    Eigen::Matrix<double, 8, 1> r;
                    const Eigen::Vector3d p(lm_raw_pose[0], lm_raw_pose[1], lm_yaw_rad);
                    const auto &corners = marker_corners[target_idx];
                    aruco_test::plane_residual(p, corners, marker_plane_points_, target_height_, lm_cam_, r);
                    for (int i = 0; i < 4; i++) {
                        const cv::Point2f fitted(corners[i].x + r(2 * i), corners[i].y + r(2 * i + 1));
                        cv::circle(RGB_frame, fitted, 3, cv::Scalar(0, 255, 255), -1);
                    }
                }
                publish_debug_image(RGB_frame, msg->header);
            }
        }
    }

    rclcpp::Subscription<sensor_msgs::msg::Image>::SharedPtr RGB_subscriber_;
    rclcpp::Subscription<sensor_msgs::msg::CameraInfo>::SharedPtr camera_info_subscriber_;
    rclcpp::Publisher<geometry_msgs::msg::PoseStamped>::SharedPtr pose_pub_;
    rclcpp::Publisher<geometry_msgs::msg::PoseStamped>::SharedPtr plane_lm_pose_pub_;
    rclcpp::Publisher<geometry_msgs::msg::PoseWithCovarianceStamped>::SharedPtr final_pose_pub_;
    std::string final_pose_topic_;
    double final_pose_yaw_offset_ = 0.0;
    std::array<double, 36> final_pose_cov_{};
    std::string RGB_topic_;
    std::string camera_info_topic_;
    std::string pose_topic_;
    std::unique_ptr<tf2_ros::Buffer> tf_buffer_;
    std::shared_ptr<tf2_ros::TransformListener> tf_listener_;
    std::unique_ptr<tf2_ros::TransformBroadcaster> tf_broadcaster_;
    std::string world_frame_;
    std::string camera_frame_;

    cv::Mat camera_matrix_;
    cv::Mat dist_coeffs_;
    Eigen::Isometry3d T_world_cam_ = Eigen::Isometry3d::Identity();
    std::vector<cv::Point3f> marker_obj_points_;
    std::vector<cv::Point2d> marker_plane_points_;  // same corners, 2D, for plane LM
    aruco_test::PlaneLmCamera lm_cam_;              // K, D, world -> camera pose for plane LM

    int robot_id_ = 1;
    double marker_size_ = 0.1;
    double target_height_ = 0.2;

    bool plane_lm_enable_ = true;
    int plane_lm_max_iter_ = 15;
    std::string plane_lm_pose_topic_;

    bool is_debug_mode_ = false;
    bool image_debug_ = false;
    rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr debug_img_pub_;

    bool is_camera_info_received_ = false;
    bool is_camera_pose_initialized_ = false;
    double camera_pose_refresh_s_ = 1.0;
    rclcpp::TimerBase::SharedPtr camera_pose_timer_;
    Eigen::Isometry3d T_cam_last_ = Eigen::Isometry3d::Identity();  // last applied world -> camera
    bool pose_filter_enable_ = true;
    double pose_filter_alpha_ = 0.2;
    double pose_filter_max_jump_m_ = 0.5;
    PoseFilterState pose_filter_state_;
    PoseFilterState lm_pose_filter_state_;

    cv::Ptr<cv::aruco::Dictionary> dictionary_;
    cv::Ptr<cv::aruco::DetectorParameters> detector_params_;
};

int main(int argc, char **argv) {
    rclcpp::init(argc, argv);
    auto node = std::make_shared<PnpDuckNode>();
    rclcpp::spin(node);
    rclcpp::shutdown();
    return 0;
}
