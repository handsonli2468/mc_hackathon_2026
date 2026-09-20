#!/usr/bin/env python3
"""Mock VLM server for docs/vlm_transport.md, so the client can be developed without a real VLM.

Answers `detect` with a fixed bbox [x1, y1, x2, y2] (centered by default), like the real server without a mask
(VLM_RETURN_MASK=0); --mask adds a mask PNG of the bbox. `ping` gets PONG right away. Artificial delay, jitter,
dropped responses, NOT_FOUND and "model loading" errors let the client's timeout and retry paths be tested.
The upstream server repo has its own mock (python -m tools.mock_server); this one only needs pyzmq and OpenCV.

  ros2 run object_tracker mock_vlm_server.py --delay-s 3.4
  ros2 run object_tracker mock_vlm_server.py --bbox 200 120 360 320 --mask --drop-rate 0.3
"""
import argparse
import json
import os
import random
import time

import cv2
import numpy as np
import zmq

PROTOCOL_VERSION = 1


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--port', type=int, default=5555)
    p.add_argument('--delay-s', type=float, default=1.0, help='pretend the VLM takes this long')
    p.add_argument('--jitter-s', type=float, default=0.0, help='uniform extra delay in [0, jitter]')
    p.add_argument('--drop-rate', type=float, default=0.0, help='probability of not answering at all')
    p.add_argument('--not-found-rate', type=float, default=0.0, help='probability of answering NOT_FOUND')
    p.add_argument('--no-query', action='store_true', help='always answer NO_QUERY')
    p.add_argument('--query-version', type=int, default=1)
    p.add_argument('--bbox', type=int, nargs=4, metavar=('X1', 'Y1', 'X2', 'Y2'),
                   help='bbox corners in upload coordinates (x2, y2 exclusive); default is a centered box of --bbox-ratio')
    p.add_argument('--bbox-ratio', type=float, default=0.3, help='centered box size as a fraction of the image')
    p.add_argument('--mask', action='store_true', help='also return a mask PNG (like VLM_RETURN_MASK=1)')
    p.add_argument('--ellipse', action='store_true', help='with --mask: mask an ellipse inside the bbox')
    p.add_argument('--loading-requests', type=int, default=0,
                   help='answer the first N detect requests with ERROR "model loading" right away')
    p.add_argument('--save-dir', help='write the received JPEGs here')
    return p.parse_args()


def make_bbox(args, width, height):
    if args.bbox:
        return list(args.bbox)
    w = max(8, int(width * args.bbox_ratio))
    h = max(8, int(height * args.bbox_ratio))
    x1, y1 = (width - w) // 2, (height - h) // 2
    return [x1, y1, x1 + w, y1 + h]


def make_mask_png(args, bbox):
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    mask = np.zeros((h, w), np.uint8)
    if args.ellipse:
        cv2.ellipse(mask, (w // 2, h // 2), (w // 2 - 1, h // 2 - 1), 0, 0, 360, 255, -1)
    else:
        mask[:] = 255
    ok, png = cv2.imencode('.png', mask)
    return png.tobytes() if ok else None


def main():
    args = parse_args()
    if args.save_dir:
        os.makedirs(args.save_dir, exist_ok=True)

    context = zmq.Context()
    socket = context.socket(zmq.ROUTER)
    socket.setsockopt(zmq.LINGER, 0)
    socket.setsockopt(zmq.TCP_KEEPALIVE, 1)
    socket.bind(f'tcp://*:{args.port}')
    print(f'mock VLM server on tcp://*:{args.port}: delay={args.delay_s}s jitter={args.jitter_s}s '
          f'drop={args.drop_rate} not_found={args.not_found_rate} mask={args.mask} '
          f'query_version={args.query_version}', flush=True)
    detect_count = 0

    while True:
        frames = socket.recv_multipart()
        # the client may have timed out and sent again: keep only the newest request (docs section 5)
        dropped = 0
        while True:
            try:
                frames = socket.recv_multipart(zmq.NOBLOCK)
                dropped += 1
            except zmq.Again:
                break
        if dropped:
            print(f'skipped {dropped} queued request(s)', flush=True)

        identity, header_raw = frames[0], frames[1]
        try:
            header = json.loads(header_raw.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            print(f'bad header: {e}', flush=True)
            continue
        request_id = header.get('request_id', 0)

        base = {'protocol_version': PROTOCOL_VERSION, 'request_id': request_id, 'query_version': args.query_version,
                'bbox': None, 'score': -1.0, 'num_candidates': 0, 'has_mask': False, 'server_ms': 0, 'error': ''}
        if header.get('type') == 'ping':
            socket.send_multipart([identity, json.dumps(dict(base, status='PONG')).encode()])
            continue
        if args.no_query:
            # answered right away, like the real server
            socket.send_multipart([identity, json.dumps(dict(base, status='NO_QUERY')).encode()])
            print(f'request {request_id}: NO_QUERY', flush=True)
            continue
        if detect_count < args.loading_requests:
            # the real server answers these right away, without queueing for inference
            detect_count += 1
            socket.send_multipart([identity, json.dumps(dict(base, status='ERROR', error='model loading')).encode()])
            print(f'request {request_id}: ERROR model loading', flush=True)
            continue
        detect_count += 1

        t0 = time.time()
        width = int(header.get('width', 0))
        height = int(header.get('height', 0))
        if args.save_dir and len(frames) > 2:
            path = os.path.join(args.save_dir, f'request_{request_id:05d}.jpg')
            with open(path, 'wb') as f:
                f.write(frames[2])

        if random.random() < args.drop_rate:
            print(f'request {request_id}: dropped on purpose', flush=True)
            continue

        time.sleep(args.delay_s + random.uniform(0.0, args.jitter_s))
        reply = dict(base, server_ms=int((time.time() - t0) * 1000))

        if random.random() < args.not_found_rate or width <= 0 or height <= 0:
            reply['status'] = 'NOT_FOUND'
            socket.send_multipart([identity, json.dumps(reply).encode()])
        else:
            reply.update(status='FOUND', bbox=make_bbox(args, width, height), num_candidates=1)
            png = make_mask_png(args, reply['bbox']) if args.mask else None
            if args.mask and png is None:
                reply.update(status='ERROR', bbox=None, error='could not encode the mask')
                socket.send_multipart([identity, json.dumps(reply).encode()])
            elif args.mask:
                reply.update(has_mask=True, score=0.87)  # SAM2 IoU estimate when a mask is attached
                socket.send_multipart([identity, json.dumps(reply).encode(), png])
            else:
                socket.send_multipart([identity, json.dumps(reply).encode()])
        print(f"request {request_id}: {width}x{height} -> {reply['status']} in {reply['server_ms']} ms", flush=True)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
