"""Protocol-compatible mock server; no models needed.

python -m tools.mock_server --query "the cup" --delay 0.8 --drop-rate 0.1 [--mask]
Change the description with: curl -X POST localhost:8080/api/query -H 'Content-Type: application/json' -d '{"text":"x"}'
"""
import argparse
import io
import os
import random
import time

import numpy as np
from PIL import Image

from vlm_server.main import run
from vlm_server.pipeline import Detection, encode_mask_crop


class MockFinder:
    info = dict(mock=True)

    def __init__(self, args):
        self.args = args

    def find(self, jpeg, text):
        time.sleep(max(0.0, random.gauss(self.args.delay, self.args.jitter)))
        if random.random() < self.args.not_found_rate:
            return None
        width, height = Image.open(io.BytesIO(jpeg)).size
        if self.args.bbox:
            x1, y1, x2, y2 = self.args.bbox
        else:
            # Centered box, half of each dimension.
            x1, y1, x2, y2 = width // 4, height // 4, width * 3 // 4, height * 3 // 4
        x1, x2 = sorted((max(0, min(width - 1, x1)), max(1, min(width, x2))))
        y1, y2 = sorted((max(0, min(height - 1, y1)), max(1, min(height, y2))))
        bbox = [x1, y1, x2, y2]
        png, score = None, -1.0
        if self.args.mask:
            yy, xx = np.mgrid[:height, :width]
            cx, cy, rx, ry = (x1 + x2) / 2, (y1 + y2) / 2, max(1, (x2 - x1) / 2), max(1, (y2 - y1) / 2)
            png, score = encode_mask_crop(((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2 <= 1, bbox), 0.9
        return Detection(bbox, score, 1, png, dict(mock_delay_s=self.args.delay))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bind', default='tcp://*:5555')
    parser.add_argument('--http-port', type=int, default=8080)
    parser.add_argument('--query', default=None, help='initial description; omit to start in NO_QUERY')
    parser.add_argument('--delay', type=float, default=0.5, help='mean inference time, seconds')
    parser.add_argument('--jitter', type=float, default=0.1)
    parser.add_argument('--drop-rate', type=float, default=0.0, help='probability of never replying')
    parser.add_argument('--not-found-rate', type=float, default=0.0)
    parser.add_argument('--bbox', type=int, nargs=4, metavar=('X1', 'Y1', 'X2', 'Y2'))
    parser.add_argument('--mask', action='store_true', help='attach an elliptical mask PNG')
    args = parser.parse_args()
    os.environ['VLM_ZMQ_BIND'] = args.bind
    os.environ['VLM_HTTP_PORT'] = str(args.http_port)
    run(lambda: MockFinder(args), drop_response=lambda: random.random() < args.drop_rate,
        initial_query=args.query)


if __name__ == '__main__':
    main()
