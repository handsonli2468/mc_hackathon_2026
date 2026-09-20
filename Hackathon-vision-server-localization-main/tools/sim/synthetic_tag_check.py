#!/usr/bin/env python3
"""End-to-end check of homography_duck_node with rendered tag images of known pose.

Publishes a static TF map -> <camera_frame>, camera_info and synthetic images of an
AprilTag 16h5 marker on the level plane z=h, then compares the node outputs with truth.

Run inside the container:
  ros2 run aruco_test homography_duck_node --ros-args \
      -r __node:=homography_duck_sim \
      -p RGB_topic:=/sim/image -p camera_info_topic:=/sim/camera_info \
      -p camera_frame:=sim_color_optical_frame \
      -p pose_topic:=/sim/pose/homography -p plane_lm.pose_topic:=/sim/pose/plane_lm \
      -p target_height:=0.2 -p robot.marker_size:=0.1
  python3 tools/sim/synthetic_tag_check.py
"""
import math

import cv2
import numpy as np
import rclpy
from geometry_msgs.msg import PoseStamped, TransformStamped
from rclpy.node import Node
from sensor_msgs.msg import CameraInfo, Image
from tf2_ros import StaticTransformBroadcaster

from plane_constrained_sim import (CAM_Q_XYZW, CAM_T, H_IMG, K, W, Camera, quat_to_rot,
                                   rot_z, sample_poses, wrap)

TAG_H = 0.2
TAG_SIZE = 0.1
TAG_ID = 1
N_POSES = 30
FRAMES_PER_POSE = 5
SUPERSAMPLE = 4
NOISE_GRAY = 2.0
CAMERA_FRAME = 'sim_color_optical_frame'


def make_tag_texture(px_per_cell=100):
    """16h5 marker (6x6 cells incl. black border) plus 1-cell white margin -> 8x8 cells."""
    d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_16h5)
    marker = cv2.aruco.drawMarker(d, TAG_ID, 6 * px_per_cell)
    return cv2.copyMakeBorder(marker, px_per_cell, px_per_cell, px_per_cell, px_per_cell,
                              cv2.BORDER_CONSTANT, value=255)


def render(cam, tex, x, y, yaw, rng):
    n = tex.shape[0]
    m_per_px = TAG_SIZE / (n * 6.0 / 8.0)
    c = (n - 1) / 2.0
    # texture pixel (u, v, 1) -> marker frame (X = (u-c)*m, Y = -(v-c)*m, Y up) -> world homogeneous
    R = rot_z(yaw)
    Mw = np.zeros((4, 3))
    Mw[:3, :] = np.column_stack([R[:, 0] * m_per_px, -R[:, 1] * m_per_px,
                                 np.array([x, y, TAG_H]) - c * m_per_px * R[:, 0] + c * m_per_px * R[:, 1]])
    Mw[3, 2] = 1.0
    Ks = K.copy()
    Ks[:2] *= SUPERSAMPLE
    Ks[:2, 2] = (K[:2, 2] + 0.5) * SUPERSAMPLE - 0.5
    P = Ks @ np.hstack([cam.R_cw, cam.t_cw[:, None]])
    Hm = P @ Mw
    big = np.full((H_IMG * SUPERSAMPLE, W * SUPERSAMPLE), 150, np.uint8)
    cv2.warpPerspective(tex, Hm, (big.shape[1], big.shape[0]), dst=big,
                        flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_TRANSPARENT)
    img = cv2.resize(big, (W, H_IMG), interpolation=cv2.INTER_AREA).astype(np.float32)
    img += rng.normal(0.0, NOISE_GRAY, img.shape)
    return cv2.cvtColor(np.clip(img, 0, 255).astype(np.uint8), cv2.COLOR_GRAY2BGR)


class Checker(Node):
    def __init__(self):
        super().__init__('synthetic_tag_check')
        self.rng = np.random.default_rng(0)
        self.cam = Camera(quat_to_rot(CAM_Q_XYZW), CAM_T)
        self.poses = sample_poses(self.cam, TAG_H, TAG_SIZE, N_POSES, self.rng, margin=30)
        self.tex = make_tag_texture()
        self.truth = {}
        self.results = {'homography': [], 'plane_lm': []}
        self.frame = 0

        tf = TransformStamped()
        tf.header.stamp = self.get_clock().now().to_msg()
        tf.header.frame_id = 'map'
        tf.child_frame_id = CAMERA_FRAME
        tf.transform.translation.x, tf.transform.translation.y, tf.transform.translation.z = CAM_T.tolist()
        q = CAM_Q_XYZW / np.linalg.norm(CAM_Q_XYZW)
        (tf.transform.rotation.x, tf.transform.rotation.y,
         tf.transform.rotation.z, tf.transform.rotation.w) = q.tolist()
        self.tf_pub = StaticTransformBroadcaster(self)
        self.tf_pub.sendTransform(tf)

        self.info_pub = self.create_publisher(CameraInfo, '/sim/camera_info', 10)
        self.img_pub = self.create_publisher(Image, '/sim/image', 10)
        for key in self.results:
            self.create_subscription(PoseStamped, f'/sim/pose/{key}',
                                     lambda m, k=key: self.on_pose(k, m), 10)
        self.warmup = 30  # frames for the node to get camera_info + TF
        self.timer = self.create_timer(0.1, self.tick)

    def tick(self):
        stamp = self.get_clock().now().to_msg()
        info = CameraInfo()
        info.header.stamp, info.header.frame_id = stamp, CAMERA_FRAME
        info.width, info.height = W, H_IMG
        info.distortion_model = 'plumb_bob'
        info.d = [0.0] * 5  # rendered with an ideal pinhole
        info.k = K.ravel().tolist()
        info.p = np.hstack([K, np.zeros((3, 1))]).ravel().tolist()
        self.info_pub.publish(info)

        idx = max(0, self.frame - self.warmup) // FRAMES_PER_POSE
        if idx >= len(self.poses):
            if self.frame > self.warmup + len(self.poses) * FRAMES_PER_POSE + 20:
                self.report()
                raise SystemExit
            self.frame += 1
            return
        x, y, yaw = self.poses[idx]
        img = render(self.cam, self.tex, x, y, yaw, self.rng)
        msg = Image()
        msg.header.stamp, msg.header.frame_id = stamp, CAMERA_FRAME
        msg.height, msg.width, msg.encoding, msg.step = H_IMG, W, 'bgr8', W * 3
        msg.data = img.tobytes()
        if self.frame >= self.warmup:
            self.truth[(stamp.sec, stamp.nanosec)] = (x, y, yaw)
        self.img_pub.publish(msg)
        self.frame += 1

    def on_pose(self, key, msg):
        gt = self.truth.get((msg.header.stamp.sec, msg.header.stamp.nanosec))
        if gt is None:
            return
        yaw = 2.0 * math.atan2(msg.pose.orientation.z, msg.pose.orientation.w)
        self.results[key].append((math.hypot(msg.pose.position.x - gt[0], msg.pose.position.y - gt[1]),
                                  abs(wrap(yaw - gt[2]))))

    def report(self):
        n_frames = len(self.truth)
        print(f'\nframes with truth: {n_frames}, poses: {len(self.poses)}')
        print('| output | detected | xy RMS (mm) | xy max (mm) | yaw RMS (deg) | yaw max (deg) |')
        print('|---|---:|---:|---:|---:|---:|')
        for key, r in self.results.items():
            if not r:
                print(f'| {key} | 0/{n_frames} | - | - | - | - |')
                continue
            e = np.array(r)
            dxy, dyaw = e[:, 0] * 1000, np.degrees(e[:, 1])
            print(f'| {key} | {len(r)}/{n_frames} | {np.sqrt(np.mean(dxy**2)):.1f} | {dxy.max():.1f} '
                  f'| {np.sqrt(np.mean(dyaw**2)):.2f} | {dyaw.max():.2f} |')


def main():
    rclpy.init()
    node = Checker()
    try:
        rclpy.spin(node)
    except SystemExit:
        pass
    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()
