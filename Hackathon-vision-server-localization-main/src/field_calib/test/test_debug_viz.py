"""Debug overlay tests on a synthetic table scene (no ROS needed).

Run: python3 -m pytest src/field_calib/test
"""
import os
import sys

import cv2
import numpy as np
import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from field_calib import core, debug_viz, synthetic  # noqa: E402

FIELD_YAML = os.path.join(os.path.dirname(__file__), '..', 'config', 'field.yaml')


@pytest.fixture(scope='module')
def scene():
    with open(FIELD_YAML) as f:
        field = core.Field(yaml.safe_load(f))
    p = field.extend_pose(core.pose_from_cam_tf(synthetic.CAM_TF, core.quat_to_rot(synthetic.Q_LO),
                                                synthetic.T_LO))
    gray = synthetic.render_scene(field, p, synthetic.true_table_dx(field))
    img = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    blurred = cv2.GaussianBlur(gray.astype(np.float32), (0, 0), 1.0)
    live = core.evaluate(blurred, field, p, synthetic.K, synthetic.D, 12, 0.01, 6.0, 20.0, 1.5)
    p0 = p.copy()
    p0[3] += 0.03  # start 3 cm off so p_before != p_after
    _, calib = core.calibrate(blurred, field, p0, synthetic.K, synthetic.D, [40, 12], 0.01, 6.0, 20.0, 1.5,
                              log=None)
    return field, img, {'live': live, 'calib': calib}


@pytest.mark.parametrize('kind', ['live', 'calib'])
@pytest.mark.parametrize('title', ['', 'calib PASS'])
def test_compact_same_size_as_camera(scene, kind, title):
    field, img, stages = scene
    out = debug_viz.render_overlay_compact(img, field, synthetic.K, synthetic.D, stages[kind], title)
    assert out.shape == img.shape
    assert out.dtype == np.uint8
    assert not np.array_equal(out, img)  # something was drawn


@pytest.mark.parametrize('kind', ['live', 'calib'])
def test_full_overlay_keeps_panel_and_shares_scene(scene, kind):
    field, img, stages = scene
    h, w = img.shape[:2]
    full = debug_viz.render_overlay(img, field, synthetic.K, synthetic.D, stages[kind], 1.5, kind)
    compact = debug_viz.render_overlay_compact(img, field, synthetic.K, synthetic.D, stages[kind])
    assert full.shape == (820, w + 460, 3)
    # Below the title lines both draw exactly the same scene on the camera image
    assert np.array_equal(full[40:h, :w], compact[40:])


def test_compact_jpeg_round_trip(scene):
    field, img, stages = scene
    out = debug_viz.render_overlay_compact(img, field, synthetic.K, synthetic.D, stages['live'])
    ok, buf = cv2.imencode('.jpg', out, [cv2.IMWRITE_JPEG_QUALITY, 80])
    assert ok and buf.size > 0
    assert cv2.imdecode(buf, cv2.IMREAD_COLOR).shape == img.shape
