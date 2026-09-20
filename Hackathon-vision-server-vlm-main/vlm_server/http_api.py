"""Upstream description API (temporary transport over WiFi)."""
from flask import Flask, jsonify, request


def create_app(queries, stats, slot=None):
    app = Flask(__name__)

    def current():
        text, version = queries.get()
        return dict(text=text, query_version=version)

    @app.get('/api/query')
    def get_query():
        return jsonify(current())

    @app.post('/api/query')
    def set_query():
        data = request.get_json(silent=True) or {}
        try:
            queries.set(data.get('text', ''))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(current())

    @app.delete('/api/query')
    def clear_query():
        queries.clear()
        return jsonify(current())

    @app.get('/api/target')
    def get_target():
        """For the BT engine: has the current description been FOUND at least once?"""
        return jsonify(queries.target())

    @app.get('/api/status')
    def status():
        result = stats.snapshot()
        result.update(query=current(), target=queries.target(), dropped_requests=slot.dropped if slot else None)
        return jsonify(result)

    return app
