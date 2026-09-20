#include "object_tracker/mask_tracker_node.hpp"

int main(int argc, char **argv) {
    rclcpp::init(argc, argv);
    try {
        auto node = std::make_shared<MaskTrackerNode>();
        rclcpp::spin(node);
    } catch (const std::exception &e) {
        RCLCPP_FATAL(rclcpp::get_logger("mask_tracker_node"), "%s", e.what());
        rclcpp::shutdown();
        return 1;
    }
    rclcpp::shutdown();
    return 0;
}
