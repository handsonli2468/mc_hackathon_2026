"""Keep native probe faults outside the server process."""
import json
import os
import subprocess
import sys


def check_gpu():
    result = subprocess.run([sys.executable, '-u', '-X', 'faulthandler', '-m', 'vlm_server.gpu_probe'],
                            capture_output=True, text=True,
                            timeout=int(os.getenv('GPU_PROBE_TIMEOUT', '180')))
    if result.returncode:
        raise RuntimeError(f'GPU preflight failed (returncode={result.returncode}). '
                           f'No CPU fallback.\n{result.stdout[-5000:]}\n{result.stderr[-5000:]}')
    lines = [line for line in result.stdout.splitlines() if line.startswith('GPU_PASS ')]
    if not lines:
        raise RuntimeError('GPU preflight returned without GPU_PASS')
    return json.loads(lines[-1][9:])
