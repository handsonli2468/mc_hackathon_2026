import contextlib
import io
import json
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np
import zmq
from PIL import Image

from scripts import eval_locate as E
from vlm_server import protocol as P
from vlm_server.gpu_check import check_gpu
from vlm_server.http_api import create_app
from vlm_server.locator import format_prompt, valid_box
from vlm_server.pipeline import Detection, TargetFinder, encode_mask_crop, rank_candidates, to_pixel_box
from vlm_server.query import QueryStore
from vlm_server.zmq_server import Stats, Worker, ZmqServer


def jpeg(width=64, height=48):
    stream = io.BytesIO()
    Image.new('RGB', (width, height)).save(stream, format='JPEG')
    return stream.getvalue()


def header(request_id, kind='detect'):
    return json.dumps(dict(protocol_version=1, type=kind, request_id=request_id)).encode()


class Protocol(unittest.TestCase):
    def test_ping_and_detect(self):
        self.assertIsNone(P.parse_request([header(1, 'ping')])[1])
        self.assertEqual(P.parse_request([header(2), b'x'])[1], b'x')

    def test_errors_keep_request_id_when_possible(self):
        with self.assertRaises(P.ProtocolError) as ctx:
            P.parse_request([header(7)])
        self.assertEqual(ctx.exception.request_id, 7)
        for frames in ([b'not json'], [b'[]'], [json.dumps(dict(request_id=-1)).encode()], []):
            with self.assertRaises(P.ProtocolError) as ctx:
                P.parse_request(frames)
            self.assertIsNone(ctx.exception.request_id)


class Query(unittest.TestCase):
    def test_every_set_is_a_new_version(self):
        store = QueryStore()
        self.assertEqual(store.get(), (None, 0))
        self.assertEqual(store.set(' cup '), 1)
        self.assertEqual(store.get(), ('cup', 1))
        self.assertEqual(store.set('cup'), 2)  # same text re-sent = new task
        self.assertEqual(store.set('red cup'), 3)
        self.assertEqual(store.clear(), 4)
        self.assertEqual(store.clear(), 4)
        with self.assertRaises(ValueError):
            store.set('  ')

    def test_found_is_tied_to_version(self):
        store = QueryStore()
        self.assertFalse(store.mark_found(0))  # no description yet
        version = store.set('cup')
        self.assertFalse(store.target()['found'])
        self.assertFalse(store.mark_found(version - 1))  # stale result
        self.assertTrue(store.mark_found(version))
        self.assertEqual(store.target(), dict(text='cup', query_version=version, found=True))
        store.set('cup')
        self.assertFalse(store.target()['found'])
        store.mark_found(store.get()[1])
        store.clear()
        self.assertEqual(store.target(), dict(text=None, query_version=store.get()[1], found=False))

    def test_http_api(self):
        store = QueryStore()
        client = create_app(store, Stats()).test_client()
        self.assertEqual(client.post('/api/query', json=dict(text='cup')).get_json()['query_version'], 1)
        self.assertEqual(client.post('/api/query', json={}).status_code, 400)
        self.assertEqual(client.delete('/api/query').get_json(), dict(text=None, query_version=2))

    def test_http_target(self):
        store = QueryStore()
        client = create_app(store, Stats()).test_client()
        self.assertEqual(client.get('/api/target').get_json(), dict(text=None, query_version=0, found=False))
        client.post('/api/query', json=dict(text='cup'))
        store.mark_found(1)
        self.assertEqual(client.get('/api/target').get_json(), dict(text='cup', query_version=1, found=True))
        self.assertTrue(client.get('/api/status').get_json()['target']['found'])
        client.post('/api/query', json=dict(text='cup'))  # same text clears
        self.assertEqual(client.get('/api/target').get_json(), dict(text='cup', query_version=2, found=False))


class Geometry(unittest.TestCase):
    def test_pixel_box_is_exclusive_and_clipped(self):
        self.assertEqual(to_pixel_box([10.4, 5.6, 20.2, 30.9], 64, 48), [10, 5, 21, 31])
        self.assertEqual(to_pixel_box([-5, -5, 100, 100], 64, 48), [0, 0, 64, 48])

    def test_invalid_boxes_rejected(self):
        for box in ([1, 2, 1, 5], [0, 0, float('nan'), 4], [1, 2]):
            with self.assertRaises(ValueError):
                valid_box(box, 640, 480)
        self.assertEqual(valid_box([-2, -5, 700, 600], 640, 480), [0., 0., 639., 479.])

    def test_oversized_image_rejected_before_locate(self):
        finder = TargetFinder.__new__(TargetFinder)
        with self.assertRaises(ValueError):
            finder.find(jpeg(2000, 100), 'cup')

    def test_mask_crop_matches_bbox(self):
        mask = np.zeros((48, 64), bool)
        mask[10:20, 30:40] = True
        png = encode_mask_crop(mask, [30, 10, 40, 20])
        crop = np.asarray(Image.open(io.BytesIO(png)))
        self.assertEqual(crop.shape, (10, 10))
        self.assertTrue((crop == 255).all())


class Prompt(unittest.TestCase):
    def test_default_is_the_original_prompt(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual(format_prompt('the cup'),
                             'Locate all the instances that matches the following description: the cup.')

    def test_named_and_custom_templates(self):
        self.assertEqual(format_prompt('the cup', 'ground_single'),
                         'Locate a single instance that matches the following description: the cup.')
        self.assertEqual(format_prompt('the cup', 'Find {q} only.'), 'Find the cup only.')
        with patch.dict('os.environ', {'LA_PROMPT_TEMPLATE': 'region'}):
            self.assertTrue(format_prompt('the cup').startswith('Locate the region'))
        with self.assertRaises(ValueError):
            format_prompt('the cup', 'nonsense')


class Ranking(unittest.TestCase):
    boxes = [[0, 0, 1, 1], [1, 1, 2, 2], [2, 2, 3, 3]]

    def test_unscored_keeps_locate_order(self):
        scores = [dict(p_start=-1, p_coord=-1, p_object=-1)] * 3
        self.assertEqual([b for b, _ in rank_candidates(self.boxes, scores)], self.boxes)

    def test_highest_score_first_and_threshold(self):
        scores = [dict(p_object=0.4, p_coord=0.9), dict(p_object=0.95, p_coord=0.5), dict(p_object=0.8, p_coord=0.99)]
        self.assertEqual(rank_candidates(self.boxes, scores)[0], (self.boxes[1], 0.95))
        self.assertEqual([s for _, s in rank_candidates(self.boxes, scores, 0.7)], [0.95, 0.8])
        self.assertEqual(rank_candidates(self.boxes, scores, 0.97), [])
        self.assertEqual(rank_candidates(self.boxes, scores, 0, 'p_coord')[0][0], self.boxes[2])


class Evaluation(unittest.TestCase):
    def test_iou(self):
        self.assertAlmostEqual(E.iou([0, 0, 10, 10], [0, 0, 10, 10]), 1.0)
        self.assertAlmostEqual(E.iou([0, 0, 10, 10], [5, 0, 15, 10]), 50 / 150)
        self.assertEqual(E.iou([0, 0, 1, 1], [2, 2, 3, 3]), 0.0)

    def test_judge(self):
        target, decoy = [0, 0, 10, 10], [50, 50, 60, 60]
        self.assertEqual(E.judge(None, [], []), 'reject')
        self.assertEqual(E.judge(None, [decoy], [0.6]), 'false_positive')
        self.assertEqual(E.judge(None, [decoy], [0.6], threshold=0.7), 'reject')
        self.assertEqual(E.judge(target, [decoy, target], [0.5, 0.9]), 'hit')
        self.assertEqual(E.judge(target, [decoy, target], [0.9, 0.5]), 'wrong_box')
        self.assertEqual(E.judge(target, [target], [0.5], threshold=0.7), 'miss')
        self.assertEqual(E.judge(target, [target, decoy], [-1, -1]), 'hit')  # unscored: first box

    def test_summarize(self):
        s = lambda v: dict(p_object=v, p_coord=v, p_start=1.0)
        rows = [dict(config='a', expected=[0, 0, 10, 10], boxes=[[0, 0, 10, 10]], scores=[s(0.9)], locate_ms=100),
                dict(config='a', expected=None, boxes=[[5, 5, 9, 9]], scores=[s(0.4)], locate_ms=300)]
        by_threshold = {row['threshold']: row for row in E.summarize(rows, thresholds=[0.0, 0.5])}
        self.assertEqual((by_threshold[0.0]['hit_rate'], by_threshold[0.0]['false_positive_rate']), (1.0, 1.0))
        self.assertEqual((by_threshold[0.5]['hit_rate'], by_threshold[0.5]['false_positive_rate']), (1.0, 0.0))
        self.assertEqual(by_threshold[0.0]['locate_ms_p50'], 200)

    def test_upload_jpeg_scales_like_client(self):
        data, scale = E.upload_jpeg(Image.new('RGB', (1280, 720)), 640)
        self.assertEqual((Image.open(io.BytesIO(data)).size, scale), ((640, 360), 0.5))
        self.assertEqual(E.upload_jpeg(Image.new('RGB', (598, 472)), 640)[1], 1.0)


class GpuCheck(unittest.TestCase):
    """The GPU probe runs in a child process so a native crash cannot take the server down."""

    @patch('vlm_server.gpu_check.subprocess.run')
    def test_native_fault_is_not_success(self, run):
        run.return_value = subprocess.CompletedProcess([], -11, 'FILL\n', 'Segmentation fault')
        with self.assertRaisesRegex(RuntimeError, 'returncode=-11'):
            check_gpu()

    @patch('vlm_server.gpu_check.subprocess.run')
    def test_empty_success_is_rejected(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, '', '')
        with self.assertRaisesRegex(RuntimeError, 'without GPU_PASS'):
            check_gpu()

    @patch('vlm_server.gpu_check.subprocess.run')
    def test_verified_child_result(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, 'GPU_PASS ' + json.dumps({'arch': 'gfx1152'}), '')
        self.assertEqual(check_gpu()['arch'], 'gfx1152')
        self.assertIn('vlm_server.gpu_probe', run.call_args.args[0])


class SlowFinder:
    def __init__(self, delay):
        self.delay, self.calls = delay, []

    def find(self, data, text):
        self.calls.append(text)
        time.sleep(self.delay)
        if text == 'nothing':
            return None
        if text == 'boom':
            raise RuntimeError('boom')
        return Detection([1, 2, 11, 22], 0.5, 3, encode_mask_crop(np.ones((48, 64), bool), [1, 2, 11, 22]))


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.context = zmq.Context()
        self.queries, self.stats = QueryStore(), Stats()
        self.server = ZmqServer('tcp://127.0.0.1:*', self.queries, self.stats, self.context)
        self.finder = SlowFinder(0.3)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        threading.Thread(target=Worker(self.server, self.finder).run_forever, daemon=True).start()
        self.assertTrue(self.server.bound.wait(2))
        self.client = self.context.socket(zmq.DEALER)
        self.client.setsockopt(zmq.LINGER, 0)
        self.client.connect(self.server.endpoint)

    def tearDown(self):
        self.server.stop.set()
        self.client.close(0)
        time.sleep(0.05)
        self.context.term()

    def recv(self, timeout=2.0):
        self.assertTrue(self.client.poll(int(timeout * 1000)), 'no reply')
        frames = self.client.recv_multipart()
        return json.loads(frames[0]), frames[1:]

    def detect(self, request_id):
        self.client.send_multipart([header(request_id), jpeg()])

    def test_immediate_statuses(self):
        self.client.send_multipart([header(1, 'ping')])
        self.assertEqual(self.recv()[0]['status'], P.PONG)
        self.detect(2)
        self.assertEqual(self.recv()[0]['status'], P.NO_QUERY)
        self.queries.set('cup')
        self.detect(3)
        reply = self.recv()[0]
        self.assertEqual((reply['request_id'], reply['status'], reply['error']), (3, P.ERROR, 'model loading'))
        self.client.send_multipart([header(4)])
        self.assertEqual(self.recv()[0]['status'], P.ERROR)

    def test_found_not_found_error(self):
        self.stats.model_ready = True
        for text, status in (('cup', P.FOUND), ('nothing', P.NOT_FOUND), ('boom', P.ERROR)):
            self.queries.set(text)
            # The worker logs the finder exception; capture it so the expected traceback stays out of test output.
            logs = self.assertLogs('vlm_server.zmq_server', 'ERROR') if status == P.ERROR else contextlib.nullcontext()
            with logs:
                self.detect(10)
                reply, extra = self.recv()
            self.assertEqual(reply['status'], status)
            if status == P.FOUND:
                self.assertEqual((reply['bbox'], reply['num_candidates'], reply['has_mask']), ([1, 2, 11, 22], 3, True))
                self.assertEqual(np.asarray(Image.open(io.BytesIO(extra[0]))).shape, (20, 10))
                self.assertGreaterEqual(reply['server_ms'], 250)
            else:
                self.assertEqual(extra, [])

    def test_found_marks_target(self):
        self.stats.model_ready = True
        self.queries.set('nothing')
        self.detect(1)
        self.assertEqual(self.recv()[0]['status'], P.NOT_FOUND)
        self.assertFalse(self.queries.target()['found'])
        self.queries.set('cup')
        self.detect(2)
        self.assertEqual(self.recv()[0]['status'], P.FOUND)
        self.assertTrue(self.queries.target()['found'])

    def test_late_found_does_not_mark_new_description(self):
        self.stats.model_ready = True
        old = self.queries.set('cup')
        self.detect(1)
        time.sleep(0.1)  # worker is inside find() for the old description
        new = self.queries.set('cup')  # new task while inference runs
        reply = self.recv()[0]
        self.assertEqual((reply['status'], reply['query_version']), (P.FOUND, old))
        self.assertEqual(self.queries.target(), dict(text='cup', query_version=new, found=False))

    def test_only_newest_pending_request_is_processed(self):
        self.stats.model_ready = True
        self.queries.set('cup')
        self.detect(1)
        time.sleep(0.1)  # worker is now busy with request 1
        for request_id in (2, 3, 4):
            self.detect(request_id)
        # Pings are answered while inference runs.
        self.client.send_multipart([header(99, 'ping')])
        self.assertEqual(self.recv(0.2)[0]['request_id'], 99)
        ids = [self.recv()[0]['request_id'], self.recv()[0]['request_id']]
        self.assertEqual(ids, [1, 4])
        self.assertFalse(self.client.poll(500))
        self.assertEqual(self.server.slot.dropped, 2)


if __name__ == '__main__':
    unittest.main()
