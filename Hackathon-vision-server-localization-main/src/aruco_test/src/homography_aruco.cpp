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

class HomographyArucoNode : public rclcpp::Node {
public:
    HomographyArucoNode() : Node("homography_aruco_node") {
        this->declare_parameter<std::string>("RGB_topic", "/camera/camera/color/image_raw");
        this->declare_parameter<double>("target_height", 0.447);
        this->declare_parameter<double>("marker_size", 0.1);
        this->declare_parameter<int>("robot.id", 2);
        this->declare_parameter<int>("rival.id", 6);
        this->declare_parameter<bool>("rival.enable", false);
        this->declare_parameter<std::string>("world_frame", "map");
        this->declare_parameter<std::string>("camera_frame", "camera_color_optical_frame");
        this->declare_parameter<bool>("pose_filter.enable", false);
        this->declare_parameter<double>("pose_filter.alpha", 0.1);
        this->declare_parameter<double>("pose_filter.max_jump_m", 0.15);
        this->declare_parameter<bool>("debug.enable", false);
        this->declare_parameter<bool>("debug.img", false);
        this->declare_parameter<std::string>("debug.img_topic", "~/debug/image");
        RGB_topic_ = this->get_parameter("RGB_topic").as_string();
        target_height_ = this->get_parameter("target_height").as_double();
        marker_size_ = this->get_parameter("marker_size").as_double();
        robot_id_ = this->get_parameter("robot.id").as_int();
        rival_id_ = this->get_parameter("rival.id").as_int();
        rival_enable_ = this->get_parameter("rival.enable").as_bool();
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

        tf_buffer_ = std::make_unique<tf2_ros::Buffer>(this->get_clock());
        tf_listener_ = std::make_shared<tf2_ros::TransformListener>(*tf_buffer_);
        tf_broadcaster_ = std::make_unique<tf2_ros::TransformBroadcaster>(*this);

        RGB_subscriber_ = this->create_subscription<sensor_msgs::msg::Image>(
            RGB_topic_, 10, 
            std::bind(&HomographyArucoNode::RGB_img_callback, this, std::placeholders::_1));

        pose_pub_ = this->create_publisher<geometry_msgs::msg::PoseStamped>("/cb_camera_pose", 10);

        dictionary_ = cv::aruco::getPredefinedDictionary(cv::aruco::DICT_4X4_100);
        detector_params_ = cv::aruco::DetectorParameters::create();
        // detector_params_->cornerRefinementMethod = cv::aruco::CORNER_REFINE_SUBPIX;
        detector_params_->polygonalApproxAccuracyRate = 0.05; 
        detector_params_->adaptiveThreshWinSizeMin = 3;
        detector_params_->adaptiveThreshWinSizeMax = 23;
        detector_params_->adaptiveThreshWinSizeStep = 10;
        
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
    void pose_filter(const double raw_pose[3], double filtered_pose[3]) {
        filtered_pose[0] = raw_pose[0];
        filtered_pose[1] = raw_pose[1];
        filtered_pose[2] = raw_pose[2];

        if (!pose_filter_enable_) {
            pose_filter_initialized_ = false;
            return;
        }

        // keep filter tunable without restart (alpha/max_jump can be updated at runtime)
        pose_filter_alpha_ = this->get_parameter("pose_filter.alpha").as_double();
        pose_filter_max_jump_m_ = this->get_parameter("pose_filter.max_jump_m").as_double();
        const double alpha = std::clamp(pose_filter_alpha_, 0.0, 1.0);

        if (!pose_filter_initialized_) {
            pose_filtered_[0] = raw_pose[0];
            pose_filtered_[1] = raw_pose[1];
            pose_filtered_[2] = raw_pose[2];
            pose_filter_initialized_ = true;
        } else {
            const double dx = raw_pose[0] - pose_filtered_[0];
            const double dy = raw_pose[1] - pose_filtered_[1];
            const double dz = raw_pose[2] - pose_filtered_[2];
            const double dist = std::sqrt(dx * dx + dy * dy + dz * dz);

            if (pose_filter_max_jump_m_ > 0.0 && dist > pose_filter_max_jump_m_) {
                // outlier guard: snap to measurement on big jumps
                pose_filtered_[0] = raw_pose[0];
                pose_filtered_[1] = raw_pose[1];
                pose_filtered_[2] = raw_pose[2];
            } else {
                pose_filtered_[0] = alpha * raw_pose[0] + (1.0 - alpha) * pose_filtered_[0];
                pose_filtered_[1] = alpha * raw_pose[1] + (1.0 - alpha) * pose_filtered_[1];
                pose_filtered_[2] = alpha * raw_pose[2] + (1.0 - alpha) * pose_filtered_[2];
            }
        }

        filtered_pose[0] = pose_filtered_[0];
        filtered_pose[1] = pose_filtered_[1];
        filtered_pose[2] = pose_filtered_[2];
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

            std::vector<int> marker_ids;
            std::vector<std::vector<cv::Point2f>> marker_corners, rejected_candidates;
            std::vector<cv::Point2f> target_corners(4);
            cv::aruco::detectMarkers(RGB_frame, dictionary_, marker_corners, marker_ids, detector_params_, rejected_candidates);

            // target pixel coordinates
            double target_u = 0.0;
            double target_v = 0.0;
            bool is_target_found = false;
            // reset fixed-marker detection flags for this frame
            for (auto &kv : marker_found_) {
                kv.second = false;
            }
            
            if (!marker_ids.empty()) {
                std::vector<cv::Point2f> pts_img_2d;
                std::vector<cv::Point2f> pts_world_2d;
                
                // collect known ArUco corner points
                for (size_t i = 0; i < marker_ids.size(); i++) {
                    int id = marker_ids[i];

                    if (marker_world_2d_.count(id)) {
                        for (int j = 0; j < 4; j++) {
                            pts_world_2d.push_back(marker_world_2d_[id][j]);
                            pts_img_2d.push_back(marker_corners[i][j]);
                            last_marker_corners_[id][j] = marker_corners[i][j];
                        }
                        marker_found_[id] = true;
                    }
                    else if(id == robot_id_) {
                        target_u = (marker_corners[i][0].x + marker_corners[i][2].x) / 2.0;
                        target_v = (marker_corners[i][0].y + marker_corners[i][2].y) / 2.0;

                        for (int j=0;j<4;j++){
                            target_corners[j] = marker_corners[i][j];
                        }

                        is_target_found = true;
                    }
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
                double raw_pose[3] = {0.0, 0.0, 0.0};
                double final_pose[3] = {0.0, 0.0, 0.0};
                double yaw_rad = 0.0;
                double yaw_deg = 0.0;
                if (pts_img_2d.size() >= 4 && is_target_found) {
                    cv::Mat H = cv::findHomography(pts_img_2d, pts_world_2d, cv::RANSAC);
                    
                    if (!H.empty()) {
                        // find 3D position of the robot
                        // project target pixel to Z=0 ground to get shadow (H * [u, v, 1])
                        cv::Mat pt_src = (cv::Mat_<double>(3, 1) << target_u, target_v, 1.0);
                        cv::Mat pt_dst = H * pt_src;
                        
                        double X_g = pt_dst.at<double>(0, 0) / pt_dst.at<double>(2, 0);
                        double Y_g = pt_dst.at<double>(1, 0) / pt_dst.at<double>(2, 0);

                        // use 3D similar triangle linear interpolation to get Z=target_height_ coordinates
                        double t = (cam_z_ - target_height_) / cam_z_;
                        double target_x = cam_x_ + t * (X_g - cam_x_);
                        double target_y = cam_y_ + t * (Y_g - cam_y_);

                        raw_pose[0] = target_x;
                        raw_pose[1] = target_y;
                        raw_pose[2] = target_height_;
                        pose_filter(raw_pose, final_pose);

                        // find orientation of the robot
                        // ==========================================
                        // 計算目標物的場地朝向角 (Yaw)
                        // ==========================================
                        // 1. 取得目標 ArUco 的 4 個像素角點 (假設在迴圈中是 marker_corners[i])
                        cv::Point2f tl = target_corners[0]; // Top-Left
                        cv::Point2f tr = target_corners[1]; // Top-Right
                        cv::Point2f br = target_corners[2]; // Bottom-Right
                        cv::Point2f bl = target_corners[3]; // Bottom-Left

                        // 2. 計算底部中心與頂部中心 (像素坐標)
                        cv::Point2f left_center = (bl + tl) / 2.0;
                        cv::Point2f right_center = (br + tr) / 2.0;

                        // 3. 透過 H 矩陣投影到底部平面 (求影子座標)
                        cv::Mat pt_bottom_src = (cv::Mat_<double>(3, 1) << left_center.x, left_center.y, 1.0);
                        cv::Mat pt_bottom_dst = H * pt_bottom_src;
                        double X_bottom = pt_bottom_dst.at<double>(0, 0) / pt_bottom_dst.at<double>(2, 0);
                        double Y_bottom = pt_bottom_dst.at<double>(1, 0) / pt_bottom_dst.at<double>(2, 0);

                        cv::Mat pt_top_src = (cv::Mat_<double>(3, 1) << right_center.x, right_center.y, 1.0);
                        cv::Mat pt_top_dst = H * pt_top_src;
                        double X_top = pt_top_dst.at<double>(0, 0) / pt_top_dst.at<double>(2, 0);
                        double Y_top = pt_top_dst.at<double>(1, 0) / pt_top_dst.at<double>(2, 0);

                        // 4. 利用這兩個點算出方向向量與角度
                        double dx = X_top - X_bottom;
                        double dy = Y_top - Y_bottom;

                        // atan2 會回傳弧度 (-π 到 π)，這裡順便轉成度數方便 Debug 觀看
                        yaw_rad = std::atan2(dy, dx);
                        yaw_deg = yaw_rad * 180.0 / CV_PI;

                        // publish filtered pose + yaw to /cb_camera_pose
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
                    }
                }
                
                // ==========================================
                // Debug : show corner points
                // ==========================================
                if (is_debug_mode_) {
                    if (image_debug_){
                        // draw real detected markers (from this frame)
                        cv::aruco::drawDetectedMarkers(RGB_frame, marker_corners, marker_ids);

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
                        cv::aruco::drawDetectedMarkers(RGB_frame, rejected_candidates, cv::noArray(), cv::Scalar(255, 0, 255));

                        // draw a red point on the target center (pixel)
                        cv::circle(RGB_frame, cv::Point(target_u, target_v), 5, cv::Scalar(0, 0, 255), -1);

                        publish_debug_image(RGB_frame, msg->header);
                    }

                    if(is_target_found){
                        RCLCPP_INFO(this->get_logger(),
                            "Target 3D raw:(%.3f, %.3f, %.3f) filtered:(%.3f, %.3f, %.3f), Yaw: %.3f rad %.3f deg",
                            raw_pose[0], raw_pose[1], raw_pose[2],
                            final_pose[0], final_pose[1], final_pose[2],
                            yaw_rad, yaw_deg
                        );
                        // brodacast TF for dubugging
                        geometry_msgs::msg::TransformStamped t;

                        t.header.stamp = msg->header.stamp;
                        t.header.frame_id = world_frame_;
                        t.child_frame_id = "detected_robot_" + std::to_string(robot_id_);

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
                }
                // ==========================================
            }
        } catch (cv_bridge::Exception& e) {
            RCLCPP_ERROR(this->get_logger(), "cv_bridge exception: %s", e.what());
        }
    }

    rclcpp::Subscription<sensor_msgs::msg::Image>::SharedPtr RGB_subscriber_;
    rclcpp::Publisher<geometry_msgs::msg::PoseStamped>::SharedPtr pose_pub_;
    std::string RGB_topic_;
    std::map<int, std::vector<cv::Point2f>> marker_world_2d_;
    std::unique_ptr<tf2_ros::Buffer> tf_buffer_;
    std::shared_ptr<tf2_ros::TransformListener> tf_listener_;
    std::unique_ptr<tf2_ros::TransformBroadcaster> tf_broadcaster_;
    std::string world_frame_;
    std::string camera_frame_;
    
    double cam_x_, cam_y_, cam_z_;
    double target_height_;
    double marker_size_ = 0.1;

    int robot_id_ = 2;
    int rival_id_ = 6;
    bool rival_enable_ = false;

    bool is_debug_mode_ = false;
    bool image_debug_ = false;
    rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr debug_img_pub_;

    bool is_camera_position_initialized_ = false;
    bool pose_filter_enable_ = true;
    bool pose_filter_initialized_ = false;
    double pose_filter_alpha_ = 0.2;
    double pose_filter_max_jump_m_ = 0.5;
    double pose_filtered_[3] = {0.0, 0.0, 0.0};

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

    cv::Ptr<cv::aruco::Dictionary> dictionary_;
    cv::Ptr<cv::aruco::DetectorParameters> detector_params_;
};

int main(int argc, char **argv) {
    rclcpp::init(argc, argv);
    auto node = std::make_shared<HomographyArucoNode>();
    rclcpp::spin(node);
    rclcpp::shutdown(); 
    return 0;
}