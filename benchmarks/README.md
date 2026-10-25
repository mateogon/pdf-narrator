# Local TTS measurements

These scripts measure runtime and memory. They do not judge narration quality.
Run each measurement in a fresh process, sequentially, with the same input text.
Use material you are authorized to synthesize. Keep generated audio and model
files outside Git. The application does not depend on ONNX or Kitten.

## Application engine

```bash
uv run --locked --with psutil python benchmarks/compare_tts.py \
  --engine torch --device mps --language es \
  --text /path/to/spanish.txt --output evaluation
```

Use `--device cpu` for the CPU comparison. English uses `--language en`.
The script records model loading, warmup, synthesis including WAV writes,
output duration, package versions, text SHA-256 and sampled process RSS.
It records PyTorch MPS allocations separately when available. RSS and GPU
allocation counters are not a total physical-memory measurement.

Kokoro/Kitten may download models on first use. Preload models before measuring
cached startup. Sampled memory can miss short spikes. One run is a smoke
benchmark, not a statistical estimate. Different engines use different voices,
phonemizers and chunking; compare audio by listening as well as by time.

## Optional comparison environments

Create isolated environments, separate from the app:

```bash
uv venv --python 3.12 /tmp/pdf-narrator-onnx-eval
uv pip install --python /tmp/pdf-narrator-onnx-eval/bin/python \
  kokoro-onnx==0.6.1 onnxruntime==1.31.0 soundfile==0.14.0 psutil==7.2.2
```

Download `kokoro-v1.0.onnx` and `voices-v1.0.bin` from the upstream
[model-files-v1.1 release](https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.1)
into `models/onnx`. Run the comparison with that environment's Python,
`--engine onnx --device cpu`, and the same text/output arguments.

KittenTTS 2 on Apple Silicon was tested separately with `kittenml[mlx]==0.9.6`,
MLX 0.32.4, soundfile 0.14.0 and psutil 7.2.2. Use `--engine kitten --device mlx`.
Its [model weights license](https://huggingface.co/KittenML/kitten-tts-2/blob/main/LICENSE.md)
differs from its code license. The script uses preset voices, not user recordings.

## Writer memory regression

```bash
uv run --locked python benchmarks/streaming_memory.py \
  --baseline-ref 42f4ac3 --output evaluation/memory-before.json
uv run --locked python benchmarks/streaming_memory.py \
  --output evaluation/memory-after.json
```

This uses the same synthetic 20-minute waveform for both writers, without
loading a TTS model. Peak RSS includes module imports; an incremental value
of zero means the writer stayed below the prior high-water mark. It does not
mean the writer allocates no memory. Original and new amplitude policies differ:
the original normalized the full chapter; the new writer clips each chunk.
