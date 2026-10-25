"""Functions the desktop window calls from JavaScript (pywebview js_api).

Each public method validates its input and delegates to Library or JobService.
Names that start with an underscore are not exposed to JavaScript.
"""

import platform
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import voices
from library import SUPPORTED_SUFFIXES, count_words, estimated_seconds, repeated_lines

ENGLISH = {"a", "b"}


class Api:
    def __init__(self, library, settings, jobs, server):
        self._library = library
        self._settings = settings
        self._jobs = jobs
        self._server = server
        self._window = None
        self._devices = None
        threading.Thread(target=self._detect_devices, daemon=True).start()

    # --- overview -------------------------------------------------------------------

    def state(self, activity_seq=0):
        books = [self._card(book) for book in self._library.books()]
        return {
            "books": books,
            "jobs": self._jobs.snapshot(),
            "activity": self._jobs.activity_since(int(activity_seq or 0)),
            "settings": dict(self._settings.values, library_root=str(self._settings.library_root)),
            "processor": self._processor(),
            "ffmpeg": shutil.which("ffmpeg") is not None,
        }

    def book(self, book_id):
        book = self._library.get(book_id)
        chapters = []
        for chapter in book["chapters"]:
            status = self._library.chapter_status(book, chapter)
            audio = self._library.audio_path(book, chapter)
            chapters.append({
                "id": chapter["id"], "title": chapter["title"], "words": chapter["words"],
                "included": chapter["included"], "status": status,
                "seconds": chapter["audio"]["seconds"] if status == "done" else round(
                    estimated_seconds(chapter["words"], book["speed"])),
                "audio_url": self._server.media_url(audio) if status in {"done", "redo"} and audio.is_file() else None,
            })
        export = book.get("export")
        return dict(self._card(book), chapters=chapters, speed=book["speed"], voice=book["voice"],
                    language=book["language"], source_name=book["source_name"],
                    export=dict(export, name=Path(export["path"]).name) if export else None,
                    folder=str(self._library.book_dir(book_id)))

    def voices(self, language):
        if language not in voices.LANGUAGES:
            raise ValueError("Unknown language.")
        return voices.voices_for(language)

    def languages(self):
        return [{"code": code, "label": label} for code, (label, _) in voices.LANGUAGES.items()]

    # --- import ---------------------------------------------------------------------

    def choose_files(self):
        import webview
        paths = self._window.create_file_dialog(
            webview.FileDialog.OPEN, allow_multiple=True,
            file_types=("Books (*.pdf;*.epub;*.txt;*.html;*.htm)",))
        return self.import_paths(list(paths or []))

    def choose_folder(self):
        import webview
        folders = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        if not folders:
            return []
        folder = Path(folders[0])
        files = sorted(path for path in folder.rglob("*") if path.suffix.lower() in SUPPORTED_SUFFIXES and path.is_file())
        return self.import_paths([str(path) for path in files])

    def import_paths(self, paths):
        created = []
        for path in paths:
            path = Path(path)
            if path.suffix.lower() not in SUPPORTED_SUFFIXES or not path.is_file():
                self._jobs.log(f"Skipped {path.name}: not a PDF, EPUB, TXT or HTML file.", "error")
                continue
            book = self._library.create_from_file(path)
            self._jobs.submit("import", book["id"])
            created.append(book["id"])
        return created

    def retry_import(self, book_id):
        book = self._library.get(book_id)
        book.update(state="importing", error=None)
        self._library.save(book)
        self._jobs.submit("import", book_id)

    # --- check text -----------------------------------------------------------------

    def chapter_text(self, book_id, chapter_id):
        book = self._library.get(book_id)
        text = self._library.chapter_text(book, chapter_id)
        return {"text": text, "tips": repeated_lines(text)}

    def save_chapter(self, book_id, chapter_id, text):
        self._require_idle(book_id)
        book = self._library.get(book_id)
        self._library.save_chapter_text(book, chapter_id, str(text))
        return {"saved": True}

    def remove_line(self, book_id, chapter_id, line):
        self._require_idle(book_id)
        self._library.remove_line(self._library.get(book_id), chapter_id, str(line))
        return self.chapter_text(book_id, chapter_id)

    def set_included(self, book_id, chapter_ids, included):
        self._library.set_included(self._library.get(book_id), list(chapter_ids), bool(included))

    def skip_front_matter(self, book_id):
        self._library.skip_front_matter(self._library.get(book_id))

    def paragraph(self, book_id, index=0):
        """A paragraph from the book for voice samples, cycling through good candidates."""
        book = self._library.get(book_id)
        candidates = []
        for chapter in book["chapters"]:
            if not chapter["included"]:
                continue
            # Extracted text keeps about one sentence per line: join lines into ~30-word passages.
            words = []
            for line in self._library.chapter_text(book, chapter["id"]).splitlines() + [""]:
                line = " ".join(line.split())
                if line and not words and count_words(line) < 7:
                    continue  # titles and stray fragments make poor openings
                if line:
                    words.extend(line.split())
                if words and (not line or len(words) >= 30):
                    if len(words) >= 20:
                        candidates.append(" ".join(words[:70]))
                    words = []
                if len(candidates) >= 12:
                    break
            if len(candidates) >= 12:
                break
        if not candidates:
            return "This is how your audiobook will sound."
        return candidates[int(index) % len(candidates)]

    # --- book settings --------------------------------------------------------------

    def update_book(self, book_id, changes):
        self._require_idle(book_id)
        book = self._library.get(book_id)
        allowed = {"title", "author", "voice", "speed", "language"}
        changes = {key: value for key, value in dict(changes).items() if key in allowed}
        reread = False
        if "speed" in changes:
            speed = float(changes["speed"])
            if not 0.5 <= speed <= 2.0:
                raise ValueError("Reading speed must be between 0.5× and 2×.")
            changes["speed"] = round(speed, 2)
        if "language" in changes:
            if changes["language"] not in voices.LANGUAGES:
                raise ValueError("Unknown language.")
            # Number and abbreviation normalization depends on English vs. other languages.
            reread = (changes["language"] in ENGLISH) != (book["language"] in ENGLISH)
            if not str(changes.get("voice", "")).startswith(changes["language"]):
                changes["voice"] = voices.default_voice(changes["language"])
        if "voice" in changes and changes["voice"] not in voices.voice_ids():
            raise ValueError("Unknown voice.")
        if "voice" in changes and changes["voice"][0] != changes.get("language", book["language"]):
            raise ValueError("The voice must match the book's language.")
        for key in ("title", "author"):
            if key in changes:
                changes[key] = str(changes[key]).strip()[:200]
        if changes.get("title") == "":
            raise ValueError("The title cannot be empty.")
        book.update(changes)
        self._library.save(book)
        if reread:
            book.update(state="importing", error=None)
            self._library.save(book)
            self._jobs.submit("import", book_id)
        return {"reread": reread}

    def remove_book(self, book_id):
        """Delete the library's copy of a book. The original file is not touched."""
        if self._jobs.book_job(book_id):
            raise RuntimeError("Stop this book's work before removing it.")
        folder = self._library.book_dir(book_id)
        if (folder / "book.json").is_file():
            shutil.rmtree(folder)

    # --- voice samples --------------------------------------------------------------

    def sample(self, book_id, voice, speed, text):
        if voice not in voices.voice_ids():
            raise ValueError("Unknown voice.")
        return self._sample_view(self._jobs.request_sample(book_id, voice, float(speed), str(text)))

    def sample_state(self, key):
        state = self._jobs.sample_state(str(key))
        return self._sample_view(state) if state else None

    def _sample_view(self, state):
        view = {"key": state["key"], "state": state["state"], "error": state.get("error")}
        if state["state"] == "ready":
            view["url"] = self._server.media_url(state["path"])
        return view

    # --- create and export ----------------------------------------------------------

    def create(self, book_id):
        book = self._library.get(book_id)
        if not any(chapter["included"] for chapter in book["chapters"]):
            raise ValueError("Choose at least one chapter to narrate.")
        return self._jobs.submit("create", book_id)

    def pause(self):
        self._jobs.pause()

    def resume(self):
        self._jobs.resume()

    def stop(self, book_id):
        self._jobs.cancel(book_id)

    def export(self, book_id, export_format):
        if export_format not in {"m4b", "mp3"}:
            raise ValueError("Export format must be m4b or mp3.")
        if export_format == "m4b" and not shutil.which("ffmpeg"):
            raise RuntimeError("Exporting an M4B needs FFmpeg. Install it, or export MP3 instead.")
        return self._jobs.submit("export", book_id, format=export_format)

    def reveal(self, book_id, which="folder"):
        book = self._library.get(book_id)
        folder = self._library.book_dir(book_id)
        target = Path(book["export"]["path"]) if which == "export" and book.get("export") else folder
        if not target.exists():
            target = folder
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(target)] if target.is_file() else ["open", str(target)])
        elif sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(target)] if target.is_file() else ["explorer", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target if target.is_dir() else target.parent)])

    # --- settings -------------------------------------------------------------------

    def update_settings(self, changes):
        changes = dict(changes)
        if "library_root" in changes:
            raise ValueError("Use choose_library_folder to change the library folder.")
        self._settings.update(**changes)
        return self.state()["settings"]

    def choose_library_folder(self):
        import webview
        if self._jobs.snapshot()["active"] or self._jobs.snapshot()["queued"]:
            raise RuntimeError("Wait until current work finishes before moving the library.")
        folders = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        if not folders:
            return None
        self._settings.update(library_root=str(Path(folders[0]).resolve()))
        self._library.root = self._settings.library_root
        return str(self._library.root)

    def diagnostics(self):
        lines = [f"PDF Narrator · Python {platform.python_version()} · {platform.platform()}",
                 f"Processor: {self._processor()}", f"FFmpeg: {'yes' if shutil.which('ffmpeg') else 'no'}"]
        for package in ("kokoro", "torch", "pywebview", "pymupdf"):
            try:
                from importlib.metadata import version
                lines.append(f"{package} {version(package)}")
            except Exception:
                lines.append(f"{package} not installed")
        errors = [entry["text"] for entry in self._jobs.activity_since(0) if entry["level"] == "error"][-5:]
        return "\n".join(lines + ["", "Recent errors:"] + (errors or ["none"]))

    # --- helpers --------------------------------------------------------------------

    def _require_idle(self, book_id):
        job = self._jobs.book_job(book_id)
        if job and job["kind"] in {"create", "import"}:
            raise RuntimeError("Pause and stop the current work on this book before changing it.")

    def _card(self, book):
        summary = self._library.summary(book) if book["chapters"] else None
        cover = self._library.book_dir(book["id"]) / book["cover"] if book.get("cover") else None
        return {
            "id": book["id"], "title": book["title"], "author": book.get("author") or "",
            "format": book["format"], "state": book["state"], "error": book.get("error"),
            "language": book.get("language"), "voice": book.get("voice"),
            "voice_name": voices.describe(book["voice"])["name"] if book.get("voice") in voices.voice_ids() else None,
            "cover_url": self._server.media_url(cover) if cover and cover.is_file() else None,
            "summary": summary, "job": self._jobs.book_job(book["id"]),
            "exported": bool(book.get("export")), "updated_at": book.get("updated_at"),
        }

    def _detect_devices(self):
        try:
            from generate_audiobook_kokoro import available_devices
            self._devices = available_devices()
        except Exception:
            self._devices = ["cpu"]

    def _processor(self):
        if self._devices is None:
            return {"detected": None, "label": "Checking…", "available": []}
        best = next(device for device in ("cuda", "mps", "cpu") if device in self._devices)
        labels = {"cuda": "NVIDIA GPU", "mps": "Apple GPU", "cpu": "CPU"}
        return {"detected": best, "label": labels[best], "available": self._devices}
