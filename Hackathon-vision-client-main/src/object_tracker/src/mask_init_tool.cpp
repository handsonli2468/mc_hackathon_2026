#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/image.hpp>
#include <sensor_msgs/image_encodings.hpp>
#include <cv_bridge/cv_bridge.h>
#include <opencv2/opencv.hpp>
#include <chrono>
#include <deque>
#include <mutex>
#include <optional>
#include <thread>

// Simulates the upstream VLM: select an object on a recent frame and publish its mask after delay_s,
// stamped with that frame's stamp.
// Keys: [s] freeze the latest frame and select a box (GrabCut inside it), [q]/[esc] quit
class MaskInitTool : public rclcpp::Node {
public:
    MaskInitTool() : Node("mask_init_tool") {
        this->declare_parameter<std::string>("color_topic", "/camera_duck/camera/color/image_rect_raw");
        this->declare_parameter<std::string>("mask_topic", "/tracked_object/init_mask");
        this->declare_parameter<double>("cache_s", 3.0);
        this->declare_parameter<double>("delay_s", 1.5);
        this->declare_parameter<bool>("use_grabcut", true);
        this->declare_parameter<int>("grabcut_iters", 3);
        color_topic_ = this->get_parameter("color_topic").as_string();
        const std::string mask_topic = this->get_parameter("mask_topic").as_string();
        cache_s_ = this->get_parameter("cache_s").as_double();
        delay_s_ = this->get_parameter("delay_s").as_double();
        use_grabcut_ = this->get_parameter("use_grabcut").as_bool();
        grabcut_iters_ = std::max(1, static_cast<int>(this->get_parameter("grabcut_iters").as_int()));

        color_sub_ = this->create_subscription<sensor_msgs::msg::Image>(
            color_topic_, rclcpp::SensorDataQoS(),
            std::bind(&MaskInitTool::color_callback, this, std::placeholders::_1));
        mask_pub_ = this->create_publisher<sensor_msgs::msg::Image>(mask_topic, rclcpp::QoS(rclcpp::KeepLast(5)).reliable());
        // node clock, so the delay follows the bag's clock when use_sim_time is set
        publish_timer_ = rclcpp::create_timer(this, this->get_clock(), rclcpp::Duration::from_seconds(0.02),
                                              std::bind(&MaskInitTool::publish_if_due, this));

        RCLCPP_INFO(this->get_logger(), "Subscribing %s, publishing masks on %s, delay %.2f s, grabcut %s",
                    color_topic_.c_str(), mask_topic.c_str(), delay_s_, use_grabcut_ ? "on" : "off");
        RCLCPP_INFO(this->get_logger(), "[s] select object  [q/esc] quit");
    }

    // Runs in the main thread so that HighGUI calls stay on one thread
    void run_gui() {
        const std::string window = "Mask Init Tool";
        cv::namedWindow(window, cv::WINDOW_AUTOSIZE);

        while (rclcpp::ok()) {
            std::optional<CachedColor> latest;
            std::optional<PublishedMask> overlay;
            bool waiting = false;
            {
                std::lock_guard<std::mutex> lock(mutex_);
                if (!frames_.empty()) {
                    latest = frames_.back();
                }
                overlay = last_published_;
                waiting = pending_.has_value();
            }

            if (!latest) {
                cv::Mat idle(240, 480, CV_8UC3, cv::Scalar(40, 40, 40));
                cv::putText(idle, "Waiting for " + color_topic_, cv::Point(10, 120),
                            cv::FONT_HERSHEY_SIMPLEX, 0.45, cv::Scalar(255, 255, 255), 1);
                cv::imshow(window, idle);
            } else {
                cv::Mat vis = latest->image.clone();
                // show the last published mask for 2 s
                if (overlay && std::chrono::steady_clock::now() - overlay->shown_at < std::chrono::seconds(2) &&
                    overlay->mask.size() == vis.size()) {
                    cv::Mat tinted = vis.clone();
                    tinted.setTo(cv::Scalar(255, 0, 255), overlay->mask);
                    cv::addWeighted(tinted, 0.4, vis, 0.6, 0.0, vis);
                }
                cv::putText(vis, cv::format("[s] select  [q] quit   delay_s=%.2f  %s", delay_s_,
                                            waiting ? "MASK PENDING" : "idle"),
                            cv::Point(10, 25), cv::FONT_HERSHEY_SIMPLEX, 0.6,
                            waiting ? cv::Scalar(0, 165, 255) : cv::Scalar(0, 255, 0), 2);
                cv::imshow(window, vis);
            }

            const int key = cv::waitKey(15) & 0xFF;
            if (key == 'q' || key == 27) {
                break;
            }
            if (key == 's' && latest) {
                select_and_queue(window, *latest);
            }
        }
        cv::destroyAllWindows();
    }

private:
    struct CachedColor {
        std_msgs::msg::Header header;
        cv::Mat image;  // BGR8
    };

    struct PublishedMask {
        cv::Mat mask;
        std::chrono::steady_clock::time_point shown_at;
    };

    void color_callback(const sensor_msgs::msg::Image::SharedPtr msg) {
        cv_bridge::CvImagePtr cv_ptr;
        try {
            cv_ptr = cv_bridge::toCvCopy(msg, sensor_msgs::image_encodings::BGR8);
        } catch (cv_bridge::Exception &e) {
            RCLCPP_ERROR(this->get_logger(), "cv_bridge exception: %s", e.what());
            return;
        }
        const double stamp_s = rclcpp::Time(msg->header.stamp).seconds();
        std::lock_guard<std::mutex> lock(mutex_);
        if (!frames_.empty() && stamp_s < rclcpp::Time(frames_.back().header.stamp).seconds()) {
            frames_.clear();  // stamps went backwards (restarted bag)
        }
        frames_.push_back(CachedColor{msg->header, cv_ptr->image});
        while (stamp_s - rclcpp::Time(frames_.front().header.stamp).seconds() > cache_s_) {
            frames_.pop_front();
        }
    }

    // Freeze the given frame, let the user draw a box, and queue its mask for delayed publishing
    void select_and_queue(const std::string &window, const CachedColor &frame) {
        // ENTER/SPACE confirm, C/ESC cancel; incoming frames keep being cached meanwhile
        const cv::Rect box = cv::selectROI(window, frame.image, true, false) & cv::Rect(cv::Point(0, 0), frame.image.size());
        if (box.area() <= 0) {
            RCLCPP_INFO(this->get_logger(), "Selection cancelled");
            return;
        }

        cv::Mat mask;
        if (use_grabcut_) {
            cv::Mat labels, bgd_model, fgd_model;
            cv::grabCut(frame.image, labels, box, bgd_model, fgd_model, grabcut_iters_, cv::GC_INIT_WITH_RECT);
            mask = ((labels == cv::GC_FGD) | (labels == cv::GC_PR_FGD));
        } else {
            mask = cv::Mat::zeros(frame.image.size(), CV_8UC1);
            mask(box).setTo(255);
        }
        const int n_pixels = cv::countNonZero(mask);
        if (n_pixels == 0) {
            RCLCPP_WARN(this->get_logger(), "Empty mask (GrabCut found no foreground), not published");
            return;
        }

        std::lock_guard<std::mutex> lock(mutex_);
        if (pending_) {
            RCLCPP_INFO(this->get_logger(), "Replacing the mask still waiting to be published");
        }
        pending_ = CachedColor{frame.header, mask};
        RCLCPP_INFO(this->get_logger(), "Mask queued: stamp=%.3f pixels=%d, publishing at stamp + %.2f s",
                    rclcpp::Time(frame.header.stamp).seconds(), n_pixels, delay_s_);
    }

    // Publish once node clock >= frame stamp + delay_s (immediately if selecting already took longer)
    void publish_if_due() {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!pending_) {
            return;
        }
        const rclcpp::Time now = this->get_clock()->now();
        const rclcpp::Time mask_stamp(pending_->header.stamp, now.get_clock_type());
        if (now < mask_stamp + rclcpp::Duration::from_seconds(delay_s_)) {
            return;
        }

        cv_bridge::CvImage out(pending_->header, sensor_msgs::image_encodings::MONO8, pending_->image);
        mask_pub_->publish(*out.toImageMsg());
        RCLCPP_INFO(this->get_logger(), "Mask published: stamp=%.3f actual_delay=%.3f s pixels=%d",
                    mask_stamp.seconds(), (now - mask_stamp).seconds(), cv::countNonZero(pending_->image));
        last_published_ = PublishedMask{pending_->image, std::chrono::steady_clock::now()};
        pending_.reset();
    }

    rclcpp::Subscription<sensor_msgs::msg::Image>::SharedPtr color_sub_;
    rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr mask_pub_;
    rclcpp::TimerBase::SharedPtr publish_timer_;
    std::string color_topic_;
    double cache_s_ = 3.0;
    double delay_s_ = 1.5;
    bool use_grabcut_ = true;
    int grabcut_iters_ = 3;

    std::mutex mutex_;
    std::deque<CachedColor> frames_;
    std::optional<CachedColor> pending_;  // header of the selected frame + its mask
    std::optional<PublishedMask> last_published_;
};

int main(int argc, char **argv) {
    rclcpp::init(argc, argv);
    auto node = std::make_shared<MaskInitTool>();

    rclcpp::executors::SingleThreadedExecutor executor;
    executor.add_node(node);
    std::thread spin_thread([&executor]() { executor.spin(); });

    node->run_gui();

    executor.cancel();
    rclcpp::shutdown();
    spin_thread.join();
    return 0;
}
