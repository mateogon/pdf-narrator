"""Background work for the desktop app: one queue, one worker, observable state.

The worker runs import (metadata + extraction), audio creation and export jobs in
order. Voice samples run on their own thread so they stay responsive while a book
is being created. The UI never calls Kokoro or ffmpeg directly: it submits jobs
and polls snapshot() and activity_since().
"""

import contextlib
import io
import itertools
import re
import sys
import tempfile
import threading
import time
import traceback
from collections import deque
from pathlib import Path

from library import estimated_seconds, text_sha256

# Lines that only matter when diagnosing: library warnings, download bars.
_TECHNICAL = re.compile(r"warn|deprecat|torch|cuda|mps|\d+%\||it/s|bytes|huggingface|\x1b|^\s*\[A", re.I)


class _ActivityStream(io.TextIOBase):
    """Copy writes to the real stream and to the activity log, line by line."""

    def __init__(self, original, log):
        self.original = original
        self.log = log
        self.buffer_text = ""

    def write(self, text):
        if self.original is not None:
            try:
                self.original.write(text)
            except Exception:
                pass
        self.buffer_text += text.replace("\r", "\n")
        *lines, self.buffer_text = self.buffer_text.split("\n")
        for line in lines:
            if line.strip():
                self.log(line.strip())
        return len(text)

    def flush(self):
        if self.original is not None:
            with contextlib.suppress(Exception):
                self.original.flush()


class JobService:
    def __init__(self, library, settings, *, extract=None, generate=None, export_m4b=None,
                 sample=None, resolve_device=None):
        self.library = library
        self.settings = settings
        # Real implementations load lazily so tests can inject fakes without torch.
        self._extract = extract
        self._generate = generate
        self._export_m4b = export_m4b
        self._sample = sample
        self._resolve_device = resolve_device
        self._ids = itertools.count(1)
        self._lock = threading.RLock()
        self._wake = threading.Condition(self._lock)
        self._queue = deque()
        self._active = None
        self._running = threading.Event()
        self._running.set()
        self._cancelled = False
        self._activity = deque(maxlen=800)
        self._activity_seq = itertools.count(1)
        self._samples = {}
        self._sample_lock = threading.Lock()
        self._stopping = False
        self._recover_interrupted_imports()
        self._worker = threading.Thread(target=self._work, name="narrator-jobs", daemon=True)
        self._worker.start()

    # --- public API ----------------------------------------------------------------

    def submit(self, kind, book_id, **options):
        if kind not in {"import", "create", "export"}:
            raise ValueError(f"Unknown job: {kind}")
        with self._lock:
            for job in self._jobs():
                if job["kind"] == kind and job["book_id"] == book_id:
                    return job["id"]
            job = {"id": next(self._ids), "kind": kind, "book_id": book_id, "options": options,
                   "state": "queued", "queued_at": time.time()}
            self._queue.append(job)
            self._wake.notify()
            return job["id"]

    def pause(self):
        with self._lock:
            if self._active:
                self._running.clear()
                self._active["state"] = "paused"

    def resume(self):
        with self._lock:
            self._running.set()
            if self._active:
                self._active["state"] = "running"

    def cancel(self, book_id=None):
        """Stop the active job (if it matches) and drop that book's queued jobs."""
        with self._lock:
            self._queue = deque(job for job in self._queue if book_id is not None and job["book_id"] != book_id)
            if self._active and (book_id is None or self._active["book_id"] == book_id):
                self._cancelled = True
                self._running.set()

    def snapshot(self):
        with self._lock:
            active = dict(self._active) if self._active else None
            if active:
                active.pop("options", None)
            return {"active": active,
                    "queued": [{key: job[key] for key in ("id", "kind", "book_id")} for job in self._queue]}

    def book_job(self, book_id):
        with self._lock:
            for job in self._jobs():
                if job["book_id"] == book_id:
                    return {key: job.get(key) for key in ("id", "kind", "state")}
        return None

    def activity_since(self, seq=0):
        with self._lock:
            return [entry for entry in self._activity if entry["seq"] > seq]

    def log(self, text, level="info"):
        level = "technical" if level == "info" and _TECHNICAL.search(text) else level
        with self._lock:
            self._activity.append({"seq": next(self._activity_seq), "time": time.time(),
                                   "level": level, "text": text[:500]})

    def stop(self):
        with self._lock:
            self._stopping = True
            self._queue.clear()
            self._cancelled = True
            self._running.set()
            self._wake.notify()

    # --- voice samples -------------------------------------------------------------

    def request_sample(self, book_id, voice, speed, text):
        """Start (or reuse) a short sample of the book's own text in one voice."""
        text = text.strip()[:600]
        if not text:
            raise ValueError("The sample needs some text.")
        key = text_sha256(f"{voice}|{speed}|{text}")[:16]
        folder = self.library.book_dir(book_id) / "samples"
        path = folder / f"{voice}-{key}.wav"
        with self._lock:
            state = self._samples.get(key)
            if path.is_file():
                self._samples[key] = {"state": "ready", "path": str(path)}
            elif state is None or state["state"] == "error":
                self._samples[key] = {"state": "running", "path": str(path)}
                book = self.library.get(book_id)
                threading.Thread(target=self._make_sample, daemon=True,
                                 args=(key, book["language"], voice, speed, text, path)).start()
            return dict(self._samples[key], key=key)

    def sample_state(self, key):
        with self._lock:
            state = self._samples.get(key)
            return dict(state, key=key) if state else None

    def _make_sample(self, key, language, voice, speed, text, path):
        try:
            with self._sample_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                sample = self._sample or _default_sample()
                sample(text, voice, path, language, self.settings.values["device"], speed)
            with self._lock:
                self._samples[key] = {"state": "ready", "path": str(path)}
        except Exception as error:
            self.log(f"Voice sample failed: {error}", "error")
            with self._lock:
                self._samples[key] = {"state": "error", "error": str(error)}

    # --- worker ----------------------------------------------------------------------

    def _jobs(self):
        return ([self._active] if self._active else []) + list(self._queue)

    def _recover_interrupted_imports(self):
        for book in self.library.books():
            if book.get("state") == "importing":
                book["state"] = "error"
                book["error"] = "The import was interrupted. Try again."
                self.library.save(book)

    def _work(self):
        while True:
            with self._lock:
                while not self._queue and not self._stopping:
                    self._wake.wait()
                if self._stopping:
                    return
                job = self._queue.popleft()
                job.update(state="running", started_at=time.time(), progress=0.0, message="Starting")
                self._active = job
                self._cancelled = False
                self._running.set()
            stream_out = _ActivityStream(sys.__stdout__, self.log)
            stream_err = _ActivityStream(sys.__stderr__, lambda text: self.log(text, "technical"))
            try:
                with contextlib.redirect_stdout(stream_out), contextlib.redirect_stderr(stream_err):
                    getattr(self, "_run_" + job["kind"])(job)
                self._finish(job, "done")
            except InterruptedError:
                self.log("Stopped.", "info")
                self._finish(job, "cancelled")
            except Exception as error:
                self.log(f"{type(error).__name__}: {error}", "error")
                self.log(traceback.format_exc(), "technical")
                self._mark_book_error(job, error)
                self._finish(job, "failed")

    def _finish(self, job, state):
        with self._lock:
            job["state"] = state
            self._active = None
            self._cancelled = False
            self._running.set()

    def _mark_book_error(self, job, error):
        try:
            book = self.library.get(job["book_id"])
        except Exception:
            return
        if job["kind"] == "import":
            book["state"] = "error"
        book["error"] = _friendly_error(job["kind"], error)
        self.library.save(book)

    def _update(self, job, **fields):
        with self._lock:
            job.update(fields)

    def _is_cancelled(self):
        return self._cancelled

    # --- jobs ------------------------------------------------------------------------

    def _run_import(self, job):
        book = self.library.get(job["book_id"])
        self._update(job, message="Reading book details")
        self.log(f"Importing {book['source_name']}")
        book = self.library.prepare_metadata(book)
        self._update(job, message="Extracting text")
        extract = self._extract or _default_extract()
        output = self.library.text_dir(book)
        output.mkdir(parents=True, exist_ok=True)
        result = extract(str(self.library.source_path(book)), output_dir=str(output), language=book["language"],
                         progress_callback=lambda percent: percent is not None and self._update(job, progress=percent))
        if not result:
            raise RuntimeError("Text extraction failed.")
        book = self.library.apply_extraction(self.library.get(book["id"]))
        summary = self.library.summary(book)
        self.log(f"Found {summary['total']} sections; {summary['included']} will be narrated.")

    def _run_create(self, job):
        book = self.library.get(job["book_id"])
        pending = self.library.pending_chapters(book)
        if not pending:
            self.log("Every chapter is already up to date.")
            return
        book["error"] = None
        self.library.save(book)
        resolve = self._resolve_device or _default_resolve_device()
        device = resolve(self.settings.values["device"])
        estimates = {chapter["id"]: estimated_seconds(chapter["words"], book["speed"]) for chapter in pending}
        self._update(job, message="Preparing the voice engine", device=device, chapter_count=len(pending),
                     chapter_index=0, chapter_id=None, chapter_progress=0.0, speed_ratio=None,
                     seconds_left=None)
        self.log(f"Creating {len(pending)} chapters with {book['voice']} on {device.upper()}")
        state = {"index": 0, "started": None, "audio_done": 0.0, "paused_for": 0.0}

        def finish_chapter(index):
            chapter = pending[index - 1]
            if self.library.audio_path(book, chapter).is_file():
                fresh = self.library.get(book["id"])
                self.library.record_audio(fresh, chapter["id"])
                seconds = self.library.chapter(self.library.get(book["id"]), chapter["id"])["audio"]["seconds"]
                state["audio_done"] += seconds
                estimates[chapter["id"]] = seconds

        def progress(percent, name, index, total):
            now = time.monotonic()
            if state["started"] is None and index >= 1:
                state["started"] = now
            if index != state["index"]:
                if state["index"]:
                    finish_chapter(state["index"])
                state["index"] = index
            fraction = max(0.0, min(1.0, percent * total / 100 - (index - 1)))
            chapter = pending[min(index, total) - 1]
            produced = state["audio_done"] + fraction * estimates[chapter["id"]]
            elapsed = now - state["started"] if state["started"] else 0
            ratio = produced / elapsed if elapsed > 2 and produced > 0 else None
            remaining = sum(estimates[item["id"]] for item in pending[index:]) + (1 - fraction) * estimates[chapter["id"]]
            self._update(job, message=f"Creating {chapter['title']}", chapter_index=index,
                         chapter_id=chapter["id"], chapter_progress=round(fraction, 3),
                         progress=round(percent, 2), speed_ratio=round(ratio, 1) if ratio else None,
                         seconds_left=round(remaining / ratio) if ratio else None)

        generate = self._generate or _default_generate()
        text_dir = self.library.text_dir(book)
        generate(str(text_dir), book["language"], book["voice"], device=device,
                 output_dir=str(self.library.audio_dir(book)), audio_format=".wav", speed=book["speed"],
                 progress_callback=progress, cancellation_flag=self._is_cancelled, pause_event=self._running,
                 resume=True, selected_files=[str(text_dir / chapter["file"]) for chapter in pending])
        if state["index"]:
            finish_chapter(state["index"])
        self.log(f"Finished creating {book['title']}.", "success")

    def _run_export(self, job):
        book = self.library.get(job["book_id"])
        finished = self.library.finished_audio(book)
        if not finished:
            raise RuntimeError("Create every chapter before exporting.")
        export_format = job["options"].get("format") or self.settings.values["export_format"]
        folder = self.library.book_dir(book["id"]) / "export"
        name = _file_name(book["title"])
        self._update(job, message="Exporting audiobook")
        if export_format == "m4b":
            export_m4b = self._export_m4b or _default_export_m4b()
            cover = self.library.book_dir(book["id"]) / book["cover"] if book.get("cover") else None
            path = export_m4b([audio for _, audio in finished], folder / f"{name}.m4b", title=book["title"],
                              author=book.get("author"), cover_path=str(cover) if cover else None,
                              cancellation_flag=self._is_cancelled,
                              chapter_titles=[chapter["title"] for chapter, _ in finished])
        elif export_format == "mp3":
            path = self._export_mp3(job, book, finished, folder / name)
        else:
            raise ValueError("Export format must be m4b or mp3.")
        book = self.library.get(book["id"])
        book["export"] = {"format": export_format, "path": str(path), "created_at": time.time()}
        self.library.save(book)
        self.log(f"Exported {Path(path).name}", "success")

    def _export_mp3(self, job, book, finished, folder):
        import soundfile as sf
        folder.mkdir(parents=True, exist_ok=True)
        for index, (chapter, audio) in enumerate(finished, 1):
            if self._cancelled:
                raise InterruptedError("Export cancelled.")
            data, rate = sf.read(audio, dtype="float32")
            target = folder / f"{index:03d} - {_file_name(chapter['title'])}.mp3"
            with tempfile.NamedTemporaryFile(dir=folder, suffix=".mp3", delete=False) as handle:
                temporary = Path(handle.name)
            try:
                sf.write(temporary, data, rate, format="MP3")
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
            self._update(job, progress=100 * index / len(finished))
        return folder


def _file_name(text):
    return re.sub(r'[\\/:*?"<>|]+', "", text).strip()[:100] or "Audiobook"


def _friendly_error(kind, error):
    text = str(error)
    if kind == "import":
        if "No readable text" in text or "no letters" in text.lower():
            return "No readable text was found. If this is a scanned PDF, install Tesseract to read it."
        return f"This book could not be read: {text}"
    if kind == "export" and "ffmpeg" in text.lower():
        return "Exporting an M4B needs FFmpeg. Install it, or export MP3 instead."
    return text


def _default_extract():
    from extract import extract_book
    return extract_book


def _default_generate():
    from generate_audiobook_kokoro import generate_audiobooks_kokoro
    return generate_audiobooks_kokoro


def _default_export_m4b():
    from audiobook_export import export_m4b
    return export_m4b


def _default_resolve_device():
    from generate_audiobook_kokoro import resolve_device
    return resolve_device


_pipelines = {}


def _default_sample():
    """Samples reuse one Kokoro pipeline per language and device."""
    from generate_audiobook_kokoro import _pipeline, generate_audio_for_file_kokoro, resolve_device

    def sample(text, voice, path, language, device, speed):
        device = resolve_device(device)
        key = (language, device)
        if key not in _pipelines:
            _pipelines[key] = _pipeline(language, device)
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "sample.txt"
            source.write_text(text, encoding="utf-8")
            if not generate_audio_for_file_kokoro(source, _pipelines[key], voice, str(path), speed):
                raise RuntimeError("Kokoro could not create the sample.")

    return sample
