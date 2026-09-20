"""Current target description from the upstream node, versioned for the client.

Also remembers whether the current description has been FOUND at least once,
for the BT engine (GET /api/target). Every new description resets it.
"""
import threading

MAX_QUERY_CHARS = 300


class QueryStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._text = None
        self._version = 0
        self._found = False

    def get(self):
        with self._lock:
            return self._text, self._version

    def set(self, text):
        """Start a new target task. Re-sending the same text also counts as new."""
        text = str(text).strip()
        if not text or len(text) > MAX_QUERY_CHARS:
            raise ValueError(f'query must be 1-{MAX_QUERY_CHARS} characters')
        with self._lock:
            self._text = text
            self._version += 1
            self._found = False
            return self._version

    def clear(self):
        with self._lock:
            if self._text is not None:
                self._text = None
                self._version += 1
            self._found = False
            return self._version

    def mark_found(self, version):
        """Record a FOUND made under `version`; ignored if the description changed meanwhile."""
        with self._lock:
            if self._text is None or version != self._version:
                return False
            self._found = True
            return True

    def target(self):
        with self._lock:
            return dict(text=self._text, query_version=self._version, found=self._found)
