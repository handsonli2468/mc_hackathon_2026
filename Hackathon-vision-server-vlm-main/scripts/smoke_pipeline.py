"""Real Locate (+ optional SAM) pipeline on one image, without ZMQ. Not a quality benchmark.

python -m scripts.smoke_pipeline /output/test.jpg --target "the cup" [--mask] [--repeat 5]
"""
import argparse
import io
import json
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from vlm_server.pipeline import TargetFinder

parser = argparse.ArgumentParser()
parser.add_argument('image', help='Image path inside container, e.g. /output/test.jpg')
parser.add_argument('--target', default='the cup')
parser.add_argument('--mask', action='store_true', help='also run SAM and check the mask PNG')
parser.add_argument('--repeat', type=int, default=3, help='first run includes warm-up')
parser.add_argument('--out', default='/output/smoke-pipeline.png')
args = parser.parse_args()

start = time.monotonic()
finder = TargetFinder(return_mask=args.mask)
print(json.dumps(dict(load_s=round(time.monotonic() - start, 2), **finder.info)), flush=True)
data = Path(args.image).read_bytes()
image = Image.open(io.BytesIO(data)).convert('RGB')
result = None
for i in range(args.repeat):
    start = time.monotonic()
    result = finder.find(data, args.target)
    total = (time.monotonic() - start) * 1000
    row = dict(run=i, total_ms=round(total, 1), found=result is not None)
    if result:
        row.update(bbox=result.bbox, score=result.score, sam_score=result.sam_score, candidates=result.num_candidates,
                   **{k: round(v, 1) for k, v in result.timings.items()})
        x1, y1, x2, y2 = result.bbox
        assert 0 <= x1 < x2 <= image.width and 0 <= y1 < y2 <= image.height, result.bbox
        if args.mask:
            mask = np.asarray(Image.open(io.BytesIO(result.mask_png)))
            assert mask.shape == (y2 - y1, x2 - x1), (mask.shape, result.bbox)
            row['mask_pixels'] = int((mask > 0).sum())
    print(json.dumps(row), flush=True)

if result:
    x1, y1, x2, y2 = result.bbox
    overlay = image.copy()
    if result.mask_png:
        tint = Image.new('RGB', (x2 - x1, y2 - y1), (40, 235, 125))
        region = overlay.crop((x1, y1, x2, y2))
        overlay.paste(Image.blend(region, tint, 0.5), (x1, y1), Image.open(io.BytesIO(result.mask_png)))
    ImageDraw.Draw(overlay).rectangle((x1, y1, x2 - 1, y2 - 1), outline=(255, 220, 0), width=2)
    overlay.save(args.out)
    print(f'saved {args.out}')
print('PIPELINE_CALL_PASS; inspect the overlay for detection quality.' if result else
      'PIPELINE_CALL_NOT_FOUND; the call ran but Locate returned no box.')
