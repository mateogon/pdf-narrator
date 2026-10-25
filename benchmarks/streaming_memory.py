"""Measure old/new writers on the same 20-minute synthetic chapter (no TTS).

Run --baseline-ref 42f4ac3 for old code, omit for working tree. Each invocation
must use a fresh process. The fixture isolates audio buffering from model RAM.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time
import types

p = argparse.ArgumentParser()
p.add_argument('--baseline-ref')
p.add_argument('--output', type=Path, required=True)
a = p.parse_args()
root = Path(__file__).resolve().parents[1]
if a.baseline_ref:
    source = subprocess.check_output(['git', 'show', f'{a.baseline_ref}:generate_audiobook_kokoro.py'], cwd=root, text=True)
    module = types.ModuleType('baseline_generation')
    exec(compile(source, 'baseline_generation.py', 'exec'), module.__dict__)
else:
    sys.path.insert(0, str(root))
    import generate_audiobook_kokoro as module
import numpy as np
import soundfile as sf

def pipeline(text, **kwargs):
    for _ in range(120):
        yield 'Synthetic narration.', '', np.full(240000, .25, dtype=np.float32)

baseline_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
start = time.perf_counter()
with tempfile.TemporaryDirectory() as directory:
    source = Path(directory)/'chapter.txt'
    output = Path(directory)/'chapter.wav'
    source.write_text('Synthetic narration.')
    assert module.generate_audio_for_file_kokoro(source, pipeline, 'af_heart', output)
    info = sf.info(output)
    assert info.frames == 120*240000
peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
scale = 2**20 if sys.platform == 'darwin' else 1024
result = {'revision': a.baseline_ref or 'working-tree', 'audio_seconds':1200,
          'elapsed_seconds':time.perf_counter()-start, 'baseline_rss_mib':baseline_rss/scale,
          'peak_rss_mib':peak/scale, 'writer_peak_increment_mib':(peak-baseline_rss)/scale}
a.output.write_text(json.dumps(result, indent=2)+'\n')
print(json.dumps(result, indent=2))
