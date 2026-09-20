#!/usr/bin/env python3
"""Offline table-edge extrinsic calibration (CLI around src/field_calib/field_calib/core.py).

Usage:
    python3 tools/calib/field_edge_calib.py tools/calib/out/cap1.npz          # debug PNGs per band
    python3 tools/calib/field_edge_calib.py tools/calib/out/cap1.npz --show   # step through bands
    python3 tools/calib/field_edge_calib.py --selftest [--show]

Debug images per band (out/debug/band_<i>_<band>px_*.png):
    overlay   : search bands, accepted / rejected samples (reason colored), corner zoom, stats
    strips    : every segment straightened, 4x across the edge (down = outward / floor side)
    residuals : residual vs position along each segment
"""
import argparse
import math
import os
import sys

import cv2
import numpy as np
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, 'src', 'field_calib'))

from field_calib import core, debug_viz, synthetic  # noqa: E402


class DebugSink:
    """Writes (and optionally shows) the debug views of every stage."""

    def __init__(self, img_bgr, field, K, D, delta, out_dir, show, title):
        self.img, self.field, self.K, self.D, self.delta = img_bgr, field, K, D, delta
        self.out_dir, self.show, self.title = out_dir, show, title
        os.makedirs(out_dir, exist_ok=True)

    def __call__(self, stage):
        views = debug_viz.render_all(self.img, self.field, self.K, self.D, stage, self.delta, self.title)
        prefix = os.path.join(self.out_dir, f'band_{stage["index"]}_{stage["band"]:.0f}px')
        for name, im in views.items():
            cv2.imwrite(f'{prefix}_{name}.png', im)
        if self.show:
            for name, im in views.items():
                cv2.namedWindow(name, cv2.WINDOW_NORMAL)
                cv2.imshow(name, im)
            print(f'  [band {stage["band"]:.0f}px] any key: next stage, q: stop showing')
            if cv2.waitKey(0) & 0xFF == ord('q'):
                self.show = False
                cv2.destroyAllWindows()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('capture', nargs='?', help='npz from capture_frames.py')
    ap.add_argument('--field', default=os.path.join(REPO, 'src', 'field_calib', 'config', 'field.yaml'))
    ap.add_argument('--out-dir', default=os.path.join(HERE, 'out'))
    ap.add_argument('--bands', type=float, nargs='+', default=[80, 40, 20, 12])
    ap.add_argument('--step', type=float, default=0.01, help='edge sample spacing on the model (m)')
    ap.add_argument('--blur', type=float, default=1.0, help='Gaussian sigma before edge search (px)')
    ap.add_argument('--grad-thresh', type=float, default=6.0, help='min |gradient| (gray/px)')
    ap.add_argument('--contrast-thresh', type=float, default=20.0, help='min table/floor contrast (gray)')
    ap.add_argument('--delta', type=float, default=1.5, help='Huber threshold (px)')
    ap.add_argument('--tag-id', type=int, default=1)
    ap.add_argument('--tag-size', type=float, default=0.08)
    ap.add_argument('--tag-height', type=float, default=0.2)
    ap.add_argument('--disable', nargs='*', help='segments to leave out (overrides field.yaml)')
    ap.add_argument('--show', action='store_true', help='show the debug views of every band')
    ap.add_argument('--selftest', action='store_true')
    args = ap.parse_args()

    with open(args.field) as f:
        field_cfg = yaml.safe_load(f)
    if args.disable is not None:
        field_cfg['disabled_segments'] = args.disable
    field = core.Field(field_cfg)
    debug_dir = os.path.join(args.out_dir, 'debug')

    if args.selftest:
        sink_holder = {}

        def cb(stage):
            sink_holder['sink'](stage)
        p_true = core.pose_from_cam_tf(synthetic.CAM_TF, core.quat_to_rot(synthetic.Q_LO), synthetic.T_LO)
        gray = synthetic.render_scene(field, p_true, synthetic.true_table_dx(field))
        sink_holder['sink'] = DebugSink(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR), field, synthetic.K, synthetic.D,
                                        args.delta, os.path.join(args.out_dir, 'debug_selftest'), args.show,
                                        'selftest')
        ok, _, _ = synthetic.selftest(field, args.bands, args.step, args.blur, args.grad_thresh,
                                      args.contrast_thresh, args.delta, callback=cb)
        print('\nno previous extrinsic: depth initialization')
        ok &= synthetic.selftest_auto(field, args.bands, args.step, args.blur, args.grad_thresh,
                                      args.contrast_thresh, args.delta)
        print('selftest', 'PASSED' if ok else 'FAILED')
        sys.exit(0 if ok else 1)
    if not args.capture:
        ap.error('capture npz is required (or use --selftest)')

    cap = np.load(args.capture)
    if 't_link_opt' not in cap:
        sys.exit('capture has no camera_link -> optical TF; re-capture with capture_frames.py')
    K, D = cap['K'], cap['D']
    med = np.median(cap['frames'], axis=0).astype(np.uint8)
    gray_u8 = cv2.cvtColor(med, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray_u8.astype(np.float32), (0, 0), args.blur)
    R_lo, t_lo = core.quat_to_rot(cap['q_link_opt']), cap['t_link_opt']

    p0 = field.extend_pose(core.pose_from_world_cam(core.quat_to_rot(cap['q']), cap['t']))
    print(f'{len(cap["frames"])} frames, {len(field.segs)} segments')
    sink = DebugSink(med, field, K, D, args.delta, debug_dir, args.show,
                     os.path.splitext(os.path.basename(args.capture))[0])
    p, stage = core.calibrate(gray, field, p0, K, D, args.bands, args.step,
                              args.grad_thresh, args.contrast_thresh, args.delta, callback=sink)
    if args.show:
        cv2.destroyAllWindows()

    print('\nPer segment (last band; RMS/mean over inliers, + = image edge outside the model line):')
    print('\n'.join(core.format_segment_table(core.segment_stats(field, stage['diag']))))

    tf0 = core.cam_tf_from_pose(p0, R_lo, t_lo)
    tf1 = core.cam_tf_from_pose(p, R_lo, t_lo)
    cov_c = core.camera_center_cov(p, stage['cov'])
    rot_std = np.degrees(np.sqrt(np.diag(stage['cov'])[:3]))
    names = ['x', 'y', 'z', 'roll', 'pitch', 'yaw']
    print('\ncam_tf (map -> camera_link, static_transform_publisher convention):')
    print(f'  {"":6s} {"initial":>10s} {"calibrated":>11s} {"diff":>10s}')
    for i, name in enumerate(names):
        if i < 3:
            print(f'  {name:6s} {tf0[i]:10.4f} {tf1[i]:11.4f} {(tf1[i] - tf0[i]) * 1000:+8.1f} mm')
        else:
            print(f'  {name:6s} {tf0[i]:10.4f} {tf1[i]:11.4f} {math.degrees(tf1[i] - tf0[i]):+8.3f} deg')
    dmm, ddeg = core.pose_delta(p0, p)
    print(f'  camera moved {dmm:.1f} mm, rotation change {ddeg:.3f} deg')
    print(f'  statistical 1-sigma: camera center {np.round(np.sqrt(np.diag(cov_c)) * 1000, 2)} mm, '
          f'rotation {np.round(rot_std, 3)} deg (edge noise only, excludes table size / lip bias)')
    if field.n_dx:
        print(f'  table x offsets (tables 1..): {np.round(core.get_dx(p) * 1000, 1)} mm')

    os.makedirs(args.out_dir, exist_ok=True)
    out_yaml = os.path.join(args.out_dir, 'cam_tf.yaml')
    with open(out_yaml, 'w') as f:
        yaml.safe_dump({'source': os.path.abspath(args.capture),
                        'cam_tf': {n: round(float(v), 5) for n, v in zip(names, tf1)},
                        'table_dx': [round(float(v), 5) for v in core.get_dx(p)]}, f, sort_keys=False)
    print(f'\nwrote {out_yaml} and debug images in {debug_dir}')
    print('launch args: ' + ' '.join(f'cam_tf.{n}:={v:.5f}' for n, v in zip(names, tf1)))
    print()
    print('\n'.join(core.tag_check(gray_u8, [('initial', p0), ('calibrated', p)], K, D,
                                   args.tag_id, args.tag_size, args.tag_height)))


if __name__ == '__main__':
    main()
