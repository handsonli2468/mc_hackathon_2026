#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
import shutil
from datetime import datetime, timezone
from pathlib import Path
import yaml

DEFAULT_FILE = Path('/mlsteam/workspace/agent-runtime/rag-knowledge/locations/site_locations.yaml')


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    dst = path.with_name(f'{path.name}.bak-{stamp}')
    shutil.copy2(path, dst)
    return dst


def main() -> int:
    ap = argparse.ArgumentParser(description='Create/update one first-class Location RAG record.')
    ap.add_argument('location_id')
    ap.add_argument('--file', type=Path, default=DEFAULT_FILE)
    ap.add_argument('--x', type=float)
    ap.add_argument('--y', type=float)
    angle = ap.add_mutually_exclusive_group()
    angle.add_argument('--yaw-deg', type=float)
    angle.add_argument('--yaw-rad', type=float)
    ap.add_argument('--map-version')
    ap.add_argument('--frame-id', default=None)
    ap.add_argument('--type', dest='location_type', default=None)
    ap.add_argument('--description')
    ap.add_argument('--alias', action='append', default=[])
    ap.add_argument('--tag', action='append', default=[])
    ap.add_argument('--region-point', action='append', default=[], metavar='X,Y', help='Polygon point for a STATIC_REGION; repeat for multiple vertices.')
    ap.add_argument('--calibrated', action='store_true')
    ap.add_argument('--uncalibrated', action='store_true')
    ap.add_argument('--enable', action='store_true')
    ap.add_argument('--disable', action='store_true')
    args = ap.parse_args()

    args.file.parent.mkdir(parents=True, exist_ok=True)
    data = yaml.safe_load(args.file.read_text(encoding='utf-8')) if args.file.exists() else {}
    if not isinstance(data, dict):
        data = {}
    locations = data.setdefault('locations', {})
    spec = locations.setdefault(args.location_id, {})
    if not isinstance(spec, dict):
        spec = {}
        locations[args.location_id] = spec

    backup = _backup(args.file)
    spec.setdefault('enabled', True)
    spec.setdefault('type', 'STATIC_LOCATION')
    spec.setdefault('frame_id', 'map')
    spec.setdefault('calibrated', False)
    spec.setdefault('aliases', [args.location_id.replace('_', ' ')])

    if args.frame_id is not None:
        spec['frame_id'] = args.frame_id
    if args.location_type is not None:
        spec['type'] = args.location_type
    if args.map_version is not None:
        spec['map_version'] = args.map_version
    if args.description is not None:
        spec['description'] = args.description
    if args.enable:
        spec['enabled'] = True
    if args.disable:
        spec['enabled'] = False
    if args.calibrated:
        spec['calibrated'] = True
    if args.uncalibrated:
        spec['calibrated'] = False

    aliases = list(spec.get('aliases') or [])
    for alias in args.alias:
        if alias and alias not in aliases:
            aliases.append(alias)
    spec['aliases'] = aliases
    tags = list(spec.get('tags') or [])
    for tag in args.tag:
        if tag and tag not in tags:
            tags.append(tag)
    if tags:
        spec['tags'] = tags

    if args.region_point:
        points=[]
        for raw in args.region_point:
            try:
                xs,ys=[x.strip() for x in str(raw).split(',',1)]
                points.append([float(xs),float(ys)])
            except Exception as exc:
                raise SystemExit(f'invalid --region-point {raw!r}; expected X,Y') from exc
        if len(points) < 3:
            raise SystemExit('STATIC_REGION polygon needs at least three --region-point values')
        spec['region']={'shape':'polygon','points':points}

    if args.x is not None or args.y is not None or args.yaw_deg is not None or args.yaw_rad is not None:
        pose_key = 'entry_pose' if str(spec.get('type') or '').upper() == 'STATIC_REGION' else 'pose'
        pose = dict(spec.get(pose_key) or {})
        if args.x is not None:
            pose['x'] = float(args.x)
        if args.y is not None:
            pose['y'] = float(args.y)
        if args.yaw_deg is not None:
            pose['yaw'] = math.radians(float(args.yaw_deg))
            spec['yaw_unit'] = 'rad'
        elif args.yaw_rad is not None:
            pose['yaw'] = float(args.yaw_rad)
            spec['yaw_unit'] = 'rad'
        spec[pose_key] = pose

    args.file.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding='utf-8')
    print(f'updated location: {args.location_id}')
    print(f'file: {args.file}')
    if backup:
        print(f'backup: {backup}')
    print(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True).rstrip())
    print('\nNext: curl -s -X POST http://127.0.0.1:8000/api/rag/sync | jq .')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
