#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
from datetime import datetime, timezone
from pathlib import Path

import yaml

DEFAULT_FILE = Path('/mlsteam/workspace/agent-runtime/rag-knowledge/scene/site_scene.yaml')


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    dst = path.with_name(f'{path.name}.bak-{stamp}')
    shutil.copy2(path, dst)
    return dst


def _dedupe(values):
    out=[]
    for value in values:
        text=str(value).strip()
        if text and text not in out:
            out.append(text)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description='Create/update one Scene RAG knowledge document.')
    ap.add_argument('scene_id')
    ap.add_argument('--file', type=Path, default=DEFAULT_FILE)
    ap.add_argument('--title')
    ap.add_argument('--knowledge-type', default=None)
    ap.add_argument('--content')
    ap.add_argument('--tag', action='append', default=[])
    ap.add_argument('--target-term', action='append', default=[])
    ap.add_argument('--object-category', action='append', default=[])
    ap.add_argument('--location-id', action='append', default=[])
    ap.add_argument('--confidence', type=float)
    ap.add_argument('--requires-visual-verification', action='store_true')
    ap.add_argument('--no-visual-verification', action='store_true')
    ap.add_argument('--enable', action='store_true')
    ap.add_argument('--disable', action='store_true')
    args = ap.parse_args()

    args.file.parent.mkdir(parents=True, exist_ok=True)
    data = yaml.safe_load(args.file.read_text(encoding='utf-8')) if args.file.exists() else {}
    if not isinstance(data, dict):
        data={}
    docs=data.setdefault('documents', [])
    if not isinstance(docs, list):
        raise SystemExit('documents must be a YAML list')
    doc=next((x for x in docs if isinstance(x,dict) and str(x.get('id'))==args.scene_id), None)
    if doc is None:
        doc={'id':args.scene_id,'enabled':True,'knowledge_type':'LOCATION_PRIOR'}
        docs.append(doc)

    backup=_backup(args.file)
    if args.title is not None: doc['title']=args.title
    else: doc.setdefault('title', args.scene_id.replace('_',' ').title())
    if args.knowledge_type is not None: doc['knowledge_type']=args.knowledge_type
    if args.content is not None: doc['content']=args.content
    if args.confidence is not None:
        if not 0.0 <= args.confidence <= 1.0:
            raise SystemExit('--confidence must be between 0 and 1')
        doc['confidence']=float(args.confidence)
    if args.enable: doc['enabled']=True
    if args.disable: doc['enabled']=False
    if args.requires_visual_verification: doc['requires_visual_verification']=True
    if args.no_visual_verification: doc['requires_visual_verification']=False

    for key, additions in (
        ('tags', args.tag),
        ('target_terms', args.target_term),
        ('object_categories', args.object_category),
        ('location_ids', args.location_id),
    ):
        values=_dedupe([*(doc.get(key) or []), *additions])
        if values: doc[key]=values

    args.file.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding='utf-8')
    print(f'updated scene knowledge: {args.scene_id}')
    print(f'file: {args.file}')
    if backup: print(f'backup: {backup}')
    print(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True).rstrip())
    print('\nNext: curl -s -X POST http://127.0.0.1:8000/api/rag/sync | jq .')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
