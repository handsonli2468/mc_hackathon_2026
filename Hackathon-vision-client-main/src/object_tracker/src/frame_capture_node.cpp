#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/image.hpp>
#include <sensor_msgs/image_encodings.hpp>
#include <cv_bridge/cv_bridge.h>
#include <opencv2/opencv.hpp>
#include <chrono>
#include <ctime>
#include <filesystem>
#include <iomanip>
#include <mutex>
#include <sstream>

// Keys: [space] save full frame, [r] select ROI and save crop, [q]/[esc] quit
class FrameCaptureNode : public rclcpp::Node {
public:
    FrameCaptureNode() : Node("frame_capture_node") {
        this->declare_parameter<std::string>("color_topic", "/camera_duck/camera/color/image_rect_raw");
        this->declare_parameter<std::string>("save_dir", "captures");
        this->declare_parameter<std::string>("prefix", "frame");
        color_topic_ = this->get_parameter("color_topic").as_string();
        save_dir_ = this->get_parameter("save_dir").as_string();
        prefix_ = this->get_parameter("prefix").as_string();

        std::filesystem::create_directories(save_dir_);

        color_sub_ = this->create_subscription<sensor_msgs::msg::Image>(
            color_topic_, rclcpp::SensorDataQoS(),
            std::bind(&FrameCaptureNode::color_callback, this, std::placeholders::_1));

        RCLCPP_INFO(this->get_logger(), "Subscribing %s, saving to %s",
                    color_topic_.c_str(), std::filesystem::absolute(save_dir_).c_str());
        RCLCPP_INFO(this->get_logger(), "[space] save frame  [r] select ROI & save crop  [q/esc] quit");
    }

    // Runs in the main thread so that HighGUI calls stay on one thread
    void run_gui() {
        const std::string window = "Frame Capture";
        cv::namedWindow(window, cv::WINDOW_AUTOSIZE);

        while (rclcpp::ok()) {
            cv::Mat frame;
            {
                std::lock_guard<std::mutex> lock(frame_mutex_);
                if (!latest_frame_.empty()) {
                    frame = latest_frame_.clone();
                }
            }

            if (frame.empty()) {
                cv::Mat waiting(240, 480, CV_8UC3, cv::Scalar(40, 40, 40));
                cv::putText(waiting, "Waiting for " + color_topic_, cv::Point(10, 120),
                            cv::FONT_HERSHEY_SIMPLEX, 0.45, cv::Scalar(255, 255, 255), 1);
                cv::imshow(window, waiting);
            } else {
                cv::Mat vis = frame.clone();
                cv::putText(vis, cv::format("[space] save  [r] crop  [q] quit   saved: %d", saved_count_),
                            cv::Point(10, 25), cv::FONT_HERSHEY_SIMPLEX, 0.6, cv::Scalar(0, 255, 0), 2);
                cv::imshow(window, vis);
            }

            const int key = cv::waitKey(15) & 0xFF;
            if (key == 'q' || key == 27) {
                break;
            }
            if (frame.empty()) {
                continue;
            }
            if (key == ' ') {
                save_image(frame, prefix_);
            } else if (key == 'r') {
                // frame is frozen while selecting; ENTER/SPACE confirm, C/ESC cancel
                const cv::Rect roi = cv::selectROI(window, frame, true, false);
                if (roi.area() > 0) {
                    save_image(frame(roi), "target");
                } else {
                    RCLCPP_INFO(this->get_logger(), "ROI selection cancelled");
                }
            }
        }
        cv::destroyAllWindows();
    }

private:
    void color_callback(const sensor_msgs::msg::Image::SharedPtr msg) {
        try {
            auto cv_ptr = cv_bridge::toCvCopy(msg, sensor_msgs::image_encodings::BGR8);
            std::lock_guard<std::mutex> lock(frame_mutex_);
            latest_frame_ = cv_ptr->image;
        } catch (cv_bridge::Exception &e) {
            RCLCPP_ERROR(this->get_logger(), "cv_bridge exception: %s", e.what());
        }
    }

    void save_image(const cv::Mat &img, const std::string &prefix) {
        const auto now = std::chrono::system_clock::now();
        const auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(now.time_since_epoch()).count() % 1000;
        const std::time_t t = std::chrono::system_clock::to_time_t(now);
        std::ostringstream name;
        name << prefix << "_" << std::put_time(std::localtime(&t), "%Y%m%d_%H%M%S")
             << "_" << std::setw(3) << std::setfill('0') << ms << ".png";

        const std::string path = (std::filesystem::path(save_dir_) / name.str()).string();
        if (cv::imwrite(path, img)) {
            ++saved_count_;
            RCLCPP_INFO(this->get_logger(), "Saved %s (%dx%d)", path.c_str(), img.cols, img.rows);
        } else {
            RCLCPP_ERROR(this->get_logger(), "Failed to write %s", path.c_str());
        }
    }

    rclcpp::Subscription<sensor_msgs::msg::Image>::SharedPtr color_sub_;
    std::string color_topic_;
    std::string save_dir_;
    std::string prefix_;

    std::mutex frame_mutex_;
    cv::Mat latest_frame_;
    int saved_count_ = 0;
};

int main(int argc, char **argv) {
    rclcpp::init(argc, argv);
    auto node = std::make_shared<FrameCaptureNode>();

    rclcpp::executors::SingleThreadedExecutor executor;
    executor.add_node(node);
    std::thread spin_thread([&executor]() { executor.spin(); });

    node->run_gui();

    executor.cancel();
    rclcpp::shutdown();
    spin_thread.join();
    return 0;
}
