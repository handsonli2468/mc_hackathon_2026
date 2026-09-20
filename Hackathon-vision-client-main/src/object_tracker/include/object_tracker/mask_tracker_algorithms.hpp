// ROS-independent helpers for mask_tracker_node: depth sampling, mask cleaning, similarity transforms,
// polygons and ROI. Kept header-only so they can later be shared or unit tested without ROS.
#pragma once

#include <opencv2/opencv.hpp>
#include <algorithm>
#include <cmath>
#include <limits>
#include <optional>
#include <string>
#include <vector>

namespace object_tracker {

constexpr int kOrbEdgeThreshold = 31;

inline bool is_depth_mm(const std::string &encoding) {
    return encoding == "16UC1" || encoding == "mono16";
}

// Depth in meters at (x, y): 16UC1 / mono16 in millimeters, otherwise 32FC1 in meters
inline double depth_value_m(const cv::Mat &depth, bool is_mm, int x, int y) {
    return is_mm ? depth.at<uint16_t>(y, x) * 0.001 : static_cast<double>(depth.at<float>(y, x));
}

// Median of in-range depth values around (u, v), plus why the window may have none
// (same classification as orb_tracker_node::sample_depth)
struct DepthSample {
    std::optional<double> median_m;
    int n_valid = 0;  // within [min_m, max_m]
    int n_near = 0;   // > 0 but < min_m
    int n_far = 0;    // finite and > max_m
};

inline DepthSample sample_depth_window(const cv::Mat &depth, bool is_mm, int u, int v, int window,
                                       double min_m, double max_m) {
    DepthSample sample;
    if (u < 0 || v < 0 || u >= depth.cols || v >= depth.rows) {
        return sample;
    }
    const int half = window / 2;
    const int x0 = std::max(0, u - half), x1 = std::min(depth.cols - 1, u + half);
    const int y0 = std::max(0, v - half), y1 = std::min(depth.rows - 1, v + half);
    double values[64];
    int n = 0;
    for (int y = y0; y <= y1; ++y) {
        for (int x = x0; x <= x1; ++x) {
            const double d = depth_value_m(depth, is_mm, x, y);
            // 0 (no data) and NaN are not counted
            if (!std::isfinite(d) || d <= 0.0) {
                continue;
            }
            if (d < min_m) {
                ++sample.n_near;
            } else if (d > max_m) {
                ++sample.n_far;
            } else if (n < 64) {
                values[n++] = d;
            }
        }
    }
    sample.n_valid = n;
    if (n > 0) {
        std::nth_element(values, values + n / 2, values + n);
        sample.median_m = values[n / 2];
    }
    return sample;
}

// Median of every measured (finite, > 0) depth around (u, v), in or out of range; none when nothing was measured.
// Used for outlier rejection, where far background beyond max_m must also count as "different depth".
inline std::optional<double> measured_depth_window(const cv::Mat &depth, bool is_mm, int u, int v, int window) {
    if (u < 0 || v < 0 || u >= depth.cols || v >= depth.rows) {
        return std::nullopt;
    }
    const int half = window / 2;
    double values[64];
    int n = 0;
    for (int y = std::max(0, v - half); y <= std::min(depth.rows - 1, v + half); ++y) {
        for (int x = std::max(0, u - half); x <= std::min(depth.cols - 1, u + half); ++x) {
            const double d = depth_value_m(depth, is_mm, x, y);
            if (std::isfinite(d) && d > 0.0 && n < 64) {
                values[n++] = d;
            }
        }
    }
    if (n == 0) {
        return std::nullopt;
    }
    std::nth_element(values, values + n / 2, values + n);
    return values[n / 2];
}

inline double median_of(std::vector<double> values) {
    std::nth_element(values.begin(), values.begin() + values.size() / 2, values.end());
    return values[values.size() / 2];
}

// Mask after the depth gate and largest-region step
struct CleanedMask {
    cv::Mat mask;                // 0 / 255, empty when no region is left
    int pixels = 0;              // pixels of the largest region
    int depth_removed = 0;       // pixels dropped by the depth gate
    std::optional<double> d0;    // median in-range depth inside the input mask
};

// Drop pixels whose measured depth differs from the mask's median in-range depth by more than gate_m (usually
// background caught at the edges), then keep the largest 8-connected region. Without any in-range depth
// (e.g. too far) the depth step is skipped. Same rules as orb_tracker_node::clean_mask.
inline CleanedMask clean_mask(const cv::Mat &mask, const cv::Mat &depth, bool is_mm, double min_m, double max_m,
                              double gate_m) {
    CleanedMask result;
    cv::Mat cleaned = (mask != 0);

    std::vector<double> in_range;
    for (int y = 0; y < cleaned.rows; ++y) {
        const uchar *row = cleaned.ptr<uchar>(y);
        for (int x = 0; x < cleaned.cols; ++x) {
            if (!row[x]) {
                continue;
            }
            const double d = depth_value_m(depth, is_mm, x, y);
            if (std::isfinite(d) && d >= min_m && d <= max_m) {
                in_range.push_back(d);
            }
        }
    }
    if (!in_range.empty()) {
        result.d0 = median_of(in_range);
        for (int y = 0; y < cleaned.rows; ++y) {
            uchar *row = cleaned.ptr<uchar>(y);
            for (int x = 0; x < cleaned.cols; ++x) {
                if (!row[x]) {
                    continue;
                }
                // any measured depth counts, so background beyond max_m is removed too
                const double d = depth_value_m(depth, is_mm, x, y);
                if (std::isfinite(d) && d > 0.0 && std::abs(d - *result.d0) > gate_m) {
                    row[x] = 0;
                    ++result.depth_removed;
                }
            }
        }
    }

    cv::Mat labels, stats, centroids;
    const int n_labels = cv::connectedComponentsWithStats(cleaned, labels, stats, centroids, 8, CV_32S);
    int best_label = 0;
    for (int i = 1; i < n_labels; ++i) {
        const int area = stats.at<int>(i, cv::CC_STAT_AREA);
        if (area > result.pixels) {
            result.pixels = area;
            best_label = i;
        }
    }
    if (best_label > 0) {
        result.mask = (labels == best_label);
    }
    return result;
}

inline cv::Mat erode_mask(const cv::Mat &mask, int erode_px) {
    if (erode_px <= 0) {
        return mask;
    }
    cv::Mat eroded;
    const int k = 2 * erode_px + 1;
    cv::erode(mask, eroded, cv::getStructuringElement(cv::MORPH_ELLIPSE, cv::Size(k, k)));
    return eroded;
}

// Outer contour of the largest blob, simplified to about 2 px
inline std::vector<cv::Point2f> mask_polygon(const cv::Mat &mask) {
    std::vector<std::vector<cv::Point>> contours;
    cv::findContours(mask.clone(), contours, cv::RETR_EXTERNAL, cv::CHAIN_APPROX_SIMPLE);
    if (contours.empty()) {
        return {};
    }
    const auto largest = std::max_element(contours.begin(), contours.end(), [](const auto &a, const auto &b) {
        return cv::contourArea(a) < cv::contourArea(b);
    });
    std::vector<cv::Point> approx;
    cv::approxPolyDP(*largest, approx, 2.0, true);
    return std::vector<cv::Point2f>(approx.begin(), approx.end());
}

// Geometric center of the mask: the centroid, or the nearest mask pixel when the centroid falls outside it
// (e.g. a C shape). Independent of where the texture is, unlike a median of feature points.
// Same rule as orb_tracker_node::build_mask_model.
inline cv::Point2f mask_center(const cv::Mat &mask) {
    const cv::Moments m = cv::moments(mask, true);
    if (m.m00 <= 0.0) {
        return cv::Point2f(0.0f, 0.0f);
    }
    const cv::Point2f centroid(static_cast<float>(m.m10 / m.m00), static_cast<float>(m.m01 / m.m00));
    const cv::Point px(static_cast<int>(std::lround(centroid.x)), static_cast<int>(std::lround(centroid.y)));
    if (cv::Rect(cv::Point(0, 0), mask.size()).contains(px) && mask.at<uchar>(px) != 0) {
        return centroid;
    }
    std::vector<cv::Point> pixels;
    cv::findNonZero(mask, pixels);
    cv::Point2f nearest = centroid;
    double best_dist = std::numeric_limits<double>::max();
    for (const auto &p : pixels) {
        const double dist = cv::norm(cv::Point2f(p) - centroid);
        if (dist < best_dist) {
            best_dist = dist;
            nearest = cv::Point2f(p);
        }
    }
    return nearest;
}

// Scale of a 2x3 similarity [s cos, -s sin, tx; s sin, s cos, ty]
inline double similarity_scale(const cv::Mat &S) {
    return std::hypot(S.at<double>(0, 0), S.at<double>(1, 0));
}

inline std::vector<cv::Point2f> apply_transform(const std::vector<cv::Point2f> &points, const cv::Mat &S) {
    std::vector<cv::Point2f> out;
    if (!points.empty()) {
        cv::transform(points, out, S);
    }
    return out;
}

inline double mean_transfer_error(const std::vector<cv::Point2f> &ref, const std::vector<cv::Point2f> &cur,
                                  const cv::Mat &S) {
    if (ref.empty()) {
        return 0.0;
    }
    const std::vector<cv::Point2f> projected = apply_transform(ref, S);
    double sum = 0.0;
    for (size_t i = 0; i < projected.size(); ++i) {
        sum += cv::norm(projected[i] - cur[i]);
    }
    return sum / projected.size();
}

inline std::vector<cv::Point> to_int_polygon(const std::vector<cv::Point2f> &polygon) {
    std::vector<cv::Point> out;
    out.reserve(polygon.size());
    for (const auto &p : polygon) {
        out.emplace_back(static_cast<int>(std::lround(p.x)), static_cast<int>(std::lround(p.y)));
    }
    return out;
}

// Search window around a region: bounding rect grown on each side by max(size * expand_ratio, min_margin),
// at least min_size wide / tall, clamped to the image
inline std::optional<cv::Rect> expand_roi(const std::vector<cv::Point2f> &region, const cv::Size &image_size,
                                          double expand_ratio, int min_size) {
    if (region.size() < 3) {
        return std::nullopt;
    }
    float min_x = region[0].x, max_x = region[0].x, min_y = region[0].y, max_y = region[0].y;
    for (const auto &p : region) {
        min_x = std::min(min_x, p.x);
        max_x = std::max(max_x, p.x);
        min_y = std::min(min_y, p.y);
        max_y = std::max(max_y, p.y);
    }
    const float min_margin = static_cast<float>(kOrbEdgeThreshold + 8);
    const float margin_x = std::max((max_x - min_x) * static_cast<float>(expand_ratio), min_margin);
    const float margin_y = std::max((max_y - min_y) * static_cast<float>(expand_ratio), min_margin);
    float x0 = min_x - margin_x, x1 = max_x + margin_x;
    float y0 = min_y - margin_y, y1 = max_y + margin_y;
    const float size = static_cast<float>(min_size);
    if (x1 - x0 < size) {
        const float cx = 0.5f * (x0 + x1);
        x0 = cx - 0.5f * size;
        x1 = cx + 0.5f * size;
    }
    if (y1 - y0 < size) {
        const float cy = 0.5f * (y0 + y1);
        y0 = cy - 0.5f * size;
        y1 = cy + 0.5f * size;
    }
    cv::Rect roi(cv::Point(static_cast<int>(std::floor(x0)), static_cast<int>(std::floor(y0))),
                 cv::Point(static_cast<int>(std::ceil(x1)), static_cast<int>(std::ceil(y1))));
    roi &= cv::Rect(cv::Point(0, 0), image_size);
    if (roi.width <= 0 || roi.height <= 0) {
        return std::nullopt;
    }
    return roi;
}

}  // namespace object_tracker
