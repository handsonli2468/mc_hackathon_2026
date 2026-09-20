#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import yaml

DEFAULT_ROOT=Path('/mlsteam/workspace/agent-runtime/rag-knowledge')


def main() -> int:
    ap=argparse.ArgumentParser(description='Validate typed Scene/Location RAG references and basic location records.')
    ap.add_argument('--root',type=Path,default=DEFAULT_ROOT)
    args=ap.parse_args()
    errors=[]; warnings=[]; locations={}; aliases={}
    for p in sorted((args.root/'locations').glob('*.yaml')) if (args.root/'locations').exists() else []:
        data=yaml.safe_load(p.read_text(encoding='utf-8')) or {}; raw=data.get('locations',data)
        if not isinstance(raw,dict):
            errors.append(f'{p}: locations must be a mapping'); continue
        for loc_id,spec in raw.items():
            if not isinstance(spec,dict): errors.append(f'{p}:{loc_id}: record must be mapping'); continue
            if loc_id in locations: errors.append(f'duplicate location_id {loc_id}: {locations[loc_id]} and {p}')
            locations[loc_id]=p
            if spec.get('enabled',True) is False: continue
            frame=str(spec.get('frame_id') or '')
            if not frame: errors.append(f'{p}:{loc_id}: missing frame_id')
            pose=spec.get('entry_pose') if isinstance(spec.get('entry_pose'),dict) else spec.get('pose')
            if not isinstance(pose,dict) or 'x' not in pose or 'y' not in pose:
                errors.append(f'{p}:{loc_id}: enabled executable location needs pose/entry_pose x,y')
            for alias in [loc_id, loc_id.replace('_',' '), *(spec.get('aliases') or [])]:
                key=' '.join(str(alias).casefold().replace('_',' ').replace('-',' ').split())
                if not key: continue
                prev=aliases.get(key)
                if prev and prev != loc_id: warnings.append(f'alias {alias!r} is shared by {prev} and {loc_id}')
                aliases[key]=loc_id
    for p in sorted((args.root/'scene').glob('*.yaml')) if (args.root/'scene').exists() else []:
        data=yaml.safe_load(p.read_text(encoding='utf-8')) or {}
        for doc in data.get('documents',[]) or []:
            if not isinstance(doc,dict) or doc.get('enabled',True) is False: continue
            did=str(doc.get('id') or '<missing-id>')
            if not doc.get('id'): errors.append(f'{p}: scene document missing id')
            for loc_id in doc.get('location_ids',[]) or doc.get('related_locations',[]) or []:
                if str(loc_id) not in locations:
                    errors.append(f'{p}:{did}: references unknown location_id {loc_id}')
    print(f'locations={len(locations)} aliases={len(aliases)}')
    for w in warnings: print('WARNING:',w)
    for e in errors: print('ERROR:',e)
    if errors: return 2
    print('typed RAG validation: OK')
    return 0

if __name__=='__main__':
    raise SystemExit(main())
