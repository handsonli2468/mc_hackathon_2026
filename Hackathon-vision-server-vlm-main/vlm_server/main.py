"""Entry point: python -m vlm_server.main"""
import logging
import os
import threading

from waitress import serve

from .http_api import create_app
from .query import QueryStore
from .zmq_server import Stats, Worker, ZmqServer

log = logging.getLogger('vlm_server')


def run(make_finder, drop_response=None, initial_query=None):
    """Shared by the real server and tools/mock_server.py."""
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    queries, stats = QueryStore(), Stats()
    if initial_query:
        queries.set(initial_query)
    server = ZmqServer(os.getenv('VLM_ZMQ_BIND', 'tcp://*:5555'), queries, stats)

    def work():
        try:
            finder = make_finder()
        except Exception as exc:
            stats.model_error = f'model init failed: {exc!r}'
            log.exception('model initialization failed')
            return
        stats.model_info = getattr(finder, 'info', {})
        stats.model_ready = True
        log.info('model ready: %s', stats.model_info)
        Worker(server, finder, drop_response).run_forever()

    threading.Thread(target=server.serve_forever, name='zmq-io', daemon=True).start()
    threading.Thread(target=work, name='inference', daemon=True).start()
    host, port = os.getenv('VLM_HTTP_HOST', '0.0.0.0'), int(os.getenv('VLM_HTTP_PORT', '8080'))
    log.info('HTTP query API on %s:%d', host, port)
    try:
        serve(create_app(queries, stats, server.slot), host=host, port=port, threads=4)
    finally:
        server.stop.set()


def main():
    from .pipeline import TargetFinder
    return_mask = os.getenv('VLM_RETURN_MASK', '0') == '1'
    run(lambda: TargetFinder(return_mask=return_mask), initial_query=os.getenv('VLM_INITIAL_QUERY'))


if __name__ == '__main__':
    main()
