"""LocateAnything boxes, optionally refined by a single-image SAM-2 mask."""
import io
import math
import os
import time
from contextlib import nullcontext
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

from .locator import valid_box


@dataclass
class Detection:
    bbox: list                 # [x1, y1, x2, y2] int, x2/y2 exclusive
    score: float = -1.0        # Locate confidence (VLM_SCORE_FIELD); -1 when the library has no scores
    num_candidates: int = 1
    mask_png: bytes = None     # single-channel 0/255 PNG cropped to bbox
    timings: dict = field(default_factory=dict)
    sam_score: float = -1.0    # SAM predicted IoU; -1 when no mask was computed


# p_object = 1 - P(<none>) where the box's first coordinate was chosen: the model's
# "box" vs "no such object" decision. p_start is ~1 even for <box><none></box> answers.
SCORE_FIELDS = ('p_object', 'p_coord', 'p_start')


def rank_candidates(boxes, scores, min_score=0.0, score_field='p_object'):
    """-> [(box, score)] with score >= min_score, highest first; ties keep Locate's order.

    min_score <= 0 disables the filter, so unscored boxes (-1) pass through unchanged.
    """
    ranked = [(box, s[score_field]) for box, s in zip(boxes, scores)]
    if min_score > 0:
        ranked = [(box, score) for box, score in ranked if score >= min_score]
    return sorted(ranked, key=lambda item: -item[1])


def to_pixel_box(box, width, height):
    """Float xyxy -> int xyxy covering the box, x2/y2 exclusive, clipped to the image."""
    x1, y1, x2, y2 = valid_box(box, width, height)
    return [int(math.floor(x1)), int(math.floor(y1)),
            min(width, int(math.floor(x2)) + 1), min(height, int(math.floor(y2)) + 1)]


def encode_mask_crop(mask, bbox):
    x1, y1, x2, y2 = bbox
    crop = np.where(mask[y1:y2, x1:x2], 255, 0).astype(np.uint8)
    stream = io.BytesIO()
    Image.fromarray(crop, mode='L').save(stream, format='PNG')
    return stream.getvalue()


class MaskPredictor:
    """SAM-2.1 image predictor; no video memory, every request is independent."""
    def __init__(self):
        import torch
        from sam2.build_sam import build_sam2
        from sam2.sam2_image_predictor import SAM2ImagePredictor
        self.torch = torch
        self.device = os.getenv('SAM_DEVICE', 'cpu')
        if self.device not in ('cpu', 'cuda'):
            raise ValueError('SAM_DEVICE must be cpu or cuda (HIP uses cuda API)')
        if self.device == 'cuda':
            from .gpu_check import check_gpu
            check_gpu()
        torch.set_num_threads(int(os.getenv('CPU_THREADS', '6')))
        ckpt = os.getenv('SAM_CHECKPOINT', '/models/sam2.1_hiera_tiny.pt')
        if not os.path.isfile(ckpt):
            raise FileNotFoundError(f'Missing SAM checkpoint: {ckpt}')
        size = int(os.getenv('VLM_SAM_IMAGE_SIZE', '1024'))
        if size not in (384, 512, 768, 1024):
            raise ValueError('VLM_SAM_IMAGE_SIZE must be 384, 512, 768 or 1024')
        model = build_sam2('configs/sam2.1/sam2.1_hiera_t.yaml', ckpt, device=self.device,
                           apply_postprocessing=False,
                           hydra_overrides_extra=[f'++model.image_size={size}'])
        self.predictor = SAM2ImagePredictor(model)
        # The predictor hardcodes backbone feature sizes for 1024 input.
        self.predictor._bb_feat_sizes = [(size // 4,) * 2, (size // 8,) * 2, (size // 16,) * 2]
        self.image_size = size

    def context(self):
        return self.torch.autocast('cuda', dtype=self.torch.float16) if self.device == 'cuda' else nullcontext()

    def best(self, rgb, boxes):
        """-> (index of best box, bool mask HxW, score)."""
        with self.torch.inference_mode(), self.context():
            self.predictor.set_image(rgb)
            masks, scores, _ = self.predictor.predict(box=np.asarray(boxes, dtype=np.float32),
                                                      multimask_output=False)
        masks = np.asarray(masks).reshape(len(boxes), *rgb.shape[:2])
        scores = np.asarray(scores, dtype=np.float32).reshape(len(boxes))
        index = int(np.argmax(scores))
        return index, masks[index] > 0, float(scores[index])


class TargetFinder:
    def __init__(self, return_mask=False):
        from .locator import Locator
        self.min_score = float(os.getenv('VLM_MIN_SCORE', '0'))
        self.score_field = os.getenv('VLM_SCORE_FIELD', 'p_object')
        if self.score_field not in SCORE_FIELDS:
            raise ValueError(f'VLM_SCORE_FIELD must be one of {SCORE_FIELDS}')
        self.locator = Locator()
        if self.min_score > 0 and not self.locator.has_scores:
            # Every box would score -1 and the server would only ever answer NOT_FOUND.
            raise RuntimeError('VLM_MIN_SCORE needs the patched locate-anything build (LA_PATCH=1)')
        self.masker = MaskPredictor() if return_mask else None

    @property
    def info(self):
        return dict(return_mask=self.masker is not None,
                    sam_image_size=self.masker.image_size if self.masker else None,
                    la_mode=os.getenv('LA_MODE', 'slow'),
                    prompt_template=os.getenv('LA_PROMPT_TEMPLATE') or 'detect',
                    custom_system_prompt=bool(os.getenv('LA_SYSTEM_PROMPT')),
                    has_scores=self.locator.has_scores,
                    score_field=self.score_field, min_score=self.min_score)

    def find(self, jpeg, text):
        """-> Detection, or None when Locate returns no valid box."""
        # Locate segfaults on huge inputs (a 4000x2700 image tried a 41 GB buffer),
        # which would take the whole server down; reject before calling it.
        limit = int(os.getenv('VLM_MAX_INPUT_SIDE', '1280'))
        if max(Image.open(io.BytesIO(jpeg)).size) > limit:
            raise ValueError(f'image larger than {limit}px; downscale before upload')
        timings = {}
        start = time.monotonic()
        boxes, scores, _raw = self.locator.locate(jpeg, text)
        timings['locate_ms'] = (time.monotonic() - start) * 1000
        ranked = rank_candidates(boxes, scores, self.min_score, self.score_field)
        if not ranked:
            return None
        image = Image.open(io.BytesIO(jpeg)).convert('RGB')
        width, height = image.size
        if self.masker is None:
            box, score = ranked[0]
            return Detection(to_pixel_box(box, width, height), score, len(ranked), timings=timings)
        start = time.monotonic()
        index, mask, sam_score = self.masker.best(np.asarray(image), [box for box, _ in ranked])
        timings['sam_ms'] = (time.monotonic() - start) * 1000
        box, score = ranked[index]
        bbox = to_pixel_box(box, width, height)
        return Detection(bbox, score, len(ranked), encode_mask_crop(mask, bbox), timings, sam_score)
