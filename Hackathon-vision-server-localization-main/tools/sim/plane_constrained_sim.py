#!/usr/bin/env python3
"""Monte Carlo comparison of overhead-camera tag localization methods.

Methods (all output world x, y, yaw of the tag center):
  homo      : replica of homography_duck_node (ground H, center=(tl+br)/2, similar-triangle to z=h)
  homo4     : 4 undistorted corners ray-cast onto plane z=h, then 2D rigid (Procrustes) fit
  pnp       : replica of pnp_duck_node (solvePnPGeneric IPPE_SQUARE, pick normal closest to world +Z)
  plane_lm  : 3-DoF (x, y, yaw) LM on image reprojection error with z=h and level tag enforced

Scenarios inject pixel noise plus one systematic error at a time (tag tilt, h error,
marker size error, camera pitch error) to see which method is sensitive to what.
"""
import argparse
import math

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# Camera model (live values from /camera/camera/color/camera_info, 848x480)
# ---------------------------------------------------------------------------
W, H_IMG = 848, 480
K = np.array([[426.4960021972656, 0.0, 427.72723388671875],
              [0.0, 425.85443115234375, 245.39883422851562],
              [0.0, 0.0, 1.0]])
D = np.array([-0.056969910860061646, 0.06651826202869415, -0.0004588848096318543,
              0.001326069817878306, -0.021789632737636566])

# map -> camera_color_optical_frame from `tf2_echo` with rs_launch.py defaults
# (cam_tf x=0.91 y=1.18 z=1.45 roll=0 pitch=1.178 yaw=-1.5708)
CAM_T = np.array([0.851, 1.180, 1.450])
CAM_Q_XYZW = np.array([0.001, 0.980, -0.197, 0.002])


def quat_to_rot(q_xyzw):
    x, y, z, w = q_xyzw / np.linalg.norm(q_xyzw)
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def rot_x(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])


def rot_y(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def rot_z(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


def wrap(a):
    return (a + np.pi) % (2 * np.pi) - np.pi


def marker_obj_points(size):
    # Same order as pnp_duck_node / detectMarkers: tl, tr, br, bl
    h = size / 2.0
    return np.array([[-h, h, 0], [h, h, 0], [h, -h, 0], [-h, -h, 0]], dtype=np.float64)


class Camera:
    """World <-> image for a camera pose given as R_wc (world_from_cam), t_wc."""

    def __init__(self, R_wc, t_wc):
        self.R_wc = R_wc
        self.t_wc = t_wc
        self.R_cw = R_wc.T
        self.t_cw = -R_wc.T @ t_wc
        self.rvec_cw, _ = cv2.Rodrigues(self.R_cw)

    def project(self, P_world):
        img, _ = cv2.projectPoints(P_world.reshape(-1, 1, 3), self.rvec_cw, self.t_cw, K, D)
        return img.reshape(-1, 2)

    def in_front(self, P_world):
        return np.all((self.R_cw @ P_world.T + self.t_cw[:, None])[2] > 0.1)


# ---------------------------------------------------------------------------
# Methods. `cam` is the camera the method BELIEVES in (may differ from truth).
# ---------------------------------------------------------------------------
def undistort(px):
    return cv2.undistortPoints(px.reshape(-1, 1, 2), K, D, P=K).reshape(-1, 2)


def ray_plane(cam, px_undist, z):
    """Intersect rays through undistorted pixels with world plane Z=z."""
    rays_c = np.linalg.inv(K) @ np.vstack([px_undist.T, np.ones(len(px_undist))])
    rays_w = cam.R_wc @ rays_c
    s = (z - cam.t_wc[2]) / rays_w[2]
    return (cam.t_wc[:, None] + rays_w * s).T


def method_homo(cam, px, h):
    """Replica of homography_duck_node."""
    und = undistort(px)
    tl, tr, br, bl = und
    Rt = np.column_stack([cam.R_cw[:, 0], cam.R_cw[:, 1], cam.t_cw])
    H = np.linalg.inv(K @ Rt)

    def to_ground(p):
        v = H @ np.array([p[0], p[1], 1.0])
        return v[:2] / v[2]

    g = to_ground((tl + br) * 0.5)
    gl = to_ground((bl + tl) * 0.5)
    gr = to_ground((br + tr) * 0.5)
    cx, cy, cz = cam.t_wc
    t = (cz - h) / cz
    x = cx + t * (g[0] - cx)
    y = cy + t * (g[1] - cy)
    yaw = math.atan2(gr[1] - gl[1], gr[0] - gl[0])
    return x, y, yaw


def rigid_fit_2d(src, dst):
    """Least-squares R(yaw), t with dst ~ R src + t (no scale)."""
    ms, md = src.mean(0), dst.mean(0)
    A, B = src - ms, dst - md
    yaw = math.atan2(np.sum(A[:, 0] * B[:, 1] - A[:, 1] * B[:, 0]),
                     np.sum(A[:, 0] * B[:, 0] + A[:, 1] * B[:, 1]))
    R = rot_z(yaw)[:2, :2]
    t = md - R @ ms
    return t[0], t[1], yaw


def method_homo4(cam, px, h, size):
    P = ray_plane(cam, undistort(px), h)
    return rigid_fit_2d(marker_obj_points(size)[:, :2], P[:, :2])


def method_pnp(cam, px, size):
    """Replica of pnp_duck_node."""
    n, rvecs, tvecs, _ = cv2.solvePnPGeneric(
        marker_obj_points(size), px.reshape(-1, 1, 2), K, D, flags=cv2.SOLVEPNP_IPPE_SQUARE)
    best, best_up, out = -1, -2.0, None
    for k in range(n):
        R_cm, _ = cv2.Rodrigues(rvecs[k])
        R_wm = cam.R_wc @ R_cm
        t_wm = cam.R_wc @ tvecs[k].ravel() + cam.t_wc
        if R_wm[2, 2] > best_up:
            best_up, best = R_wm[2, 2], k
            out = (t_wm[0], t_wm[1], math.atan2(R_wm[1, 0], R_wm[0, 0]))
    return out


def plane_corners_world(x, y, yaw, h, size):
    return (rot_z(yaw) @ marker_obj_points(size).T).T + np.array([x, y, h])


def method_plane_lm(cam, px, h, size, iters=15):
    """Minimize image reprojection error over (x, y, yaw) with tag on level plane z=h."""
    p = np.array(method_homo4(cam, px, h, size), dtype=np.float64)
    lam = 1e-3

    def residual(q):
        return (cam.project(plane_corners_world(q[0], q[1], q[2], h, size)) - px).ravel()

    r = residual(p)
    cost = r @ r
    for _ in range(iters):
        J = np.empty((8, 3))
        for j, eps in enumerate((1e-5, 1e-5, 1e-6)):
            dp = np.zeros(3)
            dp[j] = eps
            J[:, j] = (residual(p + dp) - r) / eps
        A = J.T @ J
        g = J.T @ r
        while True:
            step = np.linalg.solve(A + lam * np.diag(np.diag(A)), -g)
            p_new = p + step
            r_new = residual(p_new)
            cost_new = r_new @ r_new
            if cost_new < cost:
                p, r, cost = p_new, r_new, cost_new
                lam = max(lam / 10, 1e-9)
                break
            lam *= 10
            if lam > 1e6:
                break
        if np.linalg.norm(step) < 1e-9 or lam > 1e6:
            break
    return p[0], p[1], p[2]


METHODS = ['homo', 'homo4', 'pnp', 'plane_lm']


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------
def sample_poses(cam, h, size, n, rng, margin=20):
    """Tag poses on plane z=h whose 4 corners are all inside the image."""
    corners_img = np.array([[margin, margin], [W - margin, margin],
                            [W - margin, H_IMG - margin], [margin, H_IMG - margin]], dtype=np.float64)
    footprint = ray_plane(cam, corners_img, h)[:, :2]
    lo, hi = footprint.min(0), footprint.max(0)
    poses = []
    while len(poses) < n:
        x, y = rng.uniform(lo, hi)
        yaw = rng.uniform(-np.pi, np.pi)
        Pw = plane_corners_world(x, y, yaw, h, size)
        if not cam.in_front(Pw):
            continue
        img = cam.project(Pw)
        if np.all((img[:, 0] > margin) & (img[:, 0] < W - margin) &
                  (img[:, 1] > margin) & (img[:, 1] < H_IMG - margin)):
            poses.append((x, y, yaw))
    return np.array(poses)


def run_scenario(name, poses, cam_nominal, h, size, sigma, rng,
                 tilt_deg=0.0, dh=0.0, dsize=0.0, cam_pitch_err_deg=0.0):
    # Truth may differ from what the methods assume
    if cam_pitch_err_deg:
        R_true = cam_nominal.R_wc @ rot_x(math.radians(cam_pitch_err_deg))  # tilt about optical x
        cam_true = Camera(R_true, cam_nominal.t_wc)
    else:
        cam_true = cam_nominal
    h_true, size_true = h + dh, size + dsize

    errs = {m: [] for m in METHODS}
    fails = {m: 0 for m in METHODS}
    for x, y, yaw in poses:
        R_tag = rot_z(yaw)
        if tilt_deg:
            axis = rng.uniform(-np.pi, np.pi)  # random tilt direction
            R_tag = rot_z(axis) @ rot_x(math.radians(tilt_deg)) @ rot_z(-axis) @ R_tag
        Pw = (R_tag @ marker_obj_points(size_true).T).T + np.array([x, y, h_true])
        px = cam_true.project(Pw) + rng.normal(0.0, sigma, (4, 2))

        results = {
            'homo': lambda: method_homo(cam_nominal, px, h),
            'homo4': lambda: method_homo4(cam_nominal, px, h, size),
            'pnp': lambda: method_pnp(cam_nominal, px, size),
            'plane_lm': lambda: method_plane_lm(cam_nominal, px, h, size),
        }
        for m, fn in results.items():
            try:
                ex, ey, eyaw = fn()
            except cv2.error:
                fails[m] += 1
                continue
            errs[m].append((math.hypot(ex - x, ey - y), abs(wrap(eyaw - yaw))))

    print(f'\n### {name}')
    print('| method | xy RMS (mm) | xy p95 (mm) | xy max (mm) | yaw RMS (deg) | yaw p95 (deg) | fail |')
    print('|---|---:|---:|---:|---:|---:|---:|')
    for m in METHODS:
        e = np.array(errs[m])
        if len(e) == 0:
            print(f'| {m} | - | - | - | - | - | {fails[m]} |')
            continue
        dxy, dyaw = e[:, 0] * 1000, np.degrees(e[:, 1])
        print(f'| {m} | {np.sqrt(np.mean(dxy**2)):.1f} | {np.percentile(dxy, 95):.1f} | {dxy.max():.1f} '
              f'| {np.sqrt(np.mean(dyaw**2)):.2f} | {np.percentile(dyaw, 95):.2f} | {fails[m]} |')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--h', type=float, default=0.2, help='tag height above map z=0 (m)')
    ap.add_argument('--size', type=float, default=0.1, help='tag black-border edge (m)')
    ap.add_argument('--n', type=int, default=1000, help='poses per scenario')
    ap.add_argument('--seed', type=int, default=0)
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    cam = Camera(quat_to_rot(CAM_Q_XYZW), CAM_T)
    poses = sample_poses(cam, args.h, args.size, args.n, rng)
    tag_px = [np.sqrt(cv2.contourArea(cam.project(plane_corners_world(*p, args.h, args.size)).astype(np.float32)))
              for p in poses]
    print(f'camera z={CAM_T[2]:.3f} m, tag h={args.h} m, size={args.size} m, n={args.n}')
    print(f'visible tag x range {poses[:, 0].min():.2f}..{poses[:, 0].max():.2f} m, '
          f'y range {poses[:, 1].min():.2f}..{poses[:, 1].max():.2f} m')
    print(f'tag apparent edge in image: {np.min(tag_px):.1f}..{np.max(tag_px):.1f} px (median {np.median(tag_px):.1f})')

    for sigma in (0.2, 0.5, 1.0):
        run_scenario(f'pixel noise only, sigma={sigma} px', poses, cam, args.h, args.size, sigma, rng)
    s = 0.5
    run_scenario(f'sigma={s} px + tag tilted 2 deg (robot not level)', poses, cam, args.h, args.size, s, rng, tilt_deg=2.0)
    run_scenario(f'sigma={s} px + true h is 1 cm higher than assumed', poses, cam, args.h, args.size, s, rng, dh=0.01)
    run_scenario(f'sigma={s} px + true marker size 2 mm larger than assumed', poses, cam, args.h, args.size, s, rng, dsize=0.002)
    run_scenario(f'sigma={s} px + camera pitch off by 1 deg', poses, cam, args.h, args.size, s, rng, cam_pitch_err_deg=1.0)


if __name__ == '__main__':
    main()
