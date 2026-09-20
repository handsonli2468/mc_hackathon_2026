"""Evaluate Locate settings on the labelled eval set (TODO.md sections 3-5).

Runs inside the server image; the model is loaded once and each sweep config only
changes environment variables that are read per call (LA_MODE, LA_PROMPT_TEMPLATE,
LA_SYSTEM_PROMPT) plus the upload width.

  # Draft labels: slow-mode boxes + numbered overlays, then edit eval/cases.yaml by hand
  python -m scripts.eval_locate --propose --images /app/eval/images --out /output/eval/propose
  # Sweep
  python -m scripts.eval_locate --cases /app/eval/cases.yaml --sweep /app/eval/sweeps/round0.yaml --out /output/eval/round0
  # Re-summarize an existing run without the model (works locally too)
  python -m scripts.eval_locate --summarize output/eval/round0/results.csv
"""
import argparse
import csv
import io
import json
import os
import statistics
import time
from pathlib import Path

from PIL import Image, ImageDraw

IMAGE_SUFFIXES = ('.png', '.jpg', '.jpeg')
SWEEP_KEYS = ('name', 'template', 'system_prompt', 'mode', 'width', 'query_suffix')
THRESHOLDS = [0.0, 0.3, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95]


def iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def judge(expected, boxes, scores, threshold=0.0, iou_min=0.5):
    """Outcome of one case at one score threshold, using the top-scoring kept box like the server.

    expected None (target absent): 'reject' (correct NOT_FOUND) or 'false_positive'.
    expected box: 'hit', 'wrong_box' (answered, IoU below iou_min) or 'miss' (NOT_FOUND).
    Unscored boxes (-1) only pass threshold 0, matching VLM_MIN_SCORE semantics.
    """
    kept = [(box, score) for box, score in zip(boxes, scores) if threshold <= 0 or score >= threshold]
    if expected is None:
        return 'false_positive' if kept else 'reject'
    if not kept:
        return 'miss'
    best = max(kept, key=lambda item: item[1])[0]  # max keeps the first box on ties
    return 'hit' if iou(best, expected) >= iou_min else 'wrong_box'


def summarize(rows, score_field='p_object', thresholds=THRESHOLDS):
    """rows: result dicts (parsed CSV). -> list of summary dicts per (config, threshold)."""
    out = []
    configs = list(dict.fromkeys(row['config'] for row in rows))
    for config in configs:
        group = [row for row in rows if row['config'] == config]
        present = [row for row in group if row['expected'] is not None]
        absent = [row for row in group if row['expected'] is None]
        ms = statistics.median(row['locate_ms'] for row in group) if group else 0
        for threshold in thresholds:
            outcomes = [judge(row['expected'], row['boxes'], [s[score_field] for s in row['scores']], threshold)
                        for row in group]
            hits = sum(o == 'hit' for o in outcomes)
            fps = sum(o == 'false_positive' for o in outcomes)
            out.append(dict(config=config, threshold=threshold,
                            hit_rate=round(hits / len(present), 3) if present else None,
                            false_positive_rate=round(fps / len(absent), 3) if absent else None,
                            wrong_box=sum(o == 'wrong_box' for o in outcomes),
                            miss=sum(o == 'miss' for o in outcomes),
                            n_present=len(present), n_absent=len(absent), locate_ms_p50=round(ms)))
    return out


def upload_jpeg(image, width, quality=80):
    """Mimic the client: downscale to `width` (area filter, like cv2.INTER_AREA) and encode JPEG. -> (bytes, scale)."""
    scale = 1.0
    if width and image.width > width:
        scale = width / image.width
        image = image.resize((width, round(image.height * scale)), Image.Resampling.BOX)
    stream = io.BytesIO()
    image.save(stream, format='JPEG', quality=quality)
    return stream.getvalue(), scale


def scale_box(box, scale):
    return None if box is None else [v * scale for v in box]


def load_cases(path):
    """eval/cases.yaml -> [(image path, description, expected box or None)]."""
    import yaml
    path = Path(path)
    cases = []
    for entry in yaml.safe_load(path.read_text()) or []:
        image = path.parent / 'images' / entry['image']
        for query in entry['queries']:
            expected = query['expect']
            if expected != 'none' and (not isinstance(expected, list) or len(expected) != 4):
                raise ValueError(f'{entry["image"]}: expect must be none or [x1, y1, x2, y2]')
            cases.append((image, query['text'], None if expected == 'none' else [float(v) for v in expected]))
    return cases


def load_sweep(path):
    import yaml
    configs = yaml.safe_load(Path(path).read_text())
    for config in configs:
        unknown = set(config) - set(SWEEP_KEYS)
        if unknown or 'name' not in config:
            raise ValueError(f'sweep entry {config}: needs name, allowed keys {SWEEP_KEYS}')
    return configs


def apply_config(config):
    os.environ['LA_MODE'] = config.get('mode', 'fast')
    os.environ['LA_PROMPT_TEMPLATE'] = config.get('template', 'detect')
    if config.get('system_prompt'):
        os.environ['LA_SYSTEM_PROMPT'] = config['system_prompt']
    else:
        os.environ.pop('LA_SYSTEM_PROMPT', None)


def draw(image, boxes, labels, expected=None):
    overlay = image.convert('RGB')
    pen = ImageDraw.Draw(overlay)
    if expected:
        pen.rectangle(expected, outline=(40, 235, 125), width=3)
    for box, label in zip(boxes, labels):
        pen.rectangle(box, outline=(255, 220, 0), width=2)
        pen.text((box[0] + 3, box[1] + 2), label, fill=(255, 220, 0))
    return overlay


def run_sweep(args):
    from vlm_server.locator import Locator
    cases, configs = load_cases(args.cases), load_sweep(args.sweep)
    if not cases:
        raise SystemExit(f'{args.cases} has no cases; label images first (see --propose)')
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    locator = Locator()
    print(json.dumps(dict(cases=len(cases), configs=len(configs), has_scores=locator.has_scores)), flush=True)
    images = {path: Image.open(path) for path in dict.fromkeys(case[0] for case in cases)}
    rows = []
    with open(out / 'results.csv', 'w', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['config', 'image', 'text', 'expected', 'boxes', 'scores', 'locate_ms', 'width'])
        for config in configs:
            apply_config(config)
            locator.locate(upload_jpeg(images[cases[0][0]], config.get('width', 640))[0], 'warm up')
            for image_path, text, expected in cases:
                jpeg, scale = upload_jpeg(images[image_path], config.get('width', 640))
                start = time.monotonic()
                boxes, scores, _raw = locator.locate(jpeg, text + config.get('query_suffix', ''))
                elapsed = (time.monotonic() - start) * 1000
                boxes = [scale_box(box, 1 / scale) for box in boxes]  # back to original pixels
                row = dict(config=config['name'], image=image_path.name, text=text, expected=expected,
                           boxes=boxes, scores=scores, locate_ms=elapsed)
                rows.append(row)
                writer.writerow([row['config'], row['image'], text, json.dumps(expected), json.dumps(boxes),
                                 json.dumps(scores), round(elapsed, 1), config.get('width', 640)])
                stream.flush()
                if args.overlay:
                    labels = [f'{i}:{s["p_object"]:.2f}' for i, s in enumerate(scores)]
                    name = f'{config["name"]}__{image_path.stem}__{text.replace(" ", "_")[:40]}.jpg'
                    (out / 'overlay').mkdir(exist_ok=True)
                    draw(images[image_path], boxes, labels, expected).save(out / 'overlay' / name, quality=85)
                print(json.dumps(dict(config=config['name'], image=image_path.name, text=text,
                                      n=len(boxes), ms=round(elapsed))), flush=True)
    write_summary(rows, out)


def run_propose(args):
    """Slow-mode boxes for every image and query, as a cases.yaml draft plus numbered overlays."""
    from vlm_server.locator import Locator
    os.environ['LA_MODE'] = 'slow'
    out = Path(args.out)
    (out / 'overlay').mkdir(parents=True, exist_ok=True)
    locator = Locator()
    queries = args.query or ['the paper cup']
    lines = ['# Draft from --propose: keep the right box, or write none. Coordinates are original pixels.']
    for path in sorted(p for p in Path(args.images).iterdir() if p.suffix.lower() in IMAGE_SUFFIXES):
        image = Image.open(path)
        jpeg, scale = upload_jpeg(image, 640)
        lines += [f'- image: {path.name}', '  queries:']
        for text in queries:
            boxes, scores, _raw = locator.locate(jpeg, text)
            boxes = [[round(v) for v in scale_box(box, 1 / scale)] for box in boxes]
            lines.append(f'    - text: {text}')
            lines.append(f'      expect: {boxes[0] if boxes else "none"}' +
                         (f'  # candidates: {boxes}' if len(boxes) > 1 else ''))
            if scores:
                lines.append('      # scores (p_object, p_coord, p_start): ' +
                             ', '.join(f'({s["p_object"]:.3f}, {s["p_coord"]:.3f}, {s["p_start"]:.3f})' for s in scores))
            draw(image, boxes, [str(i) for i in range(len(boxes))]).save(
                out / 'overlay' / f'{path.stem}__{text.replace(" ", "_")[:40]}.jpg', quality=85)
        print(path.name, flush=True)
    (out / 'cases.draft.yaml').write_text('\n'.join(lines) + '\n')
    print(f'wrote {out / "cases.draft.yaml"}')


def read_results(path):
    rows = []
    with open(path, newline='') as stream:
        for row in csv.DictReader(stream):
            rows.append(dict(config=row['config'], image=row['image'], text=row['text'],
                             expected=json.loads(row['expected']), boxes=json.loads(row['boxes']),
                             scores=json.loads(row['scores']), locate_ms=float(row['locate_ms'])))
    return rows


def write_summary(rows, out, score_fields=('p_object', 'p_coord', 'p_start')):
    for field in score_fields:
        summary = summarize(rows, field)
        with open(Path(out) / f'summary_{field}.csv', 'w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(summary[0]))
            writer.writeheader()
            writer.writerows(summary)
    for row in summarize(rows, 'p_object', [0.0]):  # unfiltered view for the console
        print(json.dumps(row))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--cases', default='/app/eval/cases.yaml')
    parser.add_argument('--sweep', help='eval/sweeps/*.yaml')
    parser.add_argument('--out', default='/output/eval/run')
    parser.add_argument('--overlay', action='store_true', help='save one overlay per config and case')
    parser.add_argument('--propose', action='store_true', help='draft labels instead of a sweep')
    parser.add_argument('--images', default='/app/eval/images', help='--propose: image folder')
    parser.add_argument('--query', action='append', help='--propose: description, repeatable')
    parser.add_argument('--summarize', metavar='RESULTS_CSV', help='only rebuild summaries from a results.csv')
    args = parser.parse_args()
    if args.summarize:
        write_summary(read_results(args.summarize), Path(args.summarize).parent)
    elif args.propose:
        run_propose(args)
    elif args.sweep:
        run_sweep(args)
    else:
        parser.error('give --sweep, --propose or --summarize')


if __name__ == '__main__':
    main()
