"""Compare Kokoro, ONNX and Kitten in isolated processes.

Run with each engine's own interpreter. Kokoro and Kitten may fetch model files;
ONNX requires the model files specified by --model-dir. No packages are installed.
Texts are public-domain excerpts from Alice in Wonderland and Don Quixote,
with punctuation/typography simplified. Results measure implementation paths,
including their different phonemizers; they are not an audio-quality verdict.
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import threading
import time

os.environ.setdefault('PYTORCH_ENABLE_MPS_FALLBACK', '1')
p = argparse.ArgumentParser()
p.add_argument('--engine', choices=['torch', 'onnx', 'kitten'], required=True)
p.add_argument('--device', choices=['cpu', 'mps', 'mlx'], default='cpu')
p.add_argument('--language', choices=['en', 'es'], required=True)
p.add_argument('--output', type=Path, required=True)
p.add_argument('--text', type=Path, required=True)
p.add_argument('--model-dir', type=Path, default=Path('models/onnx'))
args = p.parse_args()
if args.engine == 'torch' and args.device == 'mlx':
    p.error('Torch uses cpu or mps, not mlx.')
if args.engine == 'onnx' and args.device != 'cpu':
    p.error('This ONNX comparison uses CPUExecutionProvider.')
import numpy as np
import psutil
import soundfile as sf
process = psutil.Process()
peak_rss = 0
peak_mps = 0
stop = threading.Event()

def sample_memory():
    global peak_rss, peak_mps
    while not stop.wait(0.01):
        peak_rss = max(peak_rss, process.memory_info().rss)
        if args.engine == 'torch' and args.device == 'mps' and 'torch' in globals():
            peak_mps = max(peak_mps, torch.mps.current_allocated_memory())

text = args.text.read_text(encoding='utf-8')
voice = 'af_heart' if args.language == 'en' else 'ef_dora'
lang = 'a' if args.language == 'en' else 'e'
monitor = threading.Thread(target=sample_memory, daemon=True)
monitor.start()
load_start = time.perf_counter()
if args.engine == 'torch':
    import torch
    from kokoro import KPipeline
    model = KPipeline(lang_code=lang, device=args.device, repo_id='hexgrad/Kokoro-82M')
    def synth(text):
        for _, _, audio in model(text, voice=voice, speed=1):
            yield audio.detach().cpu().numpy(), 24000
    providers = [args.device]
elif args.engine == 'onnx':
    from kokoro_onnx import Kokoro
    model = Kokoro(str(args.model_dir / 'kokoro-v1.0.onnx'), str(args.model_dir / 'voices-v1.0.bin'))
    def synth(text):
        for paragraph in text.splitlines():
            if paragraph.strip():
                yield model.create(paragraph, voice=voice, lang='en-us' if lang == 'a' else 'es')
    providers = model.sess.get_providers() if hasattr(model, 'sess') else model.session.get_providers()
else:
    if args.device != 'mlx':
        raise ValueError('Kitten comparison currently uses the Apple MLX backend.')
    from kittenml import KittenTTS
    model = KittenTTS('KittenML/kitten-tts-2', backend='mlx')
    voice = 'Bella' if lang == 'a' else 'Spanish'
    def synth(text):
        for audio in model.generate_stream(text, voice=voice, normalize=False, advanced={'seed': 42}):
            yield audio, model.sample_rate
    providers = ['MLX', 'MPS decoder']
load_seconds = time.perf_counter() - load_start
warmup_start = time.perf_counter()
list(synth('A quiet morning.' if lang == 'a' else 'Una mañana tranquila.'))
warmup_seconds = time.perf_counter() - warmup_start
args.output.mkdir(parents=True, exist_ok=True)
name = f'{args.engine}-{args.device}-{args.language}'
frames = 0
start = time.perf_counter()
try:
    with sf.SoundFile(args.output / (name + '.wav'), 'w', samplerate=24000, channels=1, subtype='PCM_16') as output:
        for audio, rate in synth(text):
            assert rate == 24000 and np.isfinite(audio).all() and audio.size > 0
            output.write(np.clip(audio, -1, 1) * .95)
            frames += len(audio)
finally:
    stop.set()
    monitor.join()
elapsed = time.perf_counter() - start
result = dict(text_sha256=hashlib.sha256(text.encode()).hexdigest(), words=len(text.split()), engine=args.engine, device=args.device, language=args.language,
              voice=voice, providers=providers, load_seconds=load_seconds,
              warmup_seconds=warmup_seconds, synthesis_seconds=elapsed,
              audio_seconds=frames/24000, realtime_multiplier=frames/24000/elapsed,
              peak_rss_mib=peak_rss/2**20, peak_mps_allocated_mib=peak_mps/2**20 if args.engine == 'torch' and args.device == 'mps' else None,
              python=platform.python_version(), platform=platform.platform(),
              package_version=importlib.metadata.version({'torch':'kokoro', 'onnx':'kokoro-onnx', 'kitten':'kittenml'}[args.engine]),
              runtime_version=importlib.metadata.version({'torch':'torch', 'onnx':'onnxruntime', 'kitten':'mlx'}[args.engine]))
(args.output / (name + '.json')).write_text(json.dumps(result, indent=2)+'\n')
print(json.dumps(result, indent=2))
