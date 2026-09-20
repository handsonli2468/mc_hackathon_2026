"""ctypes wrapper around locate-anything.cpp's C API (liblocate_anything.so)."""
import ctypes as C
import io
import os

import numpy as np
from PIL import Image


# Prompt templates from the LocateAnything-3B model card; `detect` is the original default.
TEMPLATES = {
    'detect': 'Locate all the instances that matches the following description: {q}.',
    'ground_multi': 'Locate all the instances that match the following description: {q}.',
    'ground_single': 'Locate a single instance that matches the following description: {q}.',
    'region': 'Locate the region that matches the following description: {q}.',
}


def format_prompt(description, template=None):
    """template: a TEMPLATES name or a custom string containing {q}; defaults to $LA_PROMPT_TEMPLATE or detect."""
    template = template or os.getenv('LA_PROMPT_TEMPLATE') or 'detect'
    if template in TEMPLATES:
        template = TEMPLATES[template]
    elif '{q}' not in template:
        raise ValueError(f'LA_PROMPT_TEMPLATE must be one of {sorted(TEMPLATES)} or contain {{q}}')
    return template.replace('{q}', description)


def valid_box(box, width, height):
    """Float xyxy clipped to the image; raises ValueError for non-finite or empty boxes."""
    b = np.asarray(box, dtype=np.float32)
    if b.shape != (4,) or not np.isfinite(b).all():
        raise ValueError('Invalid box')
    b[[0, 2]] = np.clip(b[[0, 2]], 0, width - 1)
    b[[1, 3]] = np.clip(b[[1, 3]], 0, height - 1)
    if b[2] <= b[0] or b[3] <= b[1]:
        raise ValueError('Empty box')
    return b.tolist()


class Locator:
    def __init__(self):
        path = os.environ.get('LOCATE_MODEL', '/models/locate-anything-q8_0.gguf')
        if not os.path.isfile(path):
            raise FileNotFoundError(f'Missing LocateAnything model: {path}')
        self.lib = C.CDLL(os.environ['LOCATE_LIBRARY'])
        signatures = {
            'la_capi_load': ([C.c_char_p, C.c_int], C.c_void_p),
            'la_capi_free': ([C.c_void_p], None),
            'la_capi_locate_buffer': ([C.c_void_p, C.POINTER(C.c_ubyte), C.c_size_t, C.c_char_p, C.c_int], C.c_void_p),
            'la_capi_get_n_detections': ([C.c_void_p], C.c_int),
            'la_capi_get_detection_box': ([C.c_void_p, C.c_int, C.POINTER(C.c_float)], C.c_int),
            'la_capi_free_string': ([C.c_void_p], None),
            'la_capi_last_error': ([C.c_void_p], C.c_char_p),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(self.lib, name)
            fn.argtypes, fn.restype = args, result
        # Only in our patched build (deploy/patches); the upstream library has no scores.
        self.has_scores = hasattr(self.lib, 'la_capi_get_detection_score')
        if self.has_scores:
            fn = self.lib.la_capi_get_detection_score
            fn.argtypes, fn.restype = [C.c_void_p, C.c_int, C.POINTER(C.c_float)], C.c_int
        self.ctx = self.lib.la_capi_load(path.encode(), int(os.getenv('CPU_THREADS', '6')))
        if not self.ctx:
            raise RuntimeError('LocateAnything load failed; inspect Vulkan/model logs')

    def locate(self, jpeg, description):
        """-> (boxes, scores, raw JSON). scores[i] = dict(p_start, p_coord, p_object), -1 when unavailable."""
        data = (C.c_ubyte * len(jpeg)).from_buffer_copy(jpeg)
        prompt = format_prompt(description)
        mode = {'hybrid': 0, 'slow': 1, 'fast': 2}[os.getenv('LA_MODE', 'slow')]
        ptr = self.lib.la_capi_locate_buffer(self.ctx, data, len(jpeg), prompt.encode(), mode)
        if not ptr:
            raise RuntimeError(str(self.lib.la_capi_last_error(self.ctx)))
        try:
            raw = C.string_at(ptr).decode(errors='replace')
        finally:
            self.lib.la_capi_free_string(ptr)
        width, height = Image.open(io.BytesIO(jpeg)).size
        boxes, scores = [], []
        for i in range(self.lib.la_capi_get_n_detections(self.ctx)):
            box = (C.c_float * 4)()
            if self.lib.la_capi_get_detection_box(self.ctx, i, box) == 0:
                try:
                    boxes.append(valid_box(list(box), width, height))
                except ValueError:
                    continue
                prob = (C.c_float * 3)(-1, -1, -1)
                if self.has_scores:
                    self.lib.la_capi_get_detection_score(self.ctx, i, prob)
                scores.append(dict(p_start=float(prob[0]), p_coord=float(prob[1]), p_object=float(prob[2])))
        return boxes, scores, raw
