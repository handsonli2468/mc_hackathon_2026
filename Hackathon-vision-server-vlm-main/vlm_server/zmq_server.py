"""ROUTER I/O loop plus a single inference worker.

The ZMQ socket is only touched by the I/O thread. Pings, NO_QUERY and "model
not ready" are answered immediately; detect requests go through LatestSlot, so
while the worker is busy only the newest request per client survives and the
older ones are dropped without a reply (vlm_transport.md section 5).
"""
import logging
import queue
import threading
import time
from collections import OrderedDict, deque

import zmq

from . import protocol as P

log = logging.getLogger(__name__)


class LatestSlot:
    """Pending detect requests, at most one per client identity."""
    def __init__(self):
        self._cond = threading.Condition()
        self._items = OrderedDict()
        self.dropped = 0

    def put(self, identity, item):
        with self._cond:
            if identity in self._items:
                self.dropped += 1
            self._items[identity] = item
            self._cond.notify()

    def take(self, timeout):
        with self._cond:
            if not self._items and not self._cond.wait(timeout):
                return None
            if not self._items:
                return None
            return self._items.popitem(last=False)


class Stats:
    def __init__(self, size=50):
        self._lock = threading.Lock()
        self.model_ready = False
        self.model_error = None
        self.model_info = {}
        self.recent = deque(maxlen=size)

    def record(self, **row):
        with self._lock:
            self.recent.append(row)

    def snapshot(self):
        with self._lock:
            return dict(model_ready=self.model_ready, model_error=self.model_error,
                        model_info=self.model_info, recent=list(self.recent))


class ZmqServer:
    def __init__(self, bind, queries, stats, context=None):
        self.bind = bind
        self.queries = queries
        self.stats = stats
        self.slot = LatestSlot()
        self.outbound = queue.Queue()
        self.context = context or zmq.Context.instance()
        self.stop = threading.Event()
        self.bound = threading.Event()
        self.endpoint = None

    def send(self, identity, header, payload=None):
        """Thread-safe: queue a response for the I/O thread."""
        self.outbound.put((identity, header, payload))

    def serve_forever(self):
        sock = self.context.socket(zmq.ROUTER)
        sock.setsockopt(zmq.LINGER, 0)
        sock.setsockopt(zmq.TCP_KEEPALIVE, 1)
        sock.bind(self.bind)
        self.endpoint = sock.getsockopt_string(zmq.LAST_ENDPOINT)
        self.bound.set()
        log.info('ZMQ ROUTER bound to %s', self.endpoint)
        poller = zmq.Poller()
        poller.register(sock, zmq.POLLIN)
        try:
            while not self.stop.is_set():
                if poller.poll(10):
                    while True:
                        try:
                            frames = sock.recv_multipart(zmq.NOBLOCK)
                        except zmq.Again:
                            break
                        self._handle(frames, time.monotonic())
                self._flush(sock)
        finally:
            sock.close(0)

    def _flush(self, sock):
        while True:
            try:
                identity, header, payload = self.outbound.get_nowait()
            except queue.Empty:
                return
            parts = [identity, P.encode_header(header)]
            if payload is not None:
                parts.append(payload)
            try:
                sock.send_multipart(parts, zmq.NOBLOCK)
            except zmq.ZMQError as exc:
                # Peer gone or its pipe is full; the client will time out and resend.
                log.warning('drop response %s: %s', header.get('request_id'), exc)

    def _handle(self, frames, received):
        if len(frames) < 2:
            return
        identity, parts = frames[0], frames[1:]
        _, version = self.queries.get()
        try:
            header, jpeg = P.parse_request(parts)
        except P.ProtocolError as exc:
            if exc.request_id is not None:
                self.send(identity, P.response(exc.request_id, P.ERROR, version, error=str(exc)))
            else:
                log.warning('drop malformed request: %s', exc)
            return
        request_id = header['request_id']
        if jpeg is None:
            self.send(identity, P.response(request_id, P.PONG, version))
        elif self.queries.get()[0] is None:
            self.send(identity, P.response(request_id, P.NO_QUERY, version))
        elif not self.stats.model_ready:
            error = self.stats.model_error or 'model loading'
            self.send(identity, P.response(request_id, P.ERROR, version, error=error))
        else:
            self.slot.put(identity, (header, jpeg, received))


class Worker:
    """Runs finder.find(jpeg, text) -> Detection|None on the newest pending request."""
    def __init__(self, server, finder, drop_response=None):
        self.server = server
        self.finder = finder
        self.drop_response = drop_response or (lambda: False)

    def run_forever(self):
        server = self.server
        while not server.stop.is_set():
            item = server.slot.take(0.1)
            if item is not None:
                self.process(*item)

    def process(self, identity, request):
        header, jpeg, received = request
        request_id = header['request_id']
        text, version = self.server.queries.get()
        detection, payload = None, None
        if text is None:
            reply = dict(status=P.NO_QUERY)
        else:
            try:
                detection = self.finder.find(jpeg, text)
            except Exception as exc:
                log.exception('find failed for request %s', request_id)
                reply = dict(status=P.ERROR, error=repr(exc))
            else:
                if detection is None:
                    reply = dict(status=P.NOT_FOUND)
                else:
                    payload = detection.mask_png
                    reply = dict(status=P.FOUND, bbox=detection.bbox, score=detection.score,
                                 num_candidates=detection.num_candidates, has_mask=payload is not None)
                    # Use the version this inference started with, so a FOUND for an
                    # old description never marks a newer one as found.
                    self.server.queries.mark_found(version)
        server_ms = (time.monotonic() - received) * 1000
        header_out = P.response(request_id, query_version=version, server_ms=server_ms, **reply)
        self.server.stats.record(request_id=request_id, status=header_out['status'], server_ms=round(server_ms, 1),
                                 client_stamp=header.get('client_stamp'),
                                 **(detection.timings if detection else {}))
        if self.drop_response():
            log.info('simulated drop of response %s', request_id)
            return
        self.server.send(identity, header_out, payload)
