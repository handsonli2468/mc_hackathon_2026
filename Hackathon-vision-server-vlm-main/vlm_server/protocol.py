"""Wire format of the VLM target channel (see vlm_transport.md). Dependency-free."""
import json

PROTOCOL_VERSION = 1

FOUND = 'FOUND'
NOT_FOUND = 'NOT_FOUND'
NO_QUERY = 'NO_QUERY'
ERROR = 'ERROR'
PONG = 'PONG'

DETECT = 'detect'
PING = 'ping'

MAX_REQUEST_ID = 2**32 - 1


class ProtocolError(ValueError):
    """Malformed request. `request_id` is set when it could still be parsed."""
    def __init__(self, message, request_id=None):
        super().__init__(message)
        self.request_id = request_id


def encode_header(header):
    return json.dumps(header, separators=(',', ':')).encode()


def parse_request(frames):
    """frames: message parts after the ROUTER identity -> (header dict, jpeg bytes or None)."""
    if not frames:
        raise ProtocolError('empty message')
    try:
        header = json.loads(frames[0].decode())
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError(f'header is not JSON: {exc}') from None
    if not isinstance(header, dict):
        raise ProtocolError('header must be a JSON object')
    request_id = header.get('request_id')
    if isinstance(request_id, bool) or not isinstance(request_id, int) or not 0 <= request_id <= MAX_REQUEST_ID:
        raise ProtocolError('request_id must be uint32')
    if header.get('protocol_version') != PROTOCOL_VERSION:
        raise ProtocolError(f'unsupported protocol_version {header.get("protocol_version")!r}', request_id)
    kind = header.get('type')
    if kind == PING:
        return header, None
    if kind != DETECT:
        raise ProtocolError(f'unknown type {kind!r}', request_id)
    if len(frames) < 2 or not frames[1]:
        raise ProtocolError('detect request needs a JPEG frame', request_id)
    return header, bytes(frames[1])


def response(request_id, status, query_version, server_ms=0, bbox=None, score=-1.0,
             num_candidates=0, has_mask=False, error=''):
    """bbox is [x1, y1, x2, y2] in upload-image pixels, x2/y2 exclusive; only valid for FOUND."""
    return dict(protocol_version=PROTOCOL_VERSION, request_id=request_id, status=status,
                query_version=query_version, bbox=list(bbox) if bbox is not None else None,
                score=float(score), num_candidates=int(num_candidates), has_mask=bool(has_mask),
                server_ms=int(round(server_ms)), error=error)
