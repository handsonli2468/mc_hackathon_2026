#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/image.hpp>
#include <cv_bridge/cv_bridge.h>
#include <opencv2/opencv.hpp>
#include <opencv2/aruco.hpp>
#include <tf2_ros/transform_broadcaster.h>
#include <tf2_ros/transform_listener.h>
#include <tf2_ros/buffer.h>
#include <tf2_geometry_msgs/tf2_geometry_msgs.hpp>
#include <tf2_eigen/tf2_eigen.hpp>
#include <geometry_msgs/msg/transform_stamped.hpp>
#include <geometry_msgs/msg/pose_with_covariance_stamped.hpp>
#include <geometry_msgs/msg/pose_stamped.hpp>
#include <geometry_msgs/msg/pose_with_covariance.hpp>
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <unordered_map>
#include <unordered_set>
#include <vector>

class HomographyArucoNode : public rclcpp::Node {
public:
    HomographyArucoNode() : Node("homography_sima_node") {
        this->declare_parameter<std::string>("RGB_topic", "/camera/camera/color/image_raw");
        this->declare_parameter<double>("marker_size", 0.1);
        // Normal sima param
        this->declare_parameter<std::vector<int64_t>>("sima.ids", std::vector<int64_t>{});
        this->declare_parameter<double>("sima.offset.x", 0.029);
        this->declare_parameter<double>("sima.height", 0.15);
        // Ninja sima param
        this->declare_parameter<int>("ninja.ids", 10);
        this->declare_parameter<double>("ninja.offset.x", -0.003);
        this->declare_parameter<double>("ninja.offset.y", -0.02);
        this->declare_parameter<double>("ninja.height", 0.205);
        
        this->declare_parameter<std::string>("world_frame", "map");
        this->declare_parameter<std::string>("camera_frame", "camera_color_optical_frame");
        this->declare_parameter<bool>("pose_filter.enable", true);
        this->declare_parameter<double>("pose_filter.alpha", 0.1);
        this->declare_parameter<double>("pose_filter.max_jump_m", 0.15);
        this->declare_parameter<bool>("debug.enable", false);
        this->declare_parameter<bool>("debug.img", false);
        this->declare_parameter<std::string>("debug.img_topic", "~/debug/image");
        
        RGB_topic_ = this->get_parameter("RGB_topic").as_string();
        marker_size_ = this->get_parameter("marker_size").as_double();
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
        sima_offset_x_ = this->get_parameter("sima.offset.x").as_double();
        sima_height_ = this->get_parameter("sima.height").as_double();
        ninja_id_ = this->get_parameter("ninja.ids").as_int();
        ninja_offset_x_ = this->get_parameter("ninja.offset.x").as_double();
        ninja_offset_y_ = this->get_parameter("ninja.offset.y").as_double();
        ninja_height_ = this->get_parameter("ninja.height").as_double();

        const auto sima_ids_param = this->get_parameter("sima.ids").as_integer_array();
        for (const auto &v : sima_ids_param) {
            sima_ids_.insert(static_cast<int>(v));
        }
        sima_ids_.insert(ninja_id_);  // ensure ninja ID is included

        tf_buffer_ = std::make_unique<tf2_ros::Buffer>(this->get_clock());
        tf_listener_ = std::make_shared<tf2_ros::TransformListener>(*tf_buffer_);
        tf_broadcaster_ = std::make_unique<tf2_ros::TransformBroadcaster>(*this);

        RGB_subscriber_ = this->create_subscription<sensor_msgs::msg::Image>(
            RGB_topic_, 10, 
            std::bind(&HomographyArucoNode::RGB_img_callback, this, std::placeholders::_1));

        // Per-sima publisher
        for (const int id : sima_ids_) {
            sima_pose_pubs_.emplace(
                id,
                this->create_publisher<geometry_msgs::msg::PoseWithCovarianceStamped>("/sima_" + std::to_string(id) + "/pose/global", 10));
            pose_filter_state_.emplace(id, PoseFilterState{});
        }

        // Field markers (for homography) and SIMA markers (AprilTag on robot)
        dictionary_field_ = cv::aruco::getPredefinedDictionary(cv::aruco::DICT_4X4_100);
        dictionary_sima_ = cv::aruco::getPredefinedDictionary(cv::aruco::DICT_APRILTAG_16h5);
        detector_params_ = cv::aruco::DetectorParameters::create();
        // detector_params_->cornerRefinementMethod = cv::aruco::CORNER_REFINE_SUBPIX;
        detector_params_->polygonalApproxAccuracyRate = 0.05;
        detector_params_->adaptiveThreshWinSizeMin = 3;
        detector_params_->adaptiveThreshWinSizeMax = 23;
        detector_params_->adaptiveThreshWinSizeStep = 10;

        detector_params_->perspectiveRemoveIgnoredMarginPerCell = 0.25;
        detector_params_->minMarkerDistanceRate = 0.02;
        
        // ArUco 2D real world coordinates in world coordinate system
        // top-left, top-right, bottom-right, bottom-left
        marker_world_2d_ = {
            {20, {cv::Point2f(0.6f - marker_size_/2, 1.4f + marker_size_/2), cv::Point2f(0.6f + marker_size_/2, 1.4f + marker_size_/2), cv::Point2f(0.6f + marker_size_/2, 1.4f - marker_size_/2), cv::Point2f(0.6f - marker_size_/2, 1.4f - marker_size_/2)}},
            {21, {cv::Point2f(2.4f - marker_size_/2, 1.4f + marker_size_/2), cv::Point2f(2.4f + marker_size_/2, 1.4f + marker_size_/2), cv::Point2f(2.4f + marker_size_/2, 1.4f - marker_size_/2), cv::Point2f(2.4f - marker_size_/2, 1.4f - marker_size_/2)}},
            {22, {cv::Point2f(0.6f - marker_size_/2, 0.6f + marker_size_/2), cv::Point2f(0.6f + marker_size_/2, 0.6f + marker_size_/2), cv::Point2f(0.6f + marker_size_/2, 0.6f - marker_size_/2), cv::Point2f(0.6f - marker_size_/2, 0.6f - marker_size_/2)}},
            {23, {cv::Point2f(2.4f - marker_size_/2, 0.6f + marker_size_/2), cv::Point2f(2.4f + marker_size_/2, 0.6f + marker_size_/2), cv::Point2f(2.4f + marker_size_/2, 0.6f - marker_size_/2), cv::Point2f(2.4f - marker_size_/2, 0.6f - marker_size_/2)}}
        };
    }

private:
    struct PoseFilterState {
        bool initialized{false};
        double filtered[3]{0.0, 0.0, 0.0};
    };

    void pose_filter_for_id(const int id, const double raw_pose[3], double filtered_pose[3]) {
        filtered_pose[0] = raw_pose[0];
        filtered_pose[1] = raw_pose[1];
        filtered_pose[2] = raw_pose[2];

        if (!pose_filter_enable_) {
            auto it = pose_filter_state_.find(id);
            if (it != pose_filter_state_.end()) {
                it->second.initialized = false;
            }
            return;
        }

        // keep filter tunable without restart (alpha/max_jump can be updated at runtime)
        pose_filter_alpha_ = this->get_parameter("pose_filter.alpha").as_double();
        pose_filter_max_jump_m_ = this->get_parameter("pose_filter.max_jump_m").as_double();
        const double alpha = std::clamp(pose_filter_alpha_, 0.0, 1.0);

        auto &st = pose_filter_state_[id];  // creates default if missing
        if (!st.initialized) {
            st.filtered[0] = raw_pose[0];
            st.filtered[1] = raw_pose[1];
            st.filtered[2] = raw_pose[2];
            st.initialized = true;
        } else {
            const double dx = raw_pose[0] - st.filtered[0];
            const double dy = raw_pose[1] - st.filtered[1];
            const double dz = raw_pose[2] - st.filtered[2];
            const double dist = std::sqrt(dx * dx + dy * dy + dz * dz);

            if (pose_filter_max_jump_m_ > 0.0 && dist > pose_filter_max_jump_m_) {
                // outlier guard: snap to measurement on big jumps
                st.filtered[0] = raw_pose[0];
                st.filtered[1] = raw_pose[1];
                st.filtered[2] = raw_pose[2];
            } else {
                st.filtered[0] = alpha * raw_pose[0] + (1.0 - alpha) * st.filtered[0];
                st.filtered[1] = alpha * raw_pose[1] + (1.0 - alpha) * st.filtered[1];
                st.filtered[2] = alpha * raw_pose[2] + (1.0 - alpha) * st.filtered[2];
            }
        }

        filtered_pose[0] = st.filtered[0];
        filtered_pose[1] = st.filtered[1];
        filtered_pose[2] = st.filtered[2];
    }

    void get_camera_position() {
        try{
            if (!tf_buffer_) {
                RCLCPP_ERROR(this->get_logger(), "TF buffer not initialized");
                return;
            }
            auto tf_msg = tf_buffer_->lookupTransform(world_frame_, camera_frame_, tf2::TimePointZero);
            cam_x_ = tf_msg.transform.translation.x;
            cam_y_ = tf_msg.transform.translation.y;
            cam_z_ = tf_msg.transform.translation.z;
            is_camera_position_initialized_ = true;
            RCLCPP_INFO(this->get_logger(), "Camera position: X=%.3f, Y=%.3f, Z=%.3f", cam_x_, cam_y_, cam_z_);
            RCLCPP_INFO(this->get_logger(), "Camera position initialized");
            return;
        }
        catch (tf2::TransformException &ex) {
            RCLCPP_ERROR(this->get_logger(), "Transform exception: %s", ex.what());
            return;
        }
    }
    void publish_debug_image(const cv::Mat &bgr, const std_msgs::msg::Header &header) {
        if (debug_img_pub_) {
            debug_img_pub_->publish(*cv_bridge::CvImage(header, "bgr8", bgr).toImageMsg());
        }
    }

    void RGB_img_callback(const sensor_msgs::msg::Image::SharedPtr msg) {
        try {

            if (!is_camera_position_initialized_) {
                RCLCPP_INFO(this->get_logger(), "Waiting for camera position...");
                get_camera_position();
                // cam_x_ = 1.6759;
                // cam_y_ = 2.0753;
                // cam_z_ = 1.4602;
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

            // Detect field markers (Aruco 4x4) for homography
            std::vector<int> field_marker_ids;
            std::vector<std::vector<cv::Point2f>> field_marker_corners, field_rejected_candidates;
            cv::aruco::detectMarkers(
                RGB_frame, dictionary_field_, field_marker_corners, field_marker_ids, detector_params_, field_rejected_candidates);

            // Detect SIMA markers (AprilTag) for publishing poses
            std::vector<int> sima_marker_ids;
            std::vector<std::vector<cv::Point2f>> sima_marker_corners, sima_rejected_candidates;
            cv::aruco::detectMarkers(
                RGB_frame, dictionary_sima_, sima_marker_corners, sima_marker_ids, detector_params_, sima_rejected_candidates);

            // reset fixed-marker detection flags for this frame
            for (auto &kv : marker_found_) {
                kv.second = false;
            }
            
            if (!field_marker_ids.empty() || !sima_marker_ids.empty()) {
                std::vector<cv::Point2f> pts_img_2d;
                std::vector<cv::Point2f> pts_world_2d;

                // Cache detected corners by ID for this frame (for target markers)
                std::unordered_map<int, std::vector<cv::Point2f>> detected_corners_by_id;

                // collect known field ArUco corner points (for homography) using 4x4 dictionary detections
                for (size_t i = 0; i < field_marker_ids.size(); i++) {
                    int id = field_marker_ids[i];
                    if (marker_world_2d_.count(id)) {
                        for (int j = 0; j < 4; j++) {
                            pts_world_2d.push_back(marker_world_2d_[id][j]);
                            pts_img_2d.push_back(field_marker_corners[i][j]);
                            last_marker_corners_[id][j] = field_marker_corners[i][j];
                        }
                        marker_found_[id] = true;
                    }
                }

                // cache sima marker corners using AprilTag dictionary detections
                for (size_t i = 0; i < sima_marker_ids.size(); i++) {
                    const int id = sima_marker_ids[i];
                    detected_corners_by_id[id] = sima_marker_corners[i];
                }

                // If no target markers from sima_ids_ are detected this frame, we can skip the rest.
                bool any_sima_found = false;
                for (const int id : sima_ids_) {
                    if (detected_corners_by_id.find(id) != detected_corners_by_id.end()) {
                        any_sima_found = true;
                        break;
                    }
                }
                if (!any_sima_found) {
                    return;
                }

                for (const auto &entry : marker_world_2d_) {
                    int id = entry.first;
                    if (!marker_found_[id]) {
                        for (int j = 0; j < 4; j++) {
                            pts_world_2d.push_back(marker_world_2d_[id][j]);
                            pts_img_2d.push_back(last_marker_corners_[id][j]);
                        }
                    }
                }

                // calculate homography matrix if at least 4 points are found
                if (pts_img_2d.size() >= 4) {
                    cv::Mat H = cv::findHomography(pts_img_2d, pts_world_2d, cv::RANSAC);
                    
                    if (!H.empty()) {
                        // publish pose for each detected sima id
                        for (const int sima_id : sima_ids_) {
                            auto it_corners = detected_corners_by_id.find(sima_id);
                            if (it_corners == detected_corners_by_id.end()) {
                                continue;
                            }

                            const auto &corners = it_corners->second;
                            if (corners.size() != 4) {
                                continue;
                            }

                            // center pixel coordinates
                            const double target_u = (corners[0].x + corners[2].x) / 2.0;
                            const double target_v = (corners[0].y + corners[2].y) / 2.0;

                            // project target pixel to Z=0 ground to get shadow (H * [u, v, 1])
                            cv::Mat pt_src = (cv::Mat_<double>(3, 1) << target_u, target_v, 1.0);
                            cv::Mat pt_dst = H * pt_src;

                            const double X_g = pt_dst.at<double>(0, 0) / pt_dst.at<double>(2, 0);
                            const double Y_g = pt_dst.at<double>(1, 0) / pt_dst.at<double>(2, 0);

                            // use 3D similar triangle linear interpolation to get Z=target_height_ coordinates
                            target_height_ = (sima_id == ninja_id_) ? ninja_height_ : sima_height_;
                            const double t = (cam_z_ - target_height_) / cam_z_;
                            const double target_x = cam_x_ + t * (X_g - cam_x_);
                            const double target_y = cam_y_ + t * (Y_g - cam_y_);

                            double raw_pose[3] = {target_x, target_y, target_height_};
                            double final_pose[3] = {0.0, 0.0, 0.0};
                            pose_filter_for_id(sima_id, raw_pose, final_pose);

                            // estimate yaw using left and right midpoints projected to world
                            const cv::Point2f tl = corners[0];
                            const cv::Point2f tr = corners[1];
                            const cv::Point2f br = corners[2];
                            const cv::Point2f bl = corners[3];

                            const cv::Point2f left_center = (bl + tl) * 0.5f;
                            const cv::Point2f right_center = (br + tr) * 0.5f;

                            cv::Mat pt_left_src = (cv::Mat_<double>(3, 1) << left_center.x, left_center.y, 1.0);
                            cv::Mat pt_left_dst = H * pt_left_src;
                            const double X_left = pt_left_dst.at<double>(0, 0) / pt_left_dst.at<double>(2, 0);
                            const double Y_left = pt_left_dst.at<double>(1, 0) / pt_left_dst.at<double>(2, 0);

                            cv::Mat pt_right_src = (cv::Mat_<double>(3, 1) << right_center.x, right_center.y, 1.0);
                            cv::Mat pt_right_dst = H * pt_right_src;
                            const double X_right = pt_right_dst.at<double>(0, 0) / pt_right_dst.at<double>(2, 0);
                            const double Y_right = pt_right_dst.at<double>(1, 0) / pt_right_dst.at<double>(2, 0);

                            const double dx = X_right - X_left;
                            const double dy = Y_right - Y_left;
                            // for Eurobot2026 sima, there is an offset between aruco and sima base link
                            const double yaw_rad = std::atan2(dy, dx) + sima_offset_yaw;
                            const double yaw_deg = yaw_rad * 180.0 / CV_PI;

                            // Apply offset along marker heading (forward direction)
                            const double cos_yaw = std::cos(yaw_rad);
                            const double sin_yaw = std::sin(yaw_rad);
                            if (sima_id == ninja_id_) {
                                final_pose[0] += cos_yaw * ninja_offset_x_ - sin_yaw * ninja_offset_y_;
                                final_pose[1] += sin_yaw * ninja_offset_x_ + cos_yaw * ninja_offset_y_;
                            } else {
                                final_pose[0] += cos_yaw * sima_offset_x_;
                                final_pose[1] += sin_yaw * sima_offset_x_;
                            }

                            geometry_msgs::msg::PoseWithCovarianceStamped pose_msg;
                            pose_msg.header.stamp = msg->header.stamp;
                            pose_msg.header.frame_id = world_frame_;
                            pose_msg.pose.pose.position.x = final_pose[0];
                            pose_msg.pose.pose.position.y = final_pose[1];
                            pose_msg.pose.pose.position.z = final_pose[2];

                            const double half_yaw = yaw_rad * 0.5;
                            pose_msg.pose.pose.orientation.x = 0.0;
                            pose_msg.pose.pose.orientation.y = 0.0;
                            pose_msg.pose.pose.orientation.z = std::sin(half_yaw);
                            pose_msg.pose.pose.orientation.w = std::cos(half_yaw);
                            
                            pose_msg.pose.covariance[0] = 0.05 * 0.05;   // x variance
                            pose_msg.pose.covariance[7] = 0.05 * 0.05;   // y variance
                            pose_msg.pose.covariance[14] = 1e-6;         // z variance (small but non-zero for 2D)
                            pose_msg.pose.covariance[21] = 1e-6;         // roll variance (small but non-zero for 2D)
                            pose_msg.pose.covariance[28] = 1e-6;         // pitch variance (small but non-zero for 2D)
                            pose_msg.pose.covariance[35] = 0.05 * 0.05;  // yaw variance

                            auto it_pub = sima_pose_pubs_.find(sima_id);
                            if (it_pub != sima_pose_pubs_.end()) {
                                it_pub->second->publish(pose_msg);
                            }

                            if (is_debug_mode_) {
                                RCLCPP_INFO(
                                    this->get_logger(),
                                    "sima_id=%d raw:(%.3f, %.3f, %.3f) filtered:(%.3f, %.3f, %.3f) yaw: %.3f rad %.3f deg topic:/sima_%d",
                                    sima_id,
                                    raw_pose[0], raw_pose[1], raw_pose[2],
                                    final_pose[0], final_pose[1], final_pose[2],
                                    yaw_rad, yaw_deg,
                                    sima_id);

                                geometry_msgs::msg::TransformStamped t_tf;
                                t_tf.header.stamp = msg->header.stamp;
                                t_tf.header.frame_id = world_frame_;
                                t_tf.child_frame_id = "detected_sima_" + std::to_string(sima_id);
                                t_tf.transform.translation.x = final_pose[0];
                                t_tf.transform.translation.y = final_pose[1];
                                t_tf.transform.translation.z = final_pose[2];
                                t_tf.transform.rotation.x = 0.0;
                                t_tf.transform.rotation.y = 0.0;
                                t_tf.transform.rotation.z = std::sin(half_yaw);
                                t_tf.transform.rotation.w = std::cos(half_yaw);
                                tf_broadcaster_->sendTransform(t_tf);
                            }
                        }
                    }
                }
                
                // ==========================================
                // Debug : show corner points
                // ==========================================
                if (is_debug_mode_) {
                    if (image_debug_){
                        // draw real detected markers (from this frame)
                        if (!field_marker_ids.empty()) {
                            cv::aruco::drawDetectedMarkers(RGB_frame, field_marker_corners, field_marker_ids);
                        }
                        if (!sima_marker_ids.empty()) {
                            cv::aruco::drawDetectedMarkers(RGB_frame, sima_marker_corners, sima_marker_ids);
                        }

                        // draw "imagined" fixed markers (using last seen corners) in a different color
                        for (const auto &entry : marker_world_2d_) {
                            int id = entry.first;
                            if (marker_found_[id]) {
                                continue;  // this one is already drawn as real detection
                            }

                            const auto &corners = last_marker_corners_[id];
                            // skip if we never saw this marker (all corners at (0,0))
                            bool valid = false;
                            for (const auto &c : corners) {
                                if (c.x != 0.0f || c.y != 0.0f) {
                                    valid = true;
                                    break;
                                }
                            }
                            if (!valid || corners.size() != 4) {
                                continue;
                            }

                            // draw polygon for imagined marker (blue)
                            for (int j = 0; j < 4; ++j) {
                                const cv::Point2f &p1 = corners[j];
                                const cv::Point2f &p2 = corners[(j + 1) % 4];
                                cv::line(RGB_frame, p1, p2, cv::Scalar(255, 0, 0), 2);
                            }

                            // label at center
                            cv::Point2f center = (corners[0] + corners[2]) * 0.5f;
                            cv::putText(RGB_frame,
                                        "ID " + std::to_string(id) + " (ghost)",
                                        cv::Point(center.x - 30, center.y),
                                        cv::FONT_HERSHEY_SIMPLEX, 0.5,
                                        cv::Scalar(255, 0, 0), 2);
                        }

                        // draw rejected candidates (purple) for debugging
                        if (!field_rejected_candidates.empty()) {
                            cv::aruco::drawDetectedMarkers(
                                RGB_frame, field_rejected_candidates, cv::noArray(), cv::Scalar(255, 0, 255));
                        }
                        if (!sima_rejected_candidates.empty()) {
                            cv::aruco::drawDetectedMarkers(
                                RGB_frame, sima_rejected_candidates, cv::noArray(), cv::Scalar(255, 0, 255));
                        }

                        publish_debug_image(RGB_frame, msg->header);
                    }
                }
                // ==========================================
            }
        } catch (cv_bridge::Exception& e) {
            RCLCPP_ERROR(this->get_logger(), "cv_bridge exception: %s", e.what());
        }
    }

    rclcpp::Subscription<sensor_msgs::msg::Image>::SharedPtr RGB_subscriber_;
    std::unordered_map<int, rclcpp::Publisher<geometry_msgs::msg::PoseWithCovarianceStamped>::SharedPtr> sima_pose_pubs_;
    std::string RGB_topic_;
    std::map<int, std::vector<cv::Point2f>> marker_world_2d_;
    std::unique_ptr<tf2_ros::Buffer> tf_buffer_;
    std::shared_ptr<tf2_ros::TransformListener> tf_listener_;
    std::unique_ptr<tf2_ros::TransformBroadcaster> tf_broadcaster_;
    std::string world_frame_;
    std::string camera_frame_;
    
    double cam_x_, cam_y_, cam_z_;
    double target_height_ = 0.447;
    double marker_size_ = 0.1;
    double sima_offset_x_ = 0.029;
    double sima_offset_yaw = M_PI_2;
    double ninja_offset_x_ = 0.003;
    double ninja_offset_y_ = 0.02;
    double sima_height_ = 0.15;
    double ninja_height_ = 0.205;

    std::unordered_set<int> sima_ids_;
    int ninja_id_ = 10;

    bool is_debug_mode_ = false;
    bool image_debug_ = false;
    rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr debug_img_pub_;

    bool is_camera_position_initialized_ = false;
    bool pose_filter_enable_ = true;
    double pose_filter_alpha_ = 0.2;
    double pose_filter_max_jump_m_ = 0.5;
    std::unordered_map<int, PoseFilterState> pose_filter_state_;

    std::map<int, bool> marker_found_ = {
        {20, false},
        {21, false},
        {22, false},
        {23, false}
    };
    std::map<int, std::vector<cv::Point2f>> last_marker_corners_ = {
        {20, {cv::Point2f(0.0, 0.0), cv::Point2f(0.0, 0.0), cv::Point2f(0.0, 0.0), cv::Point2f(0.0, 0.0)}},
        {21, {cv::Point2f(0.0, 0.0), cv::Point2f(0.0, 0.0), cv::Point2f(0.0, 0.0), cv::Point2f(0.0, 0.0)}},
        {22, {cv::Point2f(0.0, 0.0), cv::Point2f(0.0, 0.0), cv::Point2f(0.0, 0.0), cv::Point2f(0.0, 0.0)}},
        {23, {cv::Point2f(0.0, 0.0), cv::Point2f(0.0, 0.0), cv::Point2f(0.0, 0.0), cv::Point2f(0.0, 0.0)}}
    };

    cv::Ptr<cv::aruco::Dictionary> dictionary_field_;
    cv::Ptr<cv::aruco::Dictionary> dictionary_sima_;
    cv::Ptr<cv::aruco::DetectorParameters> detector_params_;
};

int main(int argc, char **argv) {
    rclcpp::init(argc, argv);
    auto node = std::make_shared<HomographyArucoNode>();
    rclcpp::spin(node);
    rclcpp::shutdown(); 
    return 0;
}
