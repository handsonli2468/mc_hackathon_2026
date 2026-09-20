#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/image.hpp>
#include <sensor_msgs/image_encodings.hpp>
#include <cv_bridge/cv_bridge.h>
#include <memory>
#include <string>

#include "object_tracker/vlm_client.hpp"

// Standalone VLM bridge: subscribes to the color image, sends frames to the VLM server (object_tracker::VlmClient,
// docs/vlm_transport.md) and publishes the returned object region on mask_topic, stamped with the frame it belongs
// to. Any tracker that consumes mask_topic works with it. mask_tracker_vlm_node does the same in-process, without
// a second subscription to the color image.

using ImageMsg = sensor_msgs::msg::Image;

class VlmBridgeNode : public rclcpp::Node {
public:
    VlmBridgeNode() : Node("vlm_bridge_node") {
        this->declare_parameter<std::string>("color_topic", "/camera_duck/camera/color/image_rect_raw");
        this->declare_parameter<std::string>("mask_topic", "/tracked_object/init_mask");
        this->declare_parameter<bool>("debug.enable", true);

        const std::string color_topic = this->get_parameter("color_topic").as_string();
        const std::string mask_topic = this->get_parameter("mask_topic").as_string();

        mask_pub_ = this->create_publisher<ImageMsg>(mask_topic, rclcpp::QoS(rclcpp::KeepLast(5)).reliable());
        vlm_ = std::make_unique<object_tracker::VlmClient>(
            *this, color_topic, this->get_parameter("debug.enable").as_bool(),
            [this](const cv::Mat &mask, const std_msgs::msg::Header &header) {
                mask_pub_->publish(*cv_bridge::CvImage(header, sensor_msgs::image_encodings::MONO8, mask).toImageMsg());
            });
        color_sub_ = this->create_subscription<ImageMsg>(
            color_topic, rclcpp::SensorDataQoS(),
            std::bind(&VlmBridgeNode::color_callback, this, std::placeholders::_1));

        RCLCPP_INFO(this->get_logger(), "VLM bridge: %s -> %s, masks on %s, refresh %.1f s, timeout %.1f s",
                    color_topic.c_str(), vlm_->endpoint().c_str(), mask_topic.c_str(), vlm_->refresh_period_s(),
                    vlm_->timeout_s());
    }

private:
    void color_callback(const ImageMsg::SharedPtr msg) {
        try {
            // toCvCopy owns its data, so the client may keep it
            vlm_->submit_frame(cv_bridge::toCvCopy(msg, sensor_msgs::image_encodings::BGR8)->image, msg->header);
        } catch (cv_bridge::Exception &e) {
            RCLCPP_ERROR(this->get_logger(), "cv_bridge exception: %s", e.what());
        }
    }

    rclcpp::Subscription<ImageMsg>::SharedPtr color_sub_;
    rclcpp::Publisher<ImageMsg>::SharedPtr mask_pub_;
    std::unique_ptr<object_tracker::VlmClient> vlm_;
};

int main(int argc, char **argv) {
    rclcpp::init(argc, argv);
    rclcpp::spin(std::make_shared<VlmBridgeNode>());
    rclcpp::shutdown();
    return 0;
}
