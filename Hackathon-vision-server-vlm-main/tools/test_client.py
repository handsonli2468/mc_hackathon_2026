"""Minimal DEALER client for manual checks and latency numbers.

python -m tools.test_client --endpoint tcp://192.168.68.51:5555 --ping 5
python -m tools.test_client --image test.jpg --count 5 --out overlay.jpg
"""
import argparse
import json
import statistics
import time

import cv2
import numpy as np
import zmq


def make_socket(context, endpoint):
    sock = context.socket(zmq.DEALER)
    for option, value in [(zmq.LINGER, 0), (zmq.IMMEDIATE, 1), (zmq.SNDHWM, 1), (zmq.TCP_KEEPALIVE, 1),
                          (zmq.TCP_KEEPALIVE_IDLE, 5), (zmq.TCP_KEEPALIVE_INTVL, 2), (zmq.RECONNECT_IVL, 500)]:
        sock.setsockopt(option, value)
    sock.connect(endpoint)
    return sock


def request(sock, header, payload, timeout):
    """Send and wait for the matching request_id; stale replies are discarded."""
    sent = time.monotonic()
    sock.send_multipart([json.dumps(header).encode()] + ([payload] if payload else []))
    deadline = sent + timeout
    while (remaining := deadline - time.monotonic()) > 0:
        if not sock.poll(int(remaining * 1000)):
            break
        frames = sock.recv_multipart()
        reply = json.loads(frames[0])
        if reply.get('request_id') == header['request_id']:
            return reply, frames[1:], (time.monotonic() - sent) * 1000
        print(f'discard stale reply {reply.get("request_id")}')
    return None, [], (time.monotonic() - sent) * 1000


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--endpoint', default='tcp://127.0.0.1:5555')
    parser.add_argument('--ping', type=int, default=0, help='number of pings')
    parser.add_argument('--image', help='image file to send as detect')
    parser.add_argument('--count', type=int, default=1)
    parser.add_argument('--timeout', type=float, default=5.0)
    parser.add_argument('--upload-max-width', type=int, default=640)
    parser.add_argument('--jpeg-quality', type=int, default=80)
    parser.add_argument('--out', help='save bbox/mask overlay of the last FOUND reply')
    args = parser.parse_args()
    sock = make_socket(zmq.Context.instance(), args.endpoint)
    request_id, known_version = 0, -1

    rtts = []
    for _ in range(args.ping):
        request_id += 1
        reply, _, rtt = request(sock, dict(protocol_version=1, type='ping', request_id=request_id), None, args.timeout)
        print(f'ping {request_id}: {"timeout" if reply is None else reply["status"]} rtt={rtt:.1f}ms '
              f'query_version={reply and reply["query_version"]}')
        if reply:
            rtts.append(rtt)
    if len(rtts) > 1:
        print(f'ping rtt p50={statistics.median(rtts):.1f}ms max={max(rtts):.1f}ms')

    if not args.image:
        return
    image = cv2.imread(args.image, cv2.IMREAD_COLOR)
    if image is None:
        raise SystemExit(f'cannot read {args.image}')
    if image.shape[1] > args.upload_max_width:
        scale = args.upload_max_width / image.shape[1]
        image = cv2.resize(image, (args.upload_max_width, round(image.shape[0] * scale)), interpolation=cv2.INTER_AREA)
    for _ in range(args.count):
        start = time.monotonic()
        jpeg = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, args.jpeg_quality])[1].tobytes()
        encode_ms = (time.monotonic() - start) * 1000
        request_id += 1
        header = dict(protocol_version=1, type='detect', request_id=request_id, client_stamp=time.time(),
                      width=image.shape[1], height=image.shape[0], known_query_version=known_version)
        reply, extra, rtt = request(sock, header, jpeg, args.timeout)
        if reply is None:
            print(f'detect {request_id}: timeout after {rtt:.0f}ms')
            continue
        known_version = reply['query_version']
        print(f'detect {request_id}: {reply["status"]} bbox={reply["bbox"]} score={reply["score"]:.2f} '
              f'candidates={reply["num_candidates"]} mask={reply["has_mask"]} encode={encode_ms:.1f}ms '
              f'rtt={rtt:.0f}ms server={reply["server_ms"]}ms network={rtt - reply["server_ms"]:.0f}ms '
              f'{reply["error"]}')
        if reply['status'] == 'FOUND' and args.out:
            overlay = image.copy()
            x1, y1, x2, y2 = reply['bbox']
            if extra:
                mask = cv2.imdecode(np.frombuffer(extra[0], np.uint8), cv2.IMREAD_GRAYSCALE)
                assert mask.shape == (y2 - y1, x2 - x1), f'mask {mask.shape} != bbox {(y2 - y1, x2 - x1)}'
                region = overlay[y1:y2, x1:x2]
                region[mask > 0] = (region[mask > 0] * .5 + np.array([125, 235, 40]) * .5).astype(np.uint8)
            cv2.rectangle(overlay, (x1, y1), (x2 - 1, y2 - 1), (0, 220, 255), 2)
            cv2.imwrite(args.out, overlay)
            print(f'saved {args.out}')


if __name__ == '__main__':
    main()
