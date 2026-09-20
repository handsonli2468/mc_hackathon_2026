"""Save one camera frame as a lossless PNG for the eval set (run on the Pi, inside the ROS 2 container).

  python3 grab_frame.py distract-1.png [--topic /camera/camera/color/image_rect_raw]

Standalone on purpose: only needs rclpy, sensor_msgs, numpy and either cv2 or PIL,
so it can be copied onto the Pi without this repo. Frames are kept at the camera's
native size; scripts/eval_locate.py downsizes them like the client does.
"""
import argparse
import sys

import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image as ImageMsg


def to_rgb(msg):
    channels = {'rgb8': 3, 'bgr8': 3, 'rgba8': 4, 'bgra8': 4}
    if msg.encoding not in channels:
        raise ValueError(f'unsupported encoding {msg.encoding}')
    n = channels[msg.encoding]
    rows = np.frombuffer(bytes(msg.data), np.uint8).reshape(msg.height, msg.step)
    image = rows[:, :msg.width * n].reshape(msg.height, msg.width, n)[:, :, :3]
    return image[:, :, ::-1] if msg.encoding.startswith('bgr') else image


def save_png(rgb, path):
    try:
        from PIL import Image
        Image.fromarray(np.ascontiguousarray(rgb)).save(path)
    except ImportError:
        import cv2
        cv2.imwrite(path, np.ascontiguousarray(rgb[:, :, ::-1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('out', help='output .png path')
    parser.add_argument('--topic', default='/camera/camera/color/image_rect_raw')
    parser.add_argument('--skip', type=int, default=5, help='drop the first N frames (auto exposure settling)')
    parser.add_argument('--timeout', type=float, default=10.0)
    args = parser.parse_args()

    rclpy.init()
    node = Node('grab_frame')
    received = []
    node.create_subscription(ImageMsg, args.topic, received.append, qos_profile_sensor_data)
    deadline = node.get_clock().now().nanoseconds + int(args.timeout * 1e9)
    while len(received) <= args.skip and node.get_clock().now().nanoseconds < deadline:
        rclpy.spin_once(node, timeout_sec=0.5)
    node.destroy_node()
    rclpy.shutdown()
    if len(received) <= args.skip:
        sys.exit(f'no frame on {args.topic} within {args.timeout}s; check `ros2 topic list`')
    msg = received[-1]
    save_png(to_rgb(msg), args.out)
    print(f'saved {args.out} {msg.width}x{msg.height} {msg.encoding}')


if __name__ == '__main__':
    main()
