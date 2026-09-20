"""Synthetic table scene (color + depth) and self tests for the edge calibration."""
import cv2
import numpy as np

from . import core

K = np.array([[643.94, 0, 645.63], [0, 642.97, 368.15], [0, 0, 1.0]])
D = np.array([-0.05697, 0.06652, -0.000459, 0.001326, -0.02179])
T_LO = np.array([0.0, -0.059, 0.0])
Q_LO = np.array([-0.5, 0.5, -0.5, 0.5])
CAM_TF = (0.91, 1.18, 1.3, 0.0, 1.178, -1.5708)
# Camera placements for the no-prior (depth initialization) test: x, y, z, roll, pitch, yaw
AUTO_CAM_TFS = [CAM_TF, (0.70, 1.25, 1.35, 0.03, 1.10, -1.45), (1.00, 1.10, 1.25, -0.02, 1.25, -1.65)]
FLOOR_Z = -0.72


def true_table_dx(field):
    """Per-table x offsets used by the synthetic scenes (only when the model estimates them)."""
    dx = np.zeros(field.count)
    if field.fit_dx:
        dx[1:] = 0.012 * np.arange(1, field.count)
    return dx


def _table_masks(field, X, Y, table_dx, rad=0.04):
    L, d = field.L, field.d
    tables = np.zeros(X.shape, dtype=bool)
    for i, off in enumerate(table_dx):
        x0, y0, x1, y1 = off, i * d, L + off, (i + 1) * d
        cx = np.clip(X, x0 + rad, x1 - rad)
        cy = np.clip(Y, y0 + rad, y1 - rad)
        tables |= (X - cx) ** 2 + (Y - cy) ** 2 <= rad ** 2
    seams = np.zeros(X.shape, dtype=bool)
    for i in range(1, field.count):
        seams |= np.abs(Y - i * d) < 0.002
    return tables, tables & seams


def render_scene(field, p, table_dx, w=1280, h=720, ss=2, seed=0):
    """Gray image of the tables on a wood-like floor, with a robot at the origin corner and a
    dark occluder over the right edge."""
    rng = np.random.default_rng(seed)
    R_wc, t_wc = core.world_cam_from_pose(p)
    uu, vv = np.meshgrid((np.arange(w * ss) + 0.5) / ss - 0.5, (np.arange(h * ss) + 0.5) / ss - 0.5)
    pix = np.column_stack([uu.ravel(), vv.ravel()])
    n = cv2.undistortPoints(pix.reshape(-1, 1, 2), K, D).reshape(-1, 2)
    rays = (R_wc @ np.column_stack([n, np.ones(len(n))]).T).T
    s = -t_wc[2] / np.minimum(rays[:, 2], -1e-6)
    X = t_wc[0] + s * rays[:, 0]
    Y = t_wc[1] + s * rays[:, 1]
    img = 105 + 18 * np.sin(X * 35 + 2 * np.sin(Y * 5)) + 8 * np.sin(Y * 90)
    tables, seams = _table_masks(field, X, Y, table_dx)
    img[tables] = 212
    img[seams] = 85
    img[(X - 0.08) ** 2 + (Y - 0.02) ** 2 < 0.06 ** 2] = 240
    img = cv2.resize(img.reshape(h * ss, w * ss).astype(np.float32), (w, h), interpolation=cv2.INTER_AREA)
    uv, _ = cv2.projectPoints(np.array([[0.0, 0.8 * field.W, 0.0]]).reshape(-1, 1, 3), p[:3], p[3:6], K, D)
    u0, v0 = uv.ravel().astype(int)
    cv2.rectangle(img, (u0 - 60, v0 - 70), (u0 + 150, v0 + 70), 30, -1)
    img += rng.normal(0, 3, img.shape)
    return np.clip(img, 0, 255).astype(np.uint8)


def render_depth(field, p, table_dx, w=848, h=480, scale_bias=0.015, noise_m=0.002, seed=0):
    """Depth (m) seen by a pinhole depth camera co-located with the color camera (R_cd = I, t_cd = 0).
    Tables at z = 0, floor at FLOOR_Z, a laptop-sized box on the table, 1.5% depth scale bias."""
    rng = np.random.default_rng(seed)
    Kd = np.array([[426.0, 0, w / 2], [0, 426.0, h / 2], [0, 0, 1.0]])
    R_wc, t_wc = core.world_cam_from_pose(p)
    uu, vv = np.meshgrid(np.arange(w), np.arange(h))
    rays_c = np.column_stack([(uu.ravel() - Kd[0, 2]) / Kd[0, 0], (vv.ravel() - Kd[1, 2]) / Kd[1, 1],
                              np.ones(w * h)])
    rays = (R_wc @ rays_c.T).T
    down = np.minimum(rays[:, 2], -1e-6)
    s_tab = -t_wc[2] / down
    X, Y = t_wc[0] + s_tab * rays[:, 0], t_wc[1] + s_tab * rays[:, 1]
    tables, _ = _table_masks(field, X, Y, table_dx)
    s = np.where(tables, s_tab, (FLOOR_Z - t_wc[2]) / down)
    # Box (laptop) standing 3 cm above the table, near the right side
    s_box = (0.03 - t_wc[2]) / down
    Xb, Yb = t_wc[0] + s_box * rays[:, 0], t_wc[1] + s_box * rays[:, 1]
    box = (np.abs(Xb - 0.45) < 0.15) & (np.abs(Yb - 0.5 * field.W) < 0.12)
    s = np.where(box, s_box, s)
    z = s * rays_c[:, 2] * (1 + scale_bias) + rng.normal(0, noise_m, s.shape)
    z[(z <= 0) | (z > 6.0)] = 0.0  # invalid, like RealSense
    return z.reshape(h, w).astype(np.float32), Kd


def _errors(p, R_wc, t_wc):
    R_est, t_est = core.world_cam_from_pose(p)
    return core.rot_angle_deg(R_est.T @ R_wc), np.linalg.norm(t_est - t_wc) * 1000


def selftest(field, bands, step, blur, grad_thresh, contrast_thresh, delta, trials=3,
             callback=None, log=print):
    """Converge from 3 deg / 10 cm off. Pass = < 0.2 deg and < 5 mm. Returns (ok, gray, last stage)."""
    p_true = core.pose_from_cam_tf(CAM_TF, core.quat_to_rot(Q_LO), T_LO)
    R_wc, t_wc = core.world_cam_from_pose(p_true)
    table_dx = true_table_dx(field)
    gray = render_scene(field, p_true, table_dx)
    gray_f = cv2.GaussianBlur(gray.astype(np.float32), (0, 0), blur)
    rng = np.random.default_rng(1)
    ok_all = True
    stage = None
    for trial in range(trials):
        dt = rng.normal(size=3)
        p0 = field.extend_pose(core.pose_from_world_cam(core.axis_angle_rot(rng.normal(size=3), 3.0) @ R_wc,
                                                        t_wc + 0.10 * dt / np.linalg.norm(dt)))
        log(f'trial {trial}: init off by 3.00 deg, 100.0 mm')
        p, stage = core.calibrate(gray_f, field, p0, K, D, bands, step, grad_thresh, contrast_thresh,
                                  delta, callback=callback if trial == trials - 1 else None, log=log)
        e_rot, e_pos = _errors(p, R_wc, t_wc)
        msg = f'  result: rot err {e_rot:.3f} deg, pos err {e_pos:.2f} mm'
        if field.n_dx:
            msg += f', table dx {np.round(core.get_dx(p) * 1000, 2)} mm (true {table_dx[1:] * 1000})'
        ok = e_rot < 0.2 and e_pos < 5.0
        log(msg + ('  PASS' if ok else '  FAIL'))
        ok_all &= ok
    return ok_all, gray, stage


def selftest_auto(field, bands, step, blur, grad_thresh, contrast_thresh, delta, log=print):
    """No previous extrinsic: initial pose from the synthetic depth, then the normal calibration.
    Pass = the origin is picked correctly and the result is < 0.2 deg / < 5 mm."""
    R_lo = core.quat_to_rot(Q_LO)
    table_dx = true_table_dx(field)
    ok_all = True
    for i, cam_tf in enumerate(AUTO_CAM_TFS):
        p_true = core.pose_from_cam_tf(cam_tf, R_lo, T_LO)
        R_wc, t_wc = core.world_cam_from_pose(p_true)
        depth, Kd = render_depth(field, p_true, table_dx, seed=i)
        try:
            p0, info = core.init_from_depth(depth, Kd, np.eye(3), np.zeros(3), field)
        except RuntimeError as e:
            log(f'camera {i}: depth init FAILED: {e}')
            ok_all = False
            continue
        e_rot0, e_pos0 = _errors(p0, R_wc, t_wc)
        log(f'camera {i} {cam_tf}: depth init off by {e_rot0:.2f} deg, {e_pos0:.1f} mm '
            f'(table {info["measured_length"]:.3f} x {info["measured_width"]:.3f} m, h {info["height"]:.3f} m)')
        gray = render_scene(field, p_true, table_dx, seed=i)
        gray_f = cv2.GaussianBlur(gray.astype(np.float32), (0, 0), blur)
        p, _ = core.calibrate(gray_f, field, p0, K, D, bands, step, grad_thresh, contrast_thresh, delta,
                              log=None)
        e_rot, e_pos = _errors(p, R_wc, t_wc)
        ok = e_rot < 0.2 and e_pos < 5.0
        log(f'  result: rot err {e_rot:.3f} deg, pos err {e_pos:.2f} mm' + ('  PASS' if ok else '  FAIL'))
        ok_all &= ok
    return ok_all
