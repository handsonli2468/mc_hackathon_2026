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
#include <Eigen/Geometry>
#include <algorithm>
#include <chrono>
#include <cmath>

#include "aruco_test/plane_lm.hpp"

// Localize a single robot whose tag lies on a known level plane z = target_height.
// No field markers: H is derived from the static camera TF (world -> camera) and camera intrinsics.
// Two estimates are published:
//   pose_topic          : 4 corners ray-cast onto z = target_height, then 2D rigid fit (closed form)
//   plane_lm.pose_topic : the above refined by 3-DoF (x, y, yaw) LM on image reprojection error
class HomographyDuckNode : public rclcpp::Node {
public:
    HomographyDuckNode() : Node("homography_duck_node") {
        this->declare_parameter<std::string>("RGB_topic", "/camera/camera/color/image_raw");
        this->declare_parameter<std::string>("camera_info_topic", "/camera/camera/color/camera_info");
        this->declare_parameter<std::string>("pose_topic", "/duck/pose/homography");
        this->declare_parameter<double>("target_height", 0.2);
        this->declare_parameter<int>("robot.id", 1);
        this->declare_parameter<double>("robot.marker_size", 0.1);
        this->declare_parameter<bool>("plane_lm.enable", true);
        this->declare_parameter<std::string>("plane_lm.pose_topic", "/duck/pose/plane_lm");
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
        target_height_ = this->get_parameter("target_height").as_double();
        robot_id_ = this->get_parameter("robot.id").as_int();
        marker_size_ = this->get_parameter("robot.marker_size").as_double();
        plane_lm_enable_ = this->get_parameter("plane_lm.enable").as_bool();
        plane_lm_pose_topic_ = this->get_parameter("plane_lm.pose_topic").as_string();
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
                std::bind(&HomographyDuckNode::refresh_camera_pose, this));
        }

        camera_info_subscriber_ = this->create_subscription<sensor_msgs::msg::CameraInfo>(
            camera_info_topic_, 10,
            std::bind(&HomographyDuckNode::camera_info_callback, this, std::placeholders::_1));

        RGB_subscriber_ = this->create_subscription<sensor_msgs::msg::Image>(
            RGB_topic_, 10,
            std::bind(&HomographyDuckNode::RGB_img_callback, this, std::placeholders::_1));

        pose_pub_ = this->create_publisher<geometry_msgs::msg::PoseStamped>(pose_topic_, 10);
        if (plane_lm_enable_) {
            plane_lm_pose_pub_ = this->create_publisher<geometry_msgs::msg::PoseStamped>(plane_lm_pose_topic_, 10);
        }

        // TODO: compare DICT_4X4_100 / APRILTAG_36h11 / APRILTAG_16h5 accuracy (see TODO.md)
        dictionary_ = cv::aruco::getPredefinedDictionary(cv::aruco::DICT_APRILTAG_16h5);
        detector_params_ = cv::aruco::DetectorParameters::create();
        detector_params_->cornerRefinementMethod = cv::aruco::CORNER_REFINE_SUBPIX;
        detector_params_->polygonalApproxAccuracyRate = 0.05;
        detector_params_->adaptiveThreshWinSizeMin = 3;
        detector_params_->adaptiveThreshWinSizeMax = 23;
        detector_params_->adaptiveThreshWinSizeStep = 10;

        // Marker corners in marker frame (z up), same order as detectMarkers output:
        // top-left, top-right, bottom-right, bottom-left
        const double h = marker_size_ / 2.0;
        marker_obj_points_ = {
            cv::Point2d(-h,  h),
            cv::Point2d( h,  h),
            cv::Point2d( h, -h),
            cv::Point2d(-h, -h)
        };
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
            RCLCPP_WARN(this->get_logger(), "Distortion model '%s' is not handled by cv::undistortPoints, results may be biased",
                msg->distortion_model.c_str());
        }
    }

    // Build the image -> ground(z=0) homography from camera extrinsics and intrinsics
    // Re-read world -> camera and rebuild only when the transform actually changed
    void refresh_camera_pose() {
        if (!is_camera_info_received_ || !is_camera_position_initialized_) {
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
        init_ground_homography();
        RCLCPP_INFO(this->get_logger(), "camera pose updated: moved %.1f mm, rotated %.3f deg",
            moved * 1000.0, rotated * 180.0 / M_PI);
    }

    void init_ground_homography() {
        if (!is_camera_info_received_) {
            return;
        }
        try {
            auto tf_msg = tf_buffer_->lookupTransform(world_frame_, camera_frame_, tf2::TimePointZero);
            const Eigen::Isometry3d T_world_cam = tf2::transformToEigen(tf_msg);
            T_cam_last_ = T_world_cam;

            cam_x_ = T_world_cam.translation().x();
            cam_y_ = T_world_cam.translation().y();
            cam_z_ = T_world_cam.translation().z();

            // world -> camera
            const Eigen::Matrix3d R_cw = T_world_cam.rotation().transpose();
            const Eigen::Vector3d t_cw = -R_cw * T_world_cam.translation();

            // For points on z=0: s * [u, v, 1]^T = K * [r1 r2 t] * [X, Y, 1]^T
            cv::Mat Rt = (cv::Mat_<double>(3, 3) <<
                R_cw(0, 0), R_cw(0, 1), t_cw(0),
                R_cw(1, 0), R_cw(1, 1), t_cw(1),
                R_cw(2, 0), R_cw(2, 1), t_cw(2));
            H_world_to_img_ = camera_matrix_ * Rt;
            H_ = H_world_to_img_.inv();
            H_ /= H_.at<double>(2, 2);

            // Full world -> camera pose for cv::projectPoints in plane LM
            cv::Mat R_cw_cv = (cv::Mat_<double>(3, 3) <<
                R_cw(0, 0), R_cw(0, 1), R_cw(0, 2),
                R_cw(1, 0), R_cw(1, 1), R_cw(1, 2),
                R_cw(2, 0), R_cw(2, 1), R_cw(2, 2));
            lm_cam_.K = camera_matrix_;
            lm_cam_.D = dist_coeffs_;
            cv::Rodrigues(R_cw_cv, lm_cam_.rvec_cw);
            lm_cam_.tvec_cw = (cv::Mat_<double>(3, 1) << t_cw(0), t_cw(1), t_cw(2));

            is_camera_position_initialized_ = true;
            RCLCPP_INFO(this->get_logger(), "Camera position: X=%.3f, Y=%.3f, Z=%.3f", cam_x_, cam_y_, cam_z_);
            RCLCPP_INFO(this->get_logger(), "Ground homography (image -> world):\n[%.6f, %.6f, %.6f]\n[%.6f, %.6f, %.6f]\n[%.6f, %.6f, %.6f]",
                H_.at<double>(0, 0), H_.at<double>(0, 1), H_.at<double>(0, 2),
                H_.at<double>(1, 0), H_.at<double>(1, 1), H_.at<double>(1, 2),
                H_.at<double>(2, 0), H_.at<double>(2, 1), H_.at<double>(2, 2));
        }
        catch (tf2::TransformException &ex) {
            RCLCPP_ERROR(this->get_logger(), "Transform exception: %s", ex.what());
        }
    }

    // Apply H to a pixel, return false if the result is at infinity
    bool pixel_to_ground(const cv::Point2f &px, double &X, double &Y) const {
        cv::Mat pt_src = (cv::Mat_<double>(3, 1) << px.x, px.y, 1.0);
        cv::Mat pt_dst = H_ * pt_src;
        const double w = pt_dst.at<double>(2, 0);
        if (std::abs(w) < 1e-12) {
            return false;
        }
        X = pt_dst.at<double>(0, 0) / w;
        Y = pt_dst.at<double>(1, 0) / w;
        return true;
    }

    // Intersect the ray through an undistorted pixel with the level plane z = target_height_:
    // project to z=0 ground, then scale toward the camera center (similar triangles)
    bool pixel_to_target_plane(const cv::Point2f &px, double &X, double &Y) const {
        double X_g, Y_g;
        if (!pixel_to_ground(px, X_g, Y_g)) {
            return false;
        }
        const double t = (cam_z_ - target_height_) / cam_z_;
        X = cam_x_ + t * (X_g - cam_x_);
        Y = cam_y_ + t * (Y_g - cam_y_);
        return true;
    }

    // Closed-form (x, y, yaw): ray-cast all 4 undistorted corners onto the target plane,
    // then least-squares 2D rigid fit of the marker square (Procrustes, no scale).
    // For a square the fitted center is the corner centroid, so marker size is not needed.
    bool estimate_plane_rigid(const std::vector<cv::Point2f> &undist_corners, double pose[3]) const {
        cv::Point2d dst[4];
        cv::Point2d dst_mean(0.0, 0.0);
        for (int i = 0; i < 4; i++) {
            if (!pixel_to_target_plane(undist_corners[i], dst[i].x, dst[i].y)) {
                return false;
            }
            dst_mean += dst[i] * 0.25;
        }
        double s_cross = 0.0, s_dot = 0.0;
        for (int i = 0; i < 4; i++) {
            const cv::Point2d &a = marker_obj_points_[i];  // already centered
            const cv::Point2d b = dst[i] - dst_mean;
            s_cross += a.x * b.y - a.y * b.x;
            s_dot += a.x * b.x + a.y * b.y;
        }
        pose[0] = dst_mean.x;
        pose[1] = dst_mean.y;
        pose[2] = std::atan2(s_cross, s_dot);
        return true;
    }

    geometry_msgs::msg::PoseStamped make_pose_msg(const std_msgs::msg::Header &header, const double pos[3], double yaw) const {
        geometry_msgs::msg::PoseStamped pose_msg;
        pose_msg.header.stamp = header.stamp;
        pose_msg.header.frame_id = world_frame_;
        pose_msg.pose.position.x = pos[0];
        pose_msg.pose.position.y = pos[1];
        pose_msg.pose.position.z = pos[2];
        // roll = pitch = 0, yaw = yaw
        pose_msg.pose.orientation.x = 0.0;
        pose_msg.pose.orientation.y = 0.0;
        pose_msg.pose.orientation.z = std::sin(yaw * 0.5);
        pose_msg.pose.orientation.w = std::cos(yaw * 0.5);
        return pose_msg;
    }

    void broadcast_debug_tf(const geometry_msgs::msg::PoseStamped &pose_msg, const std::string &child_frame) {
        geometry_msgs::msg::TransformStamped t;
        t.header = pose_msg.header;
        t.child_frame_id = child_frame;
        t.transform.translation.x = pose_msg.pose.position.x;
        t.transform.translation.y = pose_msg.pose.position.y;
        t.transform.translation.z = pose_msg.pose.position.z;
        t.transform.rotation = pose_msg.pose.orientation;
        tf_broadcaster_->sendTransform(t);
    }

    // Draw world origin and 1 m X/Y axes projected on the ground, for checking extrinsics by eye
    void draw_world_axes(cv::Mat &img) const {
        auto project = [this](double X, double Y, cv::Point &out) {
            cv::Mat p = H_world_to_img_ * (cv::Mat_<double>(3, 1) << X, Y, 1.0);
            const double w = p.at<double>(2, 0);
            if (w <= 1e-9) {
                return false;  // behind camera
            }
            out = cv::Point(cvRound(p.at<double>(0, 0) / w), cvRound(p.at<double>(1, 0) / w));
            return true;
        };
        cv::Point o, px, py;
        if (!project(0.0, 0.0, o)) {
            return;
        }
        if (project(1.0, 0.0, px)) {
            cv::line(img, o, px, cv::Scalar(0, 0, 255), 2);
            cv::putText(img, "X", px, cv::FONT_HERSHEY_SIMPLEX, 0.6, cv::Scalar(0, 0, 255), 2);
        }
        if (project(0.0, 1.0, py)) {
            cv::line(img, o, py, cv::Scalar(0, 255, 0), 2);
            cv::putText(img, "Y", py, cv::FONT_HERSHEY_SIMPLEX, 0.6, cv::Scalar(0, 255, 0), 2);
        }
    }

    void publish_debug_image(const cv::Mat &bgr, const std_msgs::msg::Header &header) {
        if (debug_img_pub_) {
            debug_img_pub_->publish(*cv_bridge::CvImage(header, "bgr8", bgr).toImageMsg());
        }
    }

    void RGB_img_callback(const sensor_msgs::msg::Image::SharedPtr msg) {
        if (!is_camera_position_initialized_) {
            RCLCPP_INFO(this->get_logger(), "Waiting for camera info and camera position...");
            init_ground_homography();
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

        std::vector<cv::Point2f> raw_corners, target_corners;
        bool is_target_found = false;
        for (size_t i = 0; i < marker_ids.size(); i++) {
            if (marker_ids[i] == robot_id_) {
                raw_corners = marker_corners[i];
                // H is built for an ideal pinhole camera, so remove lens distortion first
                cv::undistortPoints(raw_corners, target_corners, camera_matrix_, dist_coeffs_, cv::noArray(), camera_matrix_);
                is_target_found = true;
                break;
            }
        }

        double raw_pose[3] = {0.0, 0.0, target_height_};
        double final_pose[3] = {0.0, 0.0, 0.0};
        double yaw_rad = 0.0;
        double lm_raw_pose[3] = {0.0, 0.0, target_height_};
        double lm_final_pose[3] = {0.0, 0.0, 0.0};
        double lm_yaw_rad = 0.0;
        double lm_rms = 0.0;
        bool is_pose_valid = false;
        bool is_lm_valid = false;
        geometry_msgs::msg::PoseStamped pose_msg, lm_pose_msg;

        if (is_target_found && target_corners.size() == 4) {
            double plane_pose[3];  // x, y, yaw
            if (estimate_plane_rigid(target_corners, plane_pose)) {
                raw_pose[0] = plane_pose[0];
                raw_pose[1] = plane_pose[1];
                yaw_rad = plane_pose[2];
                pose_filter(pose_filter_state_, raw_pose, final_pose);
                pose_msg = make_pose_msg(msg->header, final_pose, yaw_rad);
                pose_pub_->publish(pose_msg);
                is_pose_valid = true;

                if (plane_lm_enable_) {
                    lm_rms = aruco_test::refine_plane_lm(raw_corners, marker_obj_points_, target_height_, lm_cam_,
                                                         plane_lm_max_iter_, plane_pose);
                    lm_raw_pose[0] = plane_pose[0];
                    lm_raw_pose[1] = plane_pose[1];
                    lm_yaw_rad = plane_pose[2];
                    pose_filter(lm_pose_filter_state_, lm_raw_pose, lm_final_pose);
                    lm_pose_msg = make_pose_msg(msg->header, lm_final_pose, lm_yaw_rad);
                    plane_lm_pose_pub_->publish(lm_pose_msg);
                    is_lm_valid = true;
                }
            }
        }

        // ==========================================
        // Debug
        // ==========================================
        if (is_debug_mode_) {
            if (is_pose_valid) {
                RCLCPP_INFO(this->get_logger(),
                    "Rigid raw:(%.3f, %.3f, %.3f) filtered:(%.3f, %.3f, %.3f), Yaw: %.3f rad %.3f deg",
                    raw_pose[0], raw_pose[1], raw_pose[2],
                    final_pose[0], final_pose[1], final_pose[2],
                    yaw_rad, yaw_rad * 180.0 / CV_PI);
                broadcast_debug_tf(pose_msg, "homo_duck_" + std::to_string(robot_id_));
            }
            if (is_lm_valid) {
                RCLCPP_INFO(this->get_logger(),
                    "PlaneLM raw:(%.3f, %.3f, %.3f) filtered:(%.3f, %.3f, %.3f), Yaw: %.3f rad %.3f deg, reproj RMS: %.3f px",
                    lm_raw_pose[0], lm_raw_pose[1], lm_raw_pose[2],
                    lm_final_pose[0], lm_final_pose[1], lm_final_pose[2],
                    lm_yaw_rad, lm_yaw_rad * 180.0 / CV_PI, lm_rms);
                broadcast_debug_tf(lm_pose_msg, "plane_lm_duck_" + std::to_string(robot_id_));
            }

            if (image_debug_) {
                if (!marker_ids.empty()) {
                    cv::aruco::drawDetectedMarkers(RGB_frame, marker_corners, marker_ids);
                }
                // draw rejected candidates (purple) for debugging
                if (!rejected_candidates.empty()) {
                    cv::aruco::drawDetectedMarkers(RGB_frame, rejected_candidates, cv::noArray(), cv::Scalar(255, 0, 255));
                }
                draw_world_axes(RGB_frame);
                // draw plane-LM fitted corners (yellow) to check the fit against the detection
                if (is_lm_valid) {
                    Eigen::Matrix<double, 8, 1> r;
                    const Eigen::Vector3d p(lm_raw_pose[0], lm_raw_pose[1], lm_yaw_rad);
                    aruco_test::plane_residual(p, raw_corners, marker_obj_points_, target_height_, lm_cam_, r);
                    for (int i = 0; i < 4; i++) {
                        const cv::Point2f fitted(raw_corners[i].x + r(2 * i), raw_corners[i].y + r(2 * i + 1));
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
    cv::Mat H_;               // image -> world ground (z=0)
    cv::Mat H_world_to_img_;  // world ground (z=0) -> image

    double cam_x_ = 0.0, cam_y_ = 0.0, cam_z_ = 0.0;
    double target_height_;

    int robot_id_ = 1;
    double marker_size_ = 0.1;
    std::vector<cv::Point2d> marker_obj_points_;  // marker frame, tl, tr, br, bl

    bool plane_lm_enable_ = true;
    int plane_lm_max_iter_ = 15;
    std::string plane_lm_pose_topic_;
    rclcpp::Publisher<geometry_msgs::msg::PoseStamped>::SharedPtr plane_lm_pose_pub_;
    aruco_test::PlaneLmCamera lm_cam_;  // K, D, world -> camera pose for plane LM

    bool is_debug_mode_ = false;
    bool image_debug_ = false;
    rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr debug_img_pub_;

    bool is_camera_info_received_ = false;
    bool is_camera_position_initialized_ = false;
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
    auto node = std::make_shared<HomographyDuckNode>();
    rclcpp::spin(node);
    rclcpp::shutdown();
    return 0;
}
