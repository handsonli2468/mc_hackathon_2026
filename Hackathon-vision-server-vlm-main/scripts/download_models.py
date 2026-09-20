"""Run in Docker; existing files are never overwritten."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import urllib.request

parser = argparse.ArgumentParser()
parser.add_argument('--with-locate', action='store_true', help='Also download the ~6.3GB Q8_0 model')
args = parser.parse_args()
targets = [('sam2.1_hiera_tiny.pt', 'https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_tiny.pt')]
if args.with_locate:
    targets.append(('locate-anything-q8_0.gguf', 'https://huggingface.co/mudler/locate-anything.cpp-gguf/resolve/main/locate-anything-q8_0.gguf'))
root = Path('/models')
root.mkdir(parents=True, exist_ok=True)
for name, url in targets:
    path = root / name
    if path.exists():
        print(f'Keep existing {path} ({path.stat().st_size} bytes)', flush=True)
        continue
    temporary = path.with_suffix(path.suffix + '.partial')
    print(f'Downloading {url}', flush=True)
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=120) as response, temporary.open('wb') as stream:
        size = 0
        while chunk := response.read(8 * 1024 * 1024):
            stream.write(chunk)
            digest.update(chunk)
            size += len(chunk)
            print(f'{name}: {size / 1024**2:.0f} MiB', flush=True)
        expected = response.headers.get('Content-Length')
        if expected and size != int(expected):
            raise RuntimeError('Incomplete download')
    if size < 1_000_000:
        raise RuntimeError('Unexpected model size')
    temporary.replace(path)
    path.with_suffix(path.suffix + '.download.json').write_text(json.dumps(
        dict(url=url, bytes=size, sha256=digest.hexdigest()), indent=2))
    print(f'Saved {path}; SHA256 recorded (not an upstream checksum verification)')
