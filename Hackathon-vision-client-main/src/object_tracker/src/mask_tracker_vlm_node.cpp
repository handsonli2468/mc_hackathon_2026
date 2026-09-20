#include <memory>
#include <mutex>
#include <optional>
#include <string>

#include "object_tracker/mask_tracker_node.hpp"
#include "object_tracker/vlm_client.hpp"

// mask_tracker_node with the VLM bridge built in. Frames sent to the VLM server are the tracker's own processed
// (and cached) frames, so:
//   - the color image is subscribed once instead of twice (less DDS traffic; see DEBUG.md section 11),
//   - a returned mask always refers to a frame the tracker has, so MASK_FRAME_MISSING only happens when the
//     answer takes longer than init.cache_s.
// Masks from the network thread are queued and applied on the executor thread before the next frame, so the
// tracker itself needs no locking. mask_topic is not subscribed; vlm.publish_mask echoes the masks there for
// debugging.

class MaskTrackerVlmNode : public MaskTrackerNode {
public:
    MaskTrackerVlmNode() : MaskTrackerNode("mask_tracker_vlm_node", false) {
        this->declare_parameter<bool>("vlm.publish_mask", false);
        if (this->get_parameter("vlm.publish_mask").as_bool()) {
            mask_pub_ = this->create_publisher<ImageMsg>(this->get_parameter("mask_topic").as_string(),
                                                         rclcpp::QoS(rclcpp::KeepLast(5)).reliable());
        }
        vlm_ = std::make_unique<object_tracker::VlmClient>(
            *this, "the tracker's color frames", is_debug_mode_,
            [this](const cv::Mat &mask, const std_msgs::msg::Header &header) {
                std::lock_guard<std::mutex> lock(mask_mutex_);
                if (queued_mask_) {
                    RCLCPP_WARN(this->get_logger(), "Mask for stamp=%.3f replaced before it was applied",
                                rclcpp::Time(queued_mask_->header.stamp).seconds());
                }
                queued_mask_ = QueuedMask{mask, header};
            });
        RCLCPP_INFO(this->get_logger(), "Built-in VLM client: %s, refresh %.1f s, timeout %.1f s%s",
                    vlm_->endpoint().c_str(), vlm_->refresh_period_s(), vlm_->timeout_s(),
                    mask_pub_ ? ", masks echoed on mask_topic" : "");
    }

    ~MaskTrackerVlmNode() override {
        // stop the network thread before the queue it writes to goes away
        vlm_.reset();
    }

protected:
    void before_frame() override {
        std::optional<QueuedMask> queued;
        {
            std::lock_guard<std::mutex> lock(mask_mutex_);
            queued.swap(queued_mask_);
        }
        if (!queued) {
            return;
        }
        if (mask_pub_) {
            mask_pub_->publish(
                *cv_bridge::CvImage(queued->header, sensor_msgs::image_encodings::MONO8, queued->mask).toImageMsg());
        }
        const double mask_stamp_s = rclcpp::Time(queued->header.stamp).seconds();
        record_event(InitEvent::MASK_RECEIVED, mask_stamp_s,
                     cv::format("size=%dx%d (built-in VLM client)", queued->mask.cols, queued->mask.rows));
        handle_mask(queued->mask, mask_stamp_s);
    }

    void on_frame(const cv::Mat &color, const std_msgs::msg::Header &header) override {
        // color may share the message buffer and is only valid during this call; the client keeps its own copy
        vlm_->submit_frame(color.clone(), header);
    }

private:
    struct QueuedMask {
        cv::Mat mask;
        std_msgs::msg::Header header;
    };

    rclcpp::Publisher<ImageMsg>::SharedPtr mask_pub_;
    std::mutex mask_mutex_;
    std::optional<QueuedMask> queued_mask_;
    std::unique_ptr<object_tracker::VlmClient> vlm_;
};

int main(int argc, char **argv) {
    rclcpp::init(argc, argv);
    try {
        auto node = std::make_shared<MaskTrackerVlmNode>();
        rclcpp::spin(node);
    } catch (const std::exception &e) {
        RCLCPP_FATAL(rclcpp::get_logger("mask_tracker_vlm_node"), "%s", e.what());
        rclcpp::shutdown();
        return 1;
    }
    rclcpp::shutdown();
    return 0;
}
