#!/usr/bin/env python3
"""Grab N color frames + camera_info + TF from the live RealSense for offline calibration.

Run inside the ROS container (needs rclpy, cv_bridge, tf2_ros):
    python3 tools/calib/capture_frames.py --n 30 --out tools/calib/out/cap.npz

Saved keys:
    frames  (N, H, W, 3) uint8 BGR
    K (3, 3), D (n,)                        from camera_info
    t, q                                    map -> camera_color_optical_frame (current extrinsic, xyzw)
    t_link_opt, q_link_opt                  camera_link -> camera_color_optical_frame (RealSense factory)
"""
import argparse
import os

import cv2
import numpy as np
import rclpy
from cv_bridge import CvBridge
from rclpy.node import Node
from rclpy.time import Time
from sensor_msgs.msg import CameraInfo, Image
from tf2_ros import Buffer, TransformListener


def tf_to_arrays(tf):
    tr, q = tf.translation, tf.rotation
    return np.array([tr.x, tr.y, tr.z]), np.array([q.x, q.y, q.z, q.w])


class Capture(Node):
    def __init__(self, args):
        super().__init__('field_calib_capture')
        self.args = args
        self.bridge = CvBridge()
        self.frames = []
        self.info = None
        self.tf_world = None
        self.tf_link = None
        self.buf = Buffer()
        self.listener = TransformListener(self.buf, self)
        self.create_subscription(CameraInfo, args.info_topic, self.on_info, 10)
        self.create_subscription(Image, args.image_topic, self.on_image, 10)

    def on_info(self, msg):
        self.info = msg

    def lookup(self, parent, child):
        try:
            return self.buf.lookup_transform(parent, child, Time()).transform
        except Exception:
            return None

    def on_image(self, msg):
        # Wait for both transforms before collecting frames
        if self.tf_world is None:
            self.tf_world = self.lookup(self.args.world_frame, self.args.optical_frame)
        if self.tf_link is None:
            self.tf_link = self.lookup(self.args.link_frame, self.args.optical_frame)
        if self.tf_world is None or self.tf_link is None:
            return
        if len(self.frames) < self.args.n:
            self.frames.append(self.bridge.imgmsg_to_cv2(msg, 'bgr8'))

    def done(self):
        return len(self.frames) >= self.args.n and self.info is not None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--n', type=int, default=30)
    ap.add_argument('--out', default='tools/calib/out/cap.npz')
    ap.add_argument('--image-topic', default='/camera/camera/color/image_raw')
    ap.add_argument('--info-topic', default='/camera/camera/color/camera_info')
    ap.add_argument('--world-frame', default='map')
    ap.add_argument('--link-frame', default='camera_link')
    ap.add_argument('--optical-frame', default='camera_color_optical_frame')
    args = ap.parse_args()

    rclpy.init()
    node = Capture(args)
    while rclpy.ok() and not node.done():
        rclpy.spin_once(node, timeout_sec=0.5)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    t, q = tf_to_arrays(node.tf_world)
    t_lo, q_lo = tf_to_arrays(node.tf_link)
    np.savez(args.out, frames=np.array(node.frames), K=np.array(node.info.k).reshape(3, 3),
             D=np.array(node.info.d), t=t, q=q, t_link_opt=t_lo, q_link_opt=q_lo)
    cv2.imwrite(os.path.splitext(args.out)[0] + '_frame0.png', node.frames[0])
    print(f'saved {len(node.frames)} frames -> {args.out}')
    print(f'map->optical t={t.round(4).tolist()} q={q.round(4).tolist()}')
    print(f'link->optical t={t_lo.round(4).tolist()} q={q_lo.round(4).tolist()}')
    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()
