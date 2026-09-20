#pragma once

#include <opencv2/calib3d.hpp>
#include <opencv2/core.hpp>
#include <Eigen/Dense>
#include <algorithm>
#include <cmath>
#include <vector>

namespace aruco_test {

// Camera model for projecting world points: world -> camera pose plus intrinsics/distortion
struct PlaneLmCamera {
    cv::Mat K;
    cv::Mat D;
    cv::Mat rvec_cw;
    cv::Mat tvec_cw;
};

// Reprojection residuals (pixels) of the marker corners for tag pose p = (x, y, yaw)
// on the level plane z = plane_z. marker_pts are the corners in the marker frame
// (tl, tr, br, bl, z up); img_corners are the raw (distorted) detections in the same order.
inline void plane_residual(const Eigen::Vector3d &p, const std::vector<cv::Point2f> &img_corners,
                           const std::vector<cv::Point2d> &marker_pts, double plane_z,
                           const PlaneLmCamera &cam, Eigen::Matrix<double, 8, 1> &r) {
    const double c = std::cos(p(2)), s = std::sin(p(2));
    std::vector<cv::Point3d> obj(4);
    for (int i = 0; i < 4; i++) {
        const cv::Point2d &m = marker_pts[i];
        obj[i] = cv::Point3d(p(0) + c * m.x - s * m.y, p(1) + s * m.x + c * m.y, plane_z);
    }
    std::vector<cv::Point2d> proj;
    cv::projectPoints(obj, cam.rvec_cw, cam.tvec_cw, cam.K, cam.D, proj);
    for (int i = 0; i < 4; i++) {
        r(2 * i) = proj[i].x - img_corners[i].x;
        r(2 * i + 1) = proj[i].y - img_corners[i].y;
    }
}

// 3-DoF Levenberg-Marquardt refinement of (x, y, yaw) with the tag constrained to the level
// plane z = plane_z. pose is the initial guess on input and the result on output.
// Returns the corner reprojection RMS (pixels).
inline double refine_plane_lm(const std::vector<cv::Point2f> &img_corners,
                              const std::vector<cv::Point2d> &marker_pts, double plane_z,
                              const PlaneLmCamera &cam, int max_iter, double pose[3]) {
    Eigen::Vector3d p(pose[0], pose[1], pose[2]);
    Eigen::Matrix<double, 8, 1> r, r_try;
    plane_residual(p, img_corners, marker_pts, plane_z, cam, r);
    double cost = r.squaredNorm();
    double lambda = 1e-3;
    const double eps[3] = {1e-5, 1e-5, 1e-6};

    for (int it = 0; it < max_iter; it++) {
        // Forward-difference Jacobian
        Eigen::Matrix<double, 8, 3> J;
        for (int j = 0; j < 3; j++) {
            Eigen::Vector3d p_eps = p;
            p_eps(j) += eps[j];
            plane_residual(p_eps, img_corners, marker_pts, plane_z, cam, r_try);
            J.col(j) = (r_try - r) / eps[j];
        }
        const Eigen::Matrix3d A = J.transpose() * J;
        const Eigen::Vector3d g = J.transpose() * r;

        bool improved = false;
        Eigen::Vector3d step = Eigen::Vector3d::Zero();
        while (lambda < 1e6) {
            Eigen::Matrix3d A_damped = A;
            A_damped.diagonal() *= (1.0 + lambda);
            step = A_damped.ldlt().solve(-g);
            plane_residual(p + step, img_corners, marker_pts, plane_z, cam, r_try);
            const double cost_try = r_try.squaredNorm();
            if (cost_try < cost) {
                p += step;
                r = r_try;
                cost = cost_try;
                lambda = std::max(lambda / 10.0, 1e-9);
                improved = true;
                break;
            }
            lambda *= 10.0;
        }
        if (!improved || step.norm() < 1e-9) {
            break;
        }
    }
    pose[0] = p(0);
    pose[1] = p(1);
    pose[2] = std::atan2(std::sin(p(2)), std::cos(p(2)));
    return std::sqrt(cost / 4.0);
}

}  // namespace aruco_test
