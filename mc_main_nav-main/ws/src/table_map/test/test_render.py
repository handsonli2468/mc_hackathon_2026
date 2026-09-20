from types import SimpleNamespace

import numpy as np
import pytest

from table_map.map_node import render, resample, validate_obstacles

RES = 0.005


def cell(x, y, origin=(0.0, 0.0)):
    return int((y - origin[1]) / RES), int((x - origin[0]) / RES)  # (row, col)


def test_empty_table_with_edge():
    g = render(240, 160, RES, (0.0, 0.0), None, 0.01, [])
    assert g.shape == (160, 240)
    assert (g[:2, :] == 100).all() and (g[-2:, :] == 100).all()
    assert (g[:, :2] == 100).all() and (g[:, -2:] == 100).all()
    assert (g[2:-2, 2:-2] == 0).all()


def test_rect_and_circle_land_in_the_right_cells():
    obs = validate_obstacles([
        {"type": "rect", "x": 0.30, "y": 0.20, "w": 0.10, "h": 0.05},
        {"type": "circle", "x": 0.80, "y": 0.50, "r": 0.04},
    ])
    g = render(240, 160, RES, (0.0, 0.0), None, 0.0, obs)
    assert g[cell(0.35, 0.22)] == 100
    assert g[cell(0.29, 0.22)] == 0          # just left of the rect
    assert g[cell(0.35, 0.26)] == 0          # just above the rect
    assert g[cell(0.80, 0.50)] == 100
    assert g[cell(0.83, 0.50)] == 100        # inside radius
    assert g[cell(0.86, 0.50)] == 0          # outside radius
    # rect area ~ 20 x 10 cells
    assert 180 <= (g[:, :] == 100).sum() - (np.pi * (0.04 / RES) ** 2) <= 260


def test_origin_offset():
    obs = validate_obstacles([{"type": "circle", "x": 0.0, "y": 0.0, "r": 0.02}])
    g = render(100, 100, RES, (-0.25, -0.25), None, 0.0, obs)
    assert g[50, 50] == 100 and g[0, 0] == 0


def test_validate_rejects_bad_input():
    for bad in [
        {"type": "rect", "x": 0, "y": 0, "w": -1, "h": 1},
        {"type": "circle", "x": 0, "y": 0},
        {"type": "triangle", "x": 0, "y": 0},
        {"type": "circle", "x": "nan", "y": 0, "r": 1},
        "rect",
    ]:
        with pytest.raises(ValueError):
            validate_obstacles([bad])
    with pytest.raises(ValueError):
        validate_obstacles({"type": "rect"})


def test_resample_camera_map_to_5mm():
    # Camera map: 2 cm cells, 10 x 5 cells (20 x 10 cm), origin (0.1, 0.1),
    # occupied cell at col 3, row 2; unknown at col 0, row 0.
    data = np.zeros((5, 10), dtype=np.int16)
    data[2, 3] = 100
    data[0, 0] = -1
    msg = SimpleNamespace(
        info=SimpleNamespace(
            width=10, height=5, resolution=0.02,
            origin=SimpleNamespace(position=SimpleNamespace(x=0.1, y=0.1)),
        ),
        data=data.flatten().tolist(),
    )
    out = resample(msg, 40, 20, RES, (0.1, 0.1))
    assert out.shape == (20, 40)
    # occupied 2 cm cell -> 4 x 4 of our cells
    assert (out[8:12, 12:16] == 100).all()
    assert out[8, 11] == 0 and out[12, 12] == 0
    assert (out[0:4, 0:4] == -1).all()
    assert (out == 100).sum() == 16
