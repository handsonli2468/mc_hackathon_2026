"""Debug views for the table-edge calibration (one set per stage / band).

overlay   : full image + search bands + accepted / rejected samples + corner zoom + stats panel
strips    : every segment straightened along the model line (x = along, y = normal, 4x zoom)
residuals : signed residual vs position along every segment (+ = detected edge is outside)
"""
import cv2
import numpy as np

from . import core

SEG_COLORS = [(255, 180, 0), (0, 200, 255), (255, 0, 200), (0, 255, 128),
              (128, 128, 255), (255, 255, 0), (200, 200, 200)]


def seg_color(j):
    return SEG_COLORS[j % len(SEG_COLORS)]


STATUS_COLORS = {
    core.ACCEPTED: (0, 255, 0),
    core.WEAK_GRADIENT: (0, 165, 255),
    core.LOW_CONTRAST: (255, 0, 255),
    core.AT_BAND_EDGE: (255, 128, 0),
    core.OUTLIER: (0, 0, 255),
}
FONT = cv2.FONT_HERSHEY_SIMPLEX
ZOOM = 4


def _text(img, s, org, scale=0.45, color=(255, 255, 255), thick=1):
    cv2.putText(img, s, org, FONT, scale, (0, 0, 0), thick + 2, cv2.LINE_AA)
    cv2.putText(img, s, org, FONT, scale, color, thick, cv2.LINE_AA)


def _project(P, p, K, D):
    uv, _ = cv2.projectPoints(np.asarray(P, dtype=np.float64).reshape(-1, 1, 3), p[:3], p[3:6], K, D)
    return uv.reshape(-1, 2)


def _marker(img, pt, status, size=3):
    x, y = int(round(pt[0])), int(round(pt[1]))
    color = STATUS_COLORS[status]
    if status == core.ACCEPTED:
        cv2.circle(img, (x, y), size - 1, color, -1, cv2.LINE_AA)
    else:
        cv2.line(img, (x - size, y - size), (x + size, y + size), color, 1, cv2.LINE_AA)
        cv2.line(img, (x - size, y + size), (x + size, y - size), color, 1, cv2.LINE_AA)


def _legend(img, org):
    x, y = org
    for status, color in STATUS_COLORS.items():
        _marker(img, (x + 4, y - 4), status, 4)
        _text(img, core.STATUS_NAMES[status], (x + 14, y), 0.42, color)
        y += 16
    return y


def _draw_scene(img_bgr, field, K, D, stage):
    """Drawing on the camera image itself (shared by the full and the compact overlay)."""
    diag, band = stage['diag'], stage['band']
    p0, p1 = stage['p_before'], stage['p_after']
    canvas = img_bgr.copy()

    # Search bands (semi-transparent), per segment
    shade = canvas.copy()
    for j in range(len(field.segs)):
        m = diag['seg'] == j
        u, n = diag['u_pred'][m], diag['nrm'][m]
        poly = np.concatenate([u + band * n, (u - band * n)[::-1]])
        cv2.fillPoly(shade, [np.round(poly).astype(np.int32)], seg_color(j))
    canvas = cv2.addWeighted(shade, 0.12, canvas, 0.88, 0)

    # Model: before this stage (gray), after (segment colors)
    for p, color in ((p0, (140, 140, 140)), (p1, None)):
        dx = core.get_dx(p)
        for poly in field.outline(dx):
            uv = _project(poly, p, K, D)
            cv2.polylines(canvas, [np.round(uv).astype(np.int32)], False,
                          color or (255, 255, 255), 1, cv2.LINE_AA)
    A, B = field.endpoints(core.get_dx(p1))
    for j in range(len(field.segs)):
        uv = _project(np.linspace(A[j], B[j], 40), p1, K, D)
        cv2.polylines(canvas, [np.round(uv).astype(np.int32)], False, seg_color(j), 2, cv2.LINE_AA)

    # Samples
    for k in np.argsort(diag['status'] == core.ACCEPTED):  # rejected first, accepted on top
        st = diag['status'][k]
        if st == core.OUT_OF_IMAGE:
            continue
        pt = diag['q'][k] if not np.isnan(diag['q'][k, 0]) else diag['u_pred'][k] + diag['nrm'][k] * \
            (band if st == core.AT_BAND_EDGE else 0.0)
        _marker(canvas, pt, st)

    # Map origin / axes on z = 0
    o, x, y = _project([[0, 0, 0], [0.2, 0, 0], [0, 0.2, 0]], p1, K, D)
    cv2.arrowedLine(canvas, tuple(np.round(o).astype(int)), tuple(np.round(x).astype(int)), (0, 0, 255), 2)
    cv2.arrowedLine(canvas, tuple(np.round(o).astype(int)), tuple(np.round(y).astype(int)), (0, 255, 0), 2)
    return canvas


def render_overlay_compact(img_bgr, field, K, D, stage, title=''):
    """Same size as the camera image: edges, samples and axes, no side panel.
    Cheap enough (and small enough as JPEG) for a >= 10 Hz live stream."""
    canvas = _draw_scene(img_bgr, field, K, D, stage)
    acc = core.stage_quality(stage)['accept_ratio']
    _text(canvas, f'{title + "  " if title else ""}band {stage["band"]:.0f}px  acc {acc * 100:.0f}%', (10, 22),
          0.6, (0, 255, 255))
    return canvas


def render_overlay(img_bgr, field, K, D, stage, delta, title=''):
    diag, band = stage['diag'], stage['band']
    p0, p1 = stage['p_before'], stage['p_after']
    canvas = _draw_scene(img_bgr, field, K, D, stage)

    # Side panel: corner zooms + stats
    h, w = canvas.shape[:2]
    panel_w = 460
    panel = np.full((max(h, 820), panel_w, 3), 30, np.uint8)
    crop = 50
    tile_sz = (panel_w - 30) // 2
    names = ['origin (far right)', 'far left', 'near left', 'near right']
    for i, c in enumerate(_project(field.corners(core.get_dx(p1)), p1, K, D)):
        cx, cy = int(round(c[0])), int(round(c[1]))
        x0, y0 = cx - crop // 2, cy - crop // 2
        patch = np.zeros((crop, crop, 3), np.uint8)
        sx0, sy0, sx1, sy1 = max(0, x0), max(0, y0), min(w, x0 + crop), min(h, y0 + crop)
        if sx1 > sx0 and sy1 > sy0:
            patch[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] = canvas[sy0:sy1, sx0:sx1]
        patch = cv2.resize(patch, (tile_sz, tile_sz), interpolation=cv2.INTER_NEAREST)
        px, py = 10 + (i % 2) * (tile_sz + 10), 30 + (i // 2) * (tile_sz + 24)
        panel[py:py + tile_sz, px:px + tile_sz] = patch
        _text(panel, names[i], (px, py - 5), 0.42)
    _text(panel, f'{title} band {band:.0f}px   corner zoom {tile_sz / crop:.1f}x', (10, 16), 0.45, (0, 255, 255))

    y = 30 + 2 * (tile_sz + 24) + 10
    dmm, ddeg = core.pose_delta(p0, p1)
    _text(panel, f'pose moved this stage: {dmm:.1f} mm, {ddeg:.3f} deg', (10, y))
    y += 20
    _text(panel, 'segment       acc/n  weak lowc edge outl   RMS  mean', (10, y), 0.42, (0, 255, 255))
    for j, r in enumerate(core.segment_stats(field, diag)):
        y += 17
        c = r['counts']
        _text(panel, f'{r["name"]:12s} {c[core.ACCEPTED]:3d}/{r["n"] - c[core.OUT_OF_IMAGE]:<3d} '
                     f'{c[core.WEAK_GRADIENT]:4d} {c[core.LOW_CONTRAST]:4d} {c[core.AT_BAND_EDGE]:4d} '
                     f'{c[core.OUTLIER]:4d} {r["rms"]:5.2f} {r["mean"]:+5.2f}', (10, y), 0.42, seg_color(j))
    y += 22
    _text(panel, 'mean > 0: detected edge outside model (floor side)', (10, y), 0.4, (180, 180, 180))
    y += 22
    _text(panel, 'gray line: model before stage, color: after', (10, y), 0.4, (180, 180, 180))
    _legend(panel, (10, y + 24))

    if panel.shape[0] > h:
        canvas = np.vstack([canvas, np.full((panel.shape[0] - h, w, 3), 30, np.uint8)])
    return np.hstack([canvas, panel])


def render_strips(gray_u8, field, K, D, stage, max_half=20):
    """Each segment straightened: columns follow the model line (1 px per image px),
    rows cover +-half px along the outward normal at ZOOM x. Down = outward (floor side)."""
    diag, band = stage['diag'], stage['band']
    p0, p1 = stage['p_before'], stage['p_after']
    half = int(min(band, max_half))
    rows_off = np.linspace(-half, half, 2 * half * ZOOM + 1)
    A0, B0 = field.endpoints(core.get_dx(p0))
    A1, B1 = field.endpoints(core.get_dx(p1))
    strips = []
    for j, seg in enumerate(field.segs):
        ends = _project([A0[j], B0[j]], p0, K, D)
        ncol = max(20, int(np.linalg.norm(ends[1] - ends[0])))
        s = np.linspace(0, 1, ncol)
        P = A0[j] + s[:, None] * (B0[j] - A0[j])
        direction = (B0[j] - A0[j]) / np.linalg.norm(B0[j] - A0[j])
        uv = _project(np.concatenate([P, P + 1e-3 * direction, P + 1e-2 * field.inward(j)]), p0, K, D)
        u, u_t, u_in = uv[:ncol], uv[ncol:2 * ncol], uv[2 * ncol:]
        t = u_t - u
        t /= np.linalg.norm(t, axis=1, keepdims=True)
        n = np.column_stack([-t[:, 1], t[:, 0]])
        n[np.sum(n * (u_in - u), axis=1) > 0] *= -1
        mx = (u[None, :, 0] + rows_off[:, None] * n[None, :, 0]).astype(np.float32)
        my = (u[None, :, 1] + rows_off[:, None] * n[None, :, 1]).astype(np.float32)
        img = cv2.remap(gray_u8, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)

        def row_of(off):
            return int(round((off + half) * ZOOM))

        # Model before (dashed gray, center row) and after (segment color)
        for x in range(0, ncol, 8):
            cv2.line(img, (x, row_of(0)), (min(ncol - 1, x + 3), row_of(0)), (150, 150, 150), 1)
        P1 = A1[j] + s[:, None] * (B1[j] - A1[j])
        off1 = np.sum((_project(P1, p1, K, D) - u) * n, axis=1)
        pts = np.column_stack([np.arange(ncol), [row_of(o) for o in np.clip(off1, -half, half)]])
        cv2.polylines(img, [pts.astype(np.int32)], False, seg_color(j), 1, cv2.LINE_AA)

        # Detections
        m = np.where(diag['seg'] == j)[0]
        for k in m:
            st = diag['status'][k]
            if st == core.OUT_OF_IMAGE:
                continue
            off = diag['offset'][k]
            if np.isnan(off):
                off = half if st == core.AT_BAND_EDGE else 0.0
            x = diag['frac'][k] * (ncol - 1)
            _marker(img, (x, row_of(np.clip(off, -half, half))), st, 3)

        c = core.segment_stats(field, diag)[j]
        head = np.full((20, ncol, 3), 30, np.uint8)
        _text(head, f'{seg[0]}  acc {c["counts"][core.ACCEPTED]}/{c["n"]}  RMS {c["rms"]:.2f}  '
                    f'mean {c["mean"]:+.2f} px   (down = outward, +-{half}px x{ZOOM})', (4, 14), 0.42,
              seg_color(j))
        strips.append(np.vstack([head, img]))
    width = max(s.shape[1] for s in strips)
    out = [cv2.copyMakeBorder(s, 0, 6, 0, width - s.shape[1], cv2.BORDER_CONSTANT, value=(30, 30, 30))
           for s in strips]
    return np.vstack(out)


def render_residuals(field, stage, delta, clip=4.0, panel=(460, 130)):
    """Signed residual (+ = outward) vs position along each segment, after this stage."""
    diag = stage['diag']
    pw, ph = panel
    scale = (ph / 2 - 12) / clip
    tiles = []
    for j, seg in enumerate(field.segs):
        tile = np.full((ph, pw, 3), 25, np.uint8)
        mid = ph // 2 + 6
        for v, col in ((0, (120, 120, 120)), (delta, (60, 60, 60)), (-delta, (60, 60, 60))):
            y = int(round(mid - v * scale))
            cv2.line(tile, (30, y), (pw - 5, y), col, 1)
        for v in (-clip, 0, clip):
            _text(tile, f'{v:+.0f}', (2, int(mid - v * scale) + 4), 0.35, (160, 160, 160))
        m = np.where((diag['seg'] == j) & ~np.isnan(diag.get('resid', np.full(len(diag['seg']), np.nan))))[0]
        for k in m:
            x = int(round(30 + diag['frac'][k] * (pw - 40)))
            r = diag['resid'][k]
            y = int(round(mid - np.clip(r, -clip, clip) * scale))
            _marker(tile, (x, y), diag['status'][k], 2)
        c = core.segment_stats(field, diag)[j]
        if not np.isnan(c['mean']):
            y = int(round(mid - np.clip(c['mean'], -clip, clip) * scale))
            cv2.line(tile, (30, y), (pw - 5, y), seg_color(j), 1, cv2.LINE_AA)
        _text(tile, f'{seg[0]}  RMS {c["rms"]:.2f}  mean {c["mean"]:+.2f} px', (32, 14), 0.42, seg_color(j))
        tiles.append(tile)
    if len(tiles) % 2:
        blank = np.full((ph, pw, 3), 25, np.uint8)
        _text(blank, f'band {stage["band"]:.0f}px, +/-{clip:.0f}px shown', (10, 20), 0.45, (0, 255, 255))
        _text(blank, 'x: along segment (A -> B)', (10, 42), 0.42)
        _text(blank, 'y: residual, + = outside model', (10, 62), 0.42)
        _text(blank, f'gray lines: 0 and +-Huber delta {delta}', (10, 82), 0.42)
        tiles.append(blank)
    rows = [np.hstack([tiles[i], np.full((ph, 4, 3), 0, np.uint8), tiles[i + 1]])
            for i in range(0, len(tiles), 2)]
    return np.vstack([np.vstack([r, np.zeros((4, r.shape[1], 3), np.uint8)]) for r in rows])


def render_all(img_bgr, field, K, D, stage, delta, title=''):
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    return dict(overlay=render_overlay(img_bgr, field, K, D, stage, delta, title),
                strips=render_strips(gray, field, K, D, stage),
                residuals=render_residuals(field, stage, delta))
