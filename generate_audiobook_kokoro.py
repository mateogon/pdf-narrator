"""Shared Kokoro generation for the CLI and desktop app."""

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path

# Torch reads this at import time. Unsupported MPS operators may run on CPU.
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

import numpy as np
import soundfile as sf
import torch
from kokoro import KPipeline

DEFAULT_SAMPLE_RATE = 24000
MODEL_REPO = "hexgrad/Kokoro-82M"
DEVICE = "auto"


def available_voices():
    """Return the hard-coded list of available Kokoro voice identifiers."""
    # This list should ideally be kept up-to-date with Kokoro's supported voices
    return [
        "af_heart", "af_alloy", "af_aoede", "af_bella", "af_jessica", "af_kore", "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
        "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael", "am_onyx", "am_puck", "am_santa",
        "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
        "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
        "jf_alpha", "jf_gongitsune", "jf_nezumi", "jf_tebukuro", "jm_kumo",
        "zf_xiaobei", "zf_xiaoni", "zf_xiaoxiao", "zf_xiaoyi", "zm_yunjian", "zm_yunxi", "zm_yunxia", "zm_yunyang",
        "ef_dora", "em_alex", "em_santa",
        "ff_siwis",
        "hf_alpha", "hf_beta", "hm_omega", "hm_psi",
        "if_sara", "im_nicola",
        "pf_dora", "pm_alex", "pm_santa"
    ]


def available_devices():
    """Return usable backends; CPU is always available."""
    devices = ["cpu"]
    if torch.cuda.is_available():
        devices.append("cuda")
    if torch.backends.mps.is_available():
        devices.append("mps")
    return devices


def resolve_device(device="auto"):
    """Resolve auto, or fall back to CPU for an unavailable accelerator."""
    if device not in {"auto", "cpu", "cuda", "mps"}:
        raise ValueError(f"Unknown device: {device}")
    devices = available_devices()
    if device == "auto":
        return next((name for name in ("cuda", "mps", "cpu") if name in devices))
    if device not in devices:
        print(f"Warning: {device.upper()} is unavailable; using CPU.")
        return "cpu"
    return device


def _pipeline(lang_code, device):
    return KPipeline(lang_code=lang_code, device=resolve_device(device), repo_id=MODEL_REPO)


def _raise_if_cancelled(cancellation_flag):
    if cancellation_flag and cancellation_flag():
        raise InterruptedError("Processing cancelled by user.")


def _checkpoint(cancellation_flag, pause_event):
    _raise_if_cancelled(cancellation_flag)
    if pause_event is not None:
        while not pause_event.wait(0.1):
            _raise_if_cancelled(cancellation_flag)
    _raise_if_cancelled(cancellation_flag)


def _spoken_text(text):
    # Keep Unicode letters/digits; underscores and punctuation are not speech.
    return bool(re.search(r"[^\W_]", text or "", re.UNICODE))


def _audio_array(audio):
    if audio is None:
        return None
    if isinstance(audio, torch.Tensor):
        audio = audio.detach().to(device="cpu", dtype=torch.float32).numpy()
    audio = np.asarray(audio, dtype=np.float32)
    if audio.size == 0:
        return None
    if audio.ndim != 1 or not np.isfinite(audio).all():
        raise ValueError("Kokoro returned non-finite or non-mono audio.")
    # Kokoro produces [-1, 1] floats; fixed clipping avoids chapter-wide copies.
    return np.clip(audio, -1.0, 1.0)


def _stream_audio(text, pipeline, voice, writer, speed, split_pattern,
                  cancellation_flag, chunk_progress_callback, pause_event):
    frames = 0
    last_chunk = time.monotonic()
    _checkpoint(cancellation_flag, pause_event)
    for graphemes, _, audio in pipeline(text, voice=voice, speed=speed, split_pattern=split_pattern):
        _checkpoint(cancellation_flag, pause_event)
        if not _spoken_text(graphemes):
            continue
        samples = _audio_array(audio)
        if samples is None:
            raise ValueError("Kokoro returned empty audio for spoken text.")
        writer.write(samples)
        frames += len(samples)
        now = time.monotonic()
        if chunk_progress_callback:
            chunk_progress_callback(len(graphemes), now - last_chunk)
        last_chunk = now
    _checkpoint(cancellation_flag, pause_event)
    if not frames:
        raise ValueError("Kokoro produced no spoken audio.")


def generate_audio_for_file_kokoro(input_path, pipeline, voice, output_path,
                                   speed=1.0, split_pattern=r'\n+',
                                   cancellation_flag=None, chunk_progress_callback=None,
                                   pause_event=None):
    """Stream one chapter atomically. Failure preserves any previous output.

    Returns False for synthesis/write failures. Cancellation raises InterruptedError.
    """
    temporary = None
    try:
        text = Path(input_path).read_text(encoding="utf-8")
        text = "\n".join(line for line in text.splitlines() if _spoken_text(line))
        if not _spoken_text(text):
            raise ValueError("Input contains no letters or digits.")
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=destination.suffix, delete=False) as handle:
            temporary = Path(handle.name)
        with sf.SoundFile(temporary, mode="w", samplerate=DEFAULT_SAMPLE_RATE, channels=1) as writer:
            _stream_audio(text, pipeline, voice, writer, speed, split_pattern,
                          cancellation_flag, chunk_progress_callback, pause_event)
        _checkpoint(cancellation_flag, pause_event)
        os.replace(temporary, destination)
        return True
    except InterruptedError:
        raise
    except Exception as error:
        print(f"Failed to generate {output_path}: {error}")
        return False
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _resume_record(input_path, settings):
    return {"version": 1, "source_sha256": _sha256(input_path), "settings": settings}


def _can_resume(output_path, record):
    try:
        stored = json.loads(Path(str(output_path) + ".resume.json").read_text(encoding="utf-8"))
        expected = dict(record, audio_sha256=_sha256(output_path))
        info = sf.info(output_path)
        return stored == expected and info.frames > 0 and info.samplerate == DEFAULT_SAMPLE_RATE
    except (OSError, ValueError, RuntimeError):
        return False


def _save_resume(output_path, record):
    sidecar = Path(str(output_path) + ".resume.json")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=sidecar.parent, encoding="utf-8", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(dict(record, audio_sha256=_sha256(output_path)), handle)
        os.replace(temporary, sidecar)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _chapter(input_path, output_path, pipeline, voice, settings, resume,
             cancellation_flag, pause_event, chunk_callback):
    record = _resume_record(input_path, settings)
    if resume and _can_resume(output_path, record):
        print(f"Resuming: verified {Path(output_path).name}")
        return
    success = generate_audio_for_file_kokoro(
        input_path, pipeline, voice, output_path, settings["speed"], settings["split_pattern"],
        cancellation_flag, chunk_callback, pause_event)
    if not success:
        raise RuntimeError(f"Audio generation failed: {input_path}")
    _save_resume(output_path, record)


def _chapter_progress(callback, input_path, index, total):
    if callback is None:
        return None
    total_chars = max(len(input_path.read_text(encoding="utf-8")), 1)
    processed = 0

    def report(chars, duration):
        nonlocal processed
        processed += chars
        fraction = min(processed / total_chars, 0.99)
        callback(100 * (index - 1 + fraction) / total, input_path.name, index, total)

    return report


def natural_chapter_key(path):
    """Sort extracted chapter 2 before chapter 10, keeping filename text order."""
    return [int(part) if part.isdigit() else part.lower()
            for part in re.split(r"(\d+)", Path(path).name)]


def _selected_chapters(input_dir, selected_files):
    if not input_dir.is_dir():
        raise FileNotFoundError(f"Input directory not found: {input_dir}")
    from extract import extracted_chapter_paths

    files = sorted((Path(path) for path in extracted_chapter_paths(input_dir)), key=natural_chapter_key)
    if selected_files is not None:
        selected = {Path(path).resolve() for path in selected_files}
        known = {path.resolve() for path in files}
        if not selected.issubset(known):
            raise ValueError("Selected chapters must belong to the current chapter set.")
        files = [path for path in files if path.resolve() in selected]
    if not files:
        raise ValueError(f"No .txt chapters found in {input_dir}")
    return files


def generate_audiobooks_kokoro(input_dir, lang_code, voice, device="auto", output_dir=None,
                                audio_format=".wav", speed=1.0, split_pattern=r'\n+',
                                progress_callback=None, cancellation_flag=None,
                                pause_event=None, resume=False, selected_files=None):
    """Generate chapters; return all paths only after successful completion.

    Resume is explicit and checks source, synthesis settings and audio integrity.
    Completed chapters remain usable after an interrupted book run.
    """
    input_dir = Path(input_dir)
    files = _selected_chapters(input_dir, selected_files)
    audio_format = "." + audio_format.lower().lstrip(".")
    if audio_format not in {".wav", ".mp3"}:
        raise ValueError("Chapter audio format must be wav or mp3.")
    if not np.isfinite(speed) or speed <= 0:
        raise ValueError("Speed must be a positive finite number.")
    output_dir = Path(output_dir or input_dir.with_name(input_dir.name + "_audio"))
    output_dir.mkdir(parents=True, exist_ok=True)
    device = resolve_device(device)
    _checkpoint(cancellation_flag, pause_event)
    pipeline = _pipeline(lang_code, device)
    settings = dict(lang_code=lang_code, voice=voice, device=device, speed=speed,
                    split_pattern=split_pattern, audio_format=audio_format, model=MODEL_REPO)
    generated = []
    for index, input_path in enumerate(files, 1):
        _checkpoint(cancellation_flag, pause_event)
        output_path = output_dir / (input_path.stem + audio_format)
        if progress_callback:
            progress_callback(100 * (index - 1) / len(files), input_path.name, index, len(files))
        _chapter(input_path, output_path, pipeline, voice, settings, resume,
                 cancellation_flag, pause_event, _chapter_progress(progress_callback, input_path, index, len(files)))
        generated.append(str(output_path))
    _checkpoint(cancellation_flag, pause_event)
    if progress_callback:
        progress_callback(100, "Completed", len(files), len(files))
    return generated


def generate_audio_for_all_voices_kokoro(input_path, lang_code, voices, output_dir,
                                         device="auto", speed=1.0, split_pattern=r'\n+',
                                         cancellation_flag=None, progress_callback=None,
                                         pause_event=None):
    """Generate voice samples, reusing the pipeline within each language."""
    if not voices:
        raise ValueError("Select at least one voice.")
    pipeline = None
    current_language = None
    generated = []
    for index, voice in enumerate(voices, 1):
        _checkpoint(cancellation_flag, pause_event)
        if voice[0] != current_language:
            current_language = voice[0]
            pipeline = _pipeline(current_language, device)
        output_path = str(Path(output_dir) / f"test_{voice}.wav")
        if not generate_audio_for_file_kokoro(input_path, pipeline, voice, output_path, speed,
                                              split_pattern, cancellation_flag, None, pause_event):
            raise RuntimeError(f"Voice sample generation failed: {voice}")
        generated.append(output_path)
        if progress_callback:
            progress_callback(100 * index / len(voices), voice, index, len(voices))
    return generated


def test_single_voice_kokoro(input_text, voice, output_path, lang_code="a", device="auto",
                             speed=1.0, split_pattern=r'\n+', cancellation_flag=None,
                             progress_callback=None, pause_event=None):
    """Generate a sample; return its path only when the audio was published."""
    with tempfile.TemporaryDirectory() as temporary:
        input_path = Path(temporary) / "sample.txt"
        input_path.write_text(input_text, encoding="utf-8")
        _checkpoint(cancellation_flag, pause_event)
        pipeline = _pipeline(lang_code, device)
        if not generate_audio_for_file_kokoro(input_path, pipeline, voice, output_path, speed,
                                              split_pattern, cancellation_flag, None, pause_event):
            return None
    if progress_callback:
        progress_callback(100, "Completed", 1, 1)
    return output_path
