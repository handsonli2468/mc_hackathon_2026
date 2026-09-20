"""field_calib_node: table-edge extrinsic calibration with live debug images.

~/debug/image         : what to watch: live overlay, or the calibration stages / result (calib.hold_s)
live (timer)          : edge search + residuals under the current TF (no optimization)
                        -> ~/live/overlay, warns when the edges drift (camera bumped)
~/calibrate (Trigger) : re-read the table size (field_file) -> median of N frames -> calibration from
                        every available initial pose (depth plane + rectangle, current TF), best one kept
                        -> if it passes the basic checks: publish map -> camera_link (static TF) and save
                        it to calib.result_file. Every band is published to ~/calib/* and saved as PNG.
startup               : publish the saved result; if there is none, calibrate once (calib.on_startup).
live.compact          : overlays published at camera size without the side panel (+ JPEG on
                        ~/live/overlay/compressed and ~/calib/overlay/compressed) for >= 10 Hz streaming;
                        PNGs written to the calibration debug_dir stay the full version.
This node is the only publisher of map -> camera_link (rs_launch.py cam_tf.enable defaults to false).
"""
import array
import datetime
import os
import threading
import time

import cv2
import numpy as np
import rclpy
import yaml
from ament_index_python.packages import get_package_share_directory
from cv_bridge import CvBridge
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.time import Time
from sensor_msgs.msg import CameraInfo, CompressedImage, Image
from geometry_msgs.msg import TransformStamped
from std_srvs.srv import Trigger
from tf2_ros import Buffer, StaticTransformBroadcaster, TransformListener

from . import core, debug_viz

VIEWS = ('overlay', 'strips', 'residuals')
JPEG_FORMAT = 'bgr8; jpeg compressed bgr8'  # same as image_transport compressed
CAM_TF_NAMES = ('x', 'y', 'z', 'roll', 'pitch', 'yaw')


def rot_to_quat(R):
    """Rotation matrix -> quaternion (x, y, z, w)."""
    rvec, _ = cv2.Rodrigues(np.asarray(R, dtype=np.float64))
    angle = float(np.linalg.norm(rvec))
    if angle < 1e-12:
        return np.array([0.0, 0.0, 0.0, 1.0])
    axis = rvec.ravel() / angle
    return np.concatenate([axis * np.sin(angle / 2), [np.cos(angle / 2)]])


def tf_to_rt(tf):
    tr, q = tf.translation, tf.rotation
    return core.quat_to_rot([q.x, q.y, q.z, q.w]), np.array([tr.x, tr.y, tr.z])


class FieldCalibNode(Node):
    def __init__(self):
        super().__init__('field_calib_node')
        dp = self.declare_parameter
        dp('image_topic', '/camera/camera/color/image_raw')
        dp('camera_info_topic', '/camera/camera/color/camera_info')
        dp('depth_topic', '/camera/camera/depth/image_rect_raw')
        dp('depth_info_topic', '/camera/camera/depth/camera_info')
        dp('world_frame', 'map')
        dp('link_frame', 'camera_link')
        dp('optical_frame', 'camera_color_optical_frame')
        dp('depth_frame', 'camera_depth_optical_frame')
        dp('field_file', '')
        dp('field.disabled_segments', '')
        dp('edge.step', 0.01)
        dp('edge.blur', 1.0)
        dp('edge.grad_thresh', 6.0)
        dp('edge.contrast_thresh', 20.0)
        dp('lm.delta', 1.5)
        dp('calib.frames', 30)
        dp('calib.bands', [80.0, 40.0, 20.0, 12.0])
        dp('calib.stage_delay', 1.0)
        dp('calib.output_dir', '~/.ros/field_calib')
        dp('calib.result_file', '')          # '' = <output_dir>/cam_tf.yaml
        dp('calib.on_startup', 'if_missing')  # if_missing / always / never
        dp('calib.min_accept_ratio', 0.6)
        dp('calib.max_rms_px', 1.5)
        dp('calib.apply', True)  # false: dry run, never publish TF / write result_file
        dp('live.enable', True)
        dp('live.period', 1.0)
        dp('live.band', 12.0)
        dp('live.warn_rms_px', 1.5)
        dp('live.warn_inlier_ratio', 0.7)
        dp('live.compact', False)     # camera-size overlays (no side panel) + JPEG compressed topics
        dp('live.jpeg_quality', 80)
        dp('depth.enable', True)
        dp('depth.frames', 10)
        dp('tag.id', 1)
        dp('tag.size', 0.08)
        dp('tag.height', 0.2)
        dp('calib.hold_s', 15.0)  # ~/debug/image keeps showing the calibration result this long

        self.field = self.load_field()

        self.bridge = CvBridge()
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)
        self.tf_broadcaster = StaticTransformBroadcaster(self)
        self.published_cam_tf = None
        self.published_dx = []  # table x offsets of the applied calibration
        self.lock = threading.Lock()
        self.K = self.D = self.K_depth = None
        self.latest = None
        self.frames = None      # list while collecting for a calibration
        self.depth_frames = None
        self.calib_views = None   # views published on ~/calib/* (compact overlay when live.compact)
        self.calib_jpeg = None    # encoded calib overlay, re-sent without re-encoding
        self.live_views = None
        self.timer_ticks = 0
        self.show_calib_until = 0.0  # ~/debug/image shows the calibration result until this time
        self.busy = False

        sensors = MutuallyExclusiveCallbackGroup()
        self.create_subscription(CameraInfo, self.p('camera_info_topic'), self.on_info, 10, callback_group=sensors)
        self.create_subscription(Image, self.p('image_topic'), self.on_image, 10, callback_group=sensors)
        if self.p('depth.enable'):
            self.create_subscription(CameraInfo, self.p('depth_info_topic'), self.on_depth_info, 10,
                                     callback_group=sensors)
            self.create_subscription(Image, self.p('depth_topic'), self.on_depth, 10, callback_group=sensors)
        self.pubs = {f'{kind}/{v}': self.create_publisher(Image, f'~/{kind}/{v}', 1)
                     for kind, vs in (('live', ('overlay',)), ('calib', VIEWS)) for v in vs}
        # What an operator watches: live overlay, or the calibration stages / result
        self.debug_pub = self.create_publisher(Image, '~/debug/image', 1)
        self.compact = bool(self.p('live.compact'))
        self.jpeg_pubs = {}
        if self.compact:
            # Same QoS as the raw overlays (reliable, keep last 1)
            self.jpeg_pubs = {kind: self.create_publisher(CompressedImage, f'~/{kind}/overlay/compressed', 1)
                              for kind in ('live', 'calib')}
        self.create_service(Trigger, '~/calibrate', self.on_calibrate,
                            callback_group=MutuallyExclusiveCallbackGroup())
        self.create_timer(self.p('live.period'), self.on_timer, callback_group=MutuallyExclusiveCallbackGroup())
        threading.Thread(target=self.startup, daemon=True).start()
        self.get_logger().info('ready: ros2 service call /field_calib_node/calibrate std_srvs/srv/Trigger')

    def p(self, name):
        return self.get_parameter(name).value

    def load_field(self):
        """Read the table size input (re-read on every calibration)."""
        field_file = self.p('field_file') or os.path.join(
            get_package_share_directory('field_calib'), 'config', 'field.yaml')
        with open(field_file) as f:
            field_cfg = yaml.safe_load(f)
        disabled = [s.strip() for s in self.p('field.disabled_segments').split(',') if s.strip()]
        if disabled:
            field_cfg['disabled_segments'] = disabled
        field = core.Field(field_cfg)
        self.get_logger().info(f'table {field.L} x {field.d} m x {field.count} ({field_file}), '
                               f'segments {field.names}')
        return field

    def result_file(self):
        return os.path.expanduser(self.p('calib.result_file') or
                                  os.path.join(self.p('calib.output_dir'), 'cam_tf.yaml'))

    # ------------------------------------------------------------------ map -> camera_link
    def publish_cam_tf(self, cam_tf):
        """cam_tf = [x, y, z, roll, pitch, yaw] of map -> camera_link."""
        q = rot_to_quat(core.rpy_to_rot(*cam_tf[3:6]))
        msg = TransformStamped()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = self.p('world_frame')
        msg.child_frame_id = self.p('link_frame')
        msg.transform.translation.x, msg.transform.translation.y, msg.transform.translation.z = \
            (float(v) for v in cam_tf[:3])
        msg.transform.rotation.x, msg.transform.rotation.y, msg.transform.rotation.z, msg.transform.rotation.w = \
            (float(v) for v in q)
        self.tf_broadcaster.sendTransform(msg)
        self.published_cam_tf = list(cam_tf)
        self.get_logger().info('published ' + self.p('world_frame') + ' -> ' + self.p('link_frame') + ': ' +
                               ' '.join(f'{n}={v:.5f}' for n, v in zip(CAM_TF_NAMES, cam_tf)))

    def startup(self):
        """Publish the saved extrinsic, or calibrate once when there is none."""
        time.sleep(2.0)  # let the TF buffer fill
        if self.tf_buffer.can_transform(self.p('world_frame'), self.p('link_frame'), Time()):
            self.get_logger().warn(
                f'{self.p("world_frame")} -> {self.p("link_frame")} is already published by another node '
                '(rs_launch.py cam_tf.enable:=true?). Two publishers override each other; disable one.')
        path, mode = self.result_file(), self.p('calib.on_startup')
        saved = None
        if os.path.exists(path):
            with open(path) as f:
                saved = (yaml.safe_load(f) or {}).get('cam_tf')
        if not self.p('calib.apply'):
            self.get_logger().info('dry run (calib.apply = false): not publishing any TF')
            return
        if saved and mode != 'always':
            with open(path) as f:
                self.published_dx = list((yaml.safe_load(f) or {}).get('table_dx') or [])
            self.publish_cam_tf([saved[n] for n in CAM_TF_NAMES])
            self.get_logger().info(f'loaded {path}')
            return
        if mode == 'never':
            self.get_logger().warn(f'no saved extrinsic ({path}); press c or call ~/calibrate')
            return
        self.get_logger().info('no saved extrinsic: calibrating automatically')
        t0 = time.time()
        while time.time() - t0 < 30.0:
            with self.lock:
                ready = self.K is not None and self.latest is not None
            if ready:
                break
            time.sleep(0.5)
        ok, msg = self.try_calibrate()
        if not ok:
            self.get_logger().error(f'startup calibration failed: {msg}')

    # ------------------------------------------------------------------ sensors
    def on_info(self, msg):
        self.K = np.array(msg.k).reshape(3, 3)
        self.D = np.array(msg.d)

    def on_depth_info(self, msg):
        self.K_depth = np.array(msg.k).reshape(3, 3)

    def on_image(self, msg):
        img = self.bridge.imgmsg_to_cv2(msg, 'bgr8')
        with self.lock:
            self.latest = (img, msg.header)
            if self.frames is not None and len(self.frames) < self.p('calib.frames'):
                self.frames.append(img)

    def on_depth(self, msg):
        with self.lock:
            if self.depth_frames is None or len(self.depth_frames) >= self.p('depth.frames'):
                return
        d = self.bridge.imgmsg_to_cv2(msg, 'passthrough')
        scale = 0.001 if d.dtype == np.uint16 else 1.0  # RealSense 16UC1 is mm
        with self.lock:
            if self.depth_frames is not None:
                self.depth_frames.append(d.astype(np.float32) * scale)

    def lookup(self, parent, child):
        return tf_to_rt(self.tf_buffer.lookup_transform(parent, child, Time()).transform)

    def tf_pose(self, field):
        """Current TF as the full parameter vector of `field` (with the applied table offsets)."""
        p = field.extend_pose(self.current_pose())
        if len(self.published_dx) == field.n_dx:
            p[6:] = self.published_dx
        return p

    def current_pose(self):
        R_wc, t_wc = self.lookup(self.p('world_frame'), self.p('optical_frame'))
        return core.pose_from_world_cam(R_wc, t_wc)  # 6-DoF; callers extend it for their field

    @staticmethod
    def has_subs(pub):
        return pub is not None and pub.get_subscription_count() > 0

    def publish_image(self, pub, im, header):
        """Convert and publish only when someone listens (large images)."""
        if not self.has_subs(pub):
            return
        msg = self.bridge.cv2_to_imgmsg(im, 'bgr8')
        msg.header = header
        pub.publish(msg)

    def publish_views(self, kind, views, header):
        for name, im in views.items():
            self.publish_image(self.pubs[f'{kind}/{name}'], im, header)

    def publish_debug(self, im, header):
        self.publish_image(self.debug_pub, im, header)

    def encode_jpeg(self, im):
        ok, buf = cv2.imencode('.jpg', im, [cv2.IMWRITE_JPEG_QUALITY, int(self.p('live.jpeg_quality'))])
        if not ok:
            return None
        # array('B') is taken as-is by the uint8[] field; bytes would be checked value by value
        data = array.array('B')
        data.frombytes(buf.tobytes())
        return data

    def publish_jpeg(self, kind, data, header):
        pub = self.jpeg_pubs.get(kind)
        if data is None or not self.has_subs(pub):
            return
        msg = CompressedImage()
        msg.header = header
        msg.format = JPEG_FORMAT
        msg.data = data
        pub.publish(msg)

    def overlay_for_publish(self, img, field, stage, title):
        """Overlay sent on the topics: compact (camera size) or the full debug version."""
        if self.compact:
            return debug_viz.render_overlay_compact(img, field, self.K, self.D, stage, title)
        return debug_viz.render_overlay(img, field, self.K, self.D, stage, self.p('lm.delta'), title)

    def publish_calib(self, views, header):
        """Store and publish the calibration views of one stage (or the final result)."""
        jpeg = None
        if self.compact and self.has_subs(self.jpeg_pubs['calib']):
            jpeg = self.encode_jpeg(views['overlay'])
        with self.lock:
            self.calib_views, self.calib_jpeg = views, jpeg
        self.publish_views('calib', views, header)
        self.publish_jpeg('calib', jpeg, header)
        self.publish_debug(views['overlay'], header)

    def blurred(self, img):
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
        return cv2.GaussianBlur(gray, (0, 0), self.p('edge.blur'))

    # ------------------------------------------------------------------ live view / monitor
    def on_timer(self):
        with self.lock:
            latest, busy = self.latest, self.busy
        if latest is None or self.K is None:
            return
        img, header = latest
        # Keep the last calibration result visible, re-sent about once per second whatever live.period is
        period = self.p('live.period')
        resend = self.timer_ticks % max(1, round(1.0 / period)) == 0 if period > 0 else True
        self.timer_ticks += 1
        with self.lock:
            calib_views, calib_jpeg = self.calib_views, self.calib_jpeg
        if calib_views is not None and resend:
            self.publish_views('calib', calib_views, header)
            if self.compact and calib_jpeg is None and self.has_subs(self.jpeg_pubs['calib']):
                calib_jpeg = self.encode_jpeg(calib_views['overlay'])  # subscriber came later
                with self.lock:
                    if self.calib_views is calib_views:
                        self.calib_jpeg = calib_jpeg
            self.publish_jpeg('calib', calib_jpeg, header)
            if not busy and time.time() < self.show_calib_until:
                self.publish_debug(calib_views['overlay'], header)
        if busy or not self.p('live.enable'):
            return
        field = self.field
        try:
            p = self.tf_pose(field)
        except Exception as e:  # noqa: BLE001
            self.get_logger().warn(f'TF not available: {e}', throttle_duration_sec=5.0)
            return
        stage = core.evaluate(self.blurred(img), field, p, self.K, self.D, self.p('live.band'),
                              self.p('edge.step'), self.p('edge.grad_thresh'), self.p('edge.contrast_thresh'),
                              self.p('lm.delta'))
        show_debug = time.time() >= self.show_calib_until
        want_raw = self.has_subs(self.pubs['live/overlay']) or (show_debug and self.has_subs(self.debug_pub))
        want_jpeg = self.has_subs(self.jpeg_pubs.get('live'))
        if want_raw or want_jpeg:  # drawing is the expensive part: skip it when nobody watches
            views = {'overlay': self.overlay_for_publish(img, field, stage, 'live')}
            with self.lock:
                self.live_views = views
            self.publish_views('live', views, header)
            if show_debug:
                self.publish_debug(views['overlay'], header)
            if want_jpeg:
                self.publish_jpeg('live', self.encode_jpeg(views['overlay']), header)
        d = stage['diag']
        considered = d['status'] != core.OUT_OF_IMAGE
        acc = d['status'] == core.ACCEPTED
        rms = float(np.sqrt(np.mean(d['resid'][acc] ** 2))) if acc.any() else float('nan')
        ratio = acc.sum() / max(1, considered.sum())
        if not (rms <= self.p('live.warn_rms_px')) or ratio < self.p('live.warn_inlier_ratio'):
            self.get_logger().warn(
                f'table edges do not match the current extrinsic: inlier RMS {rms:.2f} px, '
                f'accepted {ratio * 100:.0f}% (occlusion, moved table or bumped camera?)',
                throttle_duration_sec=10.0)

    # ------------------------------------------------------------------ calibration
    def collect(self, timeout):
        with self.lock:
            self.frames = []
            self.depth_frames = [] if self.p('depth.enable') else None
        t0 = time.time()
        n_img, n_depth = self.p('calib.frames'), self.p('depth.frames')
        while time.time() - t0 < timeout:
            with self.lock:
                done = len(self.frames) >= n_img and (self.depth_frames is None or len(self.depth_frames) >= n_depth)
            if done:
                break
            time.sleep(0.05)
        with self.lock:
            frames, depth = self.frames, self.depth_frames
            self.frames = self.depth_frames = None
        return frames, depth

    def on_calibrate(self, request, response):
        del request
        response.success, response.message = self.try_calibrate()
        return response

    def try_calibrate(self):
        """Run one calibration unless one is already running. Returns (success, message)."""
        with self.lock:
            if self.busy:
                return False, 'calibration already running'
            self.busy = True
        try:
            return True, self.run_calibration()
        except Exception as e:  # noqa: BLE001
            msg = f'calibration failed: {e}'
            self.get_logger().error(msg)
            return False, msg
        finally:
            with self.lock:
                self.busy = False
                self.show_calib_until = time.time() + self.p('calib.hold_s')

    def run_calibration(self):
        log = self.get_logger().info
        if self.K is None:
            raise RuntimeError('no camera_info yet')
        field = self.load_field()
        R_lo, t_lo = self.lookup(self.p('link_frame'), self.p('optical_frame'))
        try:
            p_tf = self.tf_pose(field)
        except Exception:  # noqa: BLE001
            p_tf = None
        frames, depth = self.collect(timeout=10.0)
        if len(frames) < 3:
            raise RuntimeError(f'only {len(frames)} frames received')
        med = np.median(np.array(frames), axis=0).astype(np.uint8)
        gray_u8 = cv2.cvtColor(med, cv2.COLOR_BGR2GRAY)
        header = self.latest[1]
        out_dir = os.path.expanduser(self.p('calib.output_dir'))
        stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        debug_dir = os.path.join(out_dir, stamp)
        os.makedirs(debug_dir, exist_ok=True)
        delta = self.p('lm.delta')
        lines = [f'table {field.L} x {field.d} m x {field.count}']

        # Initial poses: depth plane + rectangle (no prior needed) and the current TF
        dmed, R_cd, t_cd = None, None, None
        if depth and self.K_depth is not None:
            try:
                R_cd, t_cd = self.lookup(self.p('optical_frame'), self.p('depth_frame'))
                dmed = np.median(np.array(depth), axis=0)
            except Exception as e:  # noqa: BLE001
                lines.append(f'depth unavailable: {e}')
        candidates = []
        if dmed is not None:
            try:
                p_depth, info = core.init_from_depth(dmed, self.K_depth, R_cd, t_cd, field)
                candidates.append(('depth', p_depth))
                lines.append(f'depth init: table region {info["measured_length"]:.3f} x '
                             f'{info["measured_width"]:.3f} m, camera height {info["height"]:.3f} m')
            except RuntimeError as e:
                lines.append(f'depth init failed: {e}')
        if p_tf is not None:
            candidates.append(('tf', p_tf))
        if not candidates:
            raise RuntimeError('no initial pose: depth init failed and no current TF\n' + '\n'.join(lines))

        results = []
        for name, p0 in candidates:
            def on_stage(stage, name=name):
                title = f'calib ({name} init)'
                views = debug_viz.render_all(med, field, self.K, self.D, stage, delta, title)
                for v, im in views.items():  # PNGs are always the full version
                    cv2.imwrite(os.path.join(debug_dir, f'{name}_band_{stage["index"]}_{stage["band"]:.0f}px_{v}.png'),
                                im)
                if self.compact:
                    views = dict(views, overlay=self.overlay_for_publish(med, field, stage, title))
                self.publish_calib(views, header)
                time.sleep(self.p('calib.stage_delay'))  # let viewers see every stage

            log(f'calibrating from {len(frames)} frames, {name} init, debug images -> {debug_dir}')
            try:
                p, stage = core.calibrate(self.blurred(med), field, p0, self.K, self.D,
                                          list(self.p('calib.bands')), self.p('edge.step'),
                                          self.p('edge.grad_thresh'), self.p('edge.contrast_thresh'), delta,
                                          callback=on_stage, log=log)
            except RuntimeError as e:
                lines.append(f'{name} init: calibration failed: {e}')
                continue
            q = core.stage_quality(stage)
            lines.append(f'{name} init: accepted {q["accept_ratio"] * 100:.1f}%, inlier RMS {q["rms"]:.3f} px')
            results.append((name, p0, p, stage, q))
        if not results:
            raise RuntimeError('\n'.join(lines))
        name, p0, p, stage, q = max(results, key=lambda r: (round(r[4]['accept_ratio'], 2), -r[4]['rms']))
        if len(results) > 1:
            dmm, ddeg = core.pose_delta(results[0][2], results[1][2])
            lines.append(f'chosen: {name} init (the two results differ by {dmm:.1f} mm / {ddeg:.3f} deg)')

        # Basic checks before touching the TF
        problems = []
        if q['accept_ratio'] < self.p('calib.min_accept_ratio'):
            problems.append(f'accepted {q["accept_ratio"] * 100:.0f}% < {self.p("calib.min_accept_ratio") * 100:.0f}%')
        if not q['rms'] <= self.p('calib.max_rms_px'):
            problems.append(f'inlier RMS {q["rms"]:.2f} px > {self.p("calib.max_rms_px")} px')
        passed = not problems
        title = 'calib PASS' if passed else 'calib FAIL: ' + '; '.join(problems)
        views = debug_viz.render_all(med, field, self.K, self.D, stage, delta, title)
        for v, im in views.items():  # final_overlay.png stays the full version (read by the App)
            cv2.imwrite(os.path.join(debug_dir, f'final_{v}.png'), im)
        if self.compact:
            views = dict(views, overlay=self.overlay_for_publish(med, field, stage, title))
        self.publish_calib(views, header)

        tf1 = core.cam_tf_from_pose(p, R_lo, t_lo)
        rows = core.segment_stats(field, stage['diag'])
        lines += ['per segment (last band):'] + core.format_segment_table(rows)
        lines.append('cam_tf        before   calibrated')
        tf_before = self.published_cam_tf
        for i, n in enumerate(CAM_TF_NAMES):
            before = f'{tf_before[i]:10.4f}' if tf_before else '         -'
            lines.append(f'  {n:6s} {before} {tf1[i]:11.4f}')
        if field.n_dx:
            lines.append(f'table x offsets (tables 1..): {np.round(core.get_dx(p) * 1000, 1)} mm')
        depth_result = None
        if dmed is not None:
            depth_result = core.depth_plane_check(dmed, self.K_depth, R_cd, t_cd, field, p)
        if depth_result:
            lines.append(f'depth plane: normal vs edge solution {depth_result["tilt_deg"]:.3f} deg, camera height '
                         f'depth {depth_result["height_depth"]:.4f} m vs edge {depth_result["height_edge"]:.4f} m')
        lines += core.tag_check(gray_u8, [('calibrated', p)], self.K, self.D,
                                self.p('tag.id'), self.p('tag.size'), self.p('tag.height'))

        result = {
            'calibrated_at': stamp,
            'table': {'length': field.L, 'depth': field.d, 'count': field.count},
            'cam_tf': {n: round(float(v), 5) for n, v in zip(CAM_TF_NAMES, tf1)},
            'table_dx': [round(float(v), 5) for v in core.get_dx(p)],
            'init': name,
            'passed': passed,
            'problems': problems,
            'quality': {
                'accept_ratio': round(q['accept_ratio'], 4), 'rms_px': round(q['rms'], 4),
                'segments': {r['name']: {'accepted': r['counts'][core.ACCEPTED], 'samples': r['n'],
                                         'rms_px': round(r['rms'], 3), 'mean_px': round(r['mean'], 3)}
                             for r in rows},
                'depth_plane': ({k: (round(v, 4) if isinstance(v, float) else v) for k, v in depth_result.items()}
                                if depth_result else None),
            },
            'debug_dir': debug_dir,
        }
        with open(os.path.join(debug_dir, 'cam_tf.yaml'), 'w') as f:
            yaml.safe_dump(result, f, sort_keys=False)
        if not passed:
            text = '\n'.join(lines + ['NOT APPLIED: ' + '; '.join(problems)])
            log('\n' + text)
            raise RuntimeError(text)

        if not self.p('calib.apply'):
            text = '\n'.join(lines + ['DRY RUN (calib.apply = false): TF and result_file not changed'])
            log('\n' + text)
            return text
        path = self.result_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            yaml.safe_dump(result, f, sort_keys=False)
        self.published_dx = [float(v) for v in core.get_dx(p)]
        self.publish_cam_tf(tf1)
        self.field = field
        lines.append(f'APPLIED: published map -> camera_link, saved {path}')
        text = '\n'.join(lines)
        log('\n' + text)
        return text


def main():
    rclpy.init()
    node = FieldCalibNode()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == '__main__':
    main()
