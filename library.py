"""Audiobook library on disk: one folder per book, with book.json as its record.

Layout of a book folder:
    book.json        title, author, language, voice, chapters and their audio records
    source/<file>    copy of the imported book, so it can be extracted again
    text/            extracted chapter .txt files (extract.py output and manifest)
    chapters/        chapter audio (.wav) and generator resume sidecars
    export/          finished audiobook files
    cover.(jpg|png)  optional cover
"""

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from collections import Counter
from pathlib import Path

import voices

SCHEMA_VERSION = 1
SUPPORTED_SUFFIXES = {".pdf", ".epub", ".txt", ".html", ".htm"}
WORDS_PER_MINUTE = 200  # Kokoro at 1.0x, measured on English prose
# Sections that people rarely want narrated. Matched against chapter titles.
FRONT_MATTER = re.compile(
    r"^\s*(cover|title page|half title|copyright|contents|table of contents|dedication|"
    r"also by|imprint|colophon|index|about the publisher)\b"
    r"|project gutenberg|gutenberg license|full license", re.IGNORECASE)
MIN_NARRATED_WORDS = 40


def default_library_root():
    return Path.home() / "Audiobooks" / "PDF Narrator"


def config_dir():
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "PDF Narrator"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "PDF Narrator"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "pdf-narrator"


def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False,
                                         prefix="." + path.name, suffix=".tmp") as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_text(path, text):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=Path(path).parent, delete=False,
                                         prefix=".edit-", suffix=".tmp") as handle:
            temporary = Path(handle.name)
            handle.write(text)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def text_sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def count_words(text):
    return len(re.findall(r"[^\W_]+", text, re.UNICODE))


def estimated_seconds(words, speed=1.0):
    return words / WORDS_PER_MINUTE * 60 / max(speed, 0.1)


class Settings:
    DEFAULTS = {"library_root": None, "export_format": "m4b", "device": "auto", "theme": "light"}

    def __init__(self, path=None):
        self.path = Path(path) if path else config_dir() / "settings.json"
        self.values = dict(self.DEFAULTS)
        try:
            self.values.update(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass

    @property
    def library_root(self):
        return Path(self.values["library_root"]) if self.values["library_root"] else default_library_root()

    def update(self, **changes):
        unknown = set(changes) - set(self.DEFAULTS)
        if unknown:
            raise ValueError(f"Unknown settings: {sorted(unknown)}")
        if changes.get("export_format") not in (None, "m4b", "mp3"):
            raise ValueError("Export format must be m4b or mp3.")
        if changes.get("device") not in (None, "auto", "cpu", "cuda", "mps"):
            raise ValueError("Unknown processor.")
        if changes.get("theme") not in (None, "light", "dark", "system"):
            raise ValueError("Unknown appearance.")
        self.values.update(changes)
        _write_json(self.path, self.values)


# --- Language detection for sources without metadata -------------------------------

_STOPWORDS = {
    "a": {"the", "and", "of", "to", "is", "that", "was", "with", "for", "his", "her", "it"},
    "e": {"el", "la", "de", "que", "y", "en", "los", "las", "por", "con", "una", "del"},
    "f": {"le", "la", "les", "et", "des", "est", "une", "que", "dans", "pour", "du", "pas"},
    "i": {"il", "la", "che", "di", "e", "un", "una", "per", "non", "del", "della", "sono"},
    "p": {"o", "a", "que", "de", "e", "não", "uma", "com", "os", "do", "da", "para"},
}


def detect_language(text):
    """Guess the Kokoro language code from common words; English when unsure."""
    words = re.findall(r"[^\W\d_]+", text[:20000].lower(), re.UNICODE)
    if not words:
        return "a"
    if sum(1 for char in text[:5000] if "぀" <= char <= "ヿ") > 50:
        return "j"
    if sum(1 for char in text[:5000] if "一" <= char <= "鿿") > 50:
        return "z"
    counts = Counter(words)
    scores = {code: sum(counts[word] for word in stop) for code, stop in _STOPWORDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "a"


def _source_sample(path):
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            import pymupdf
            with pymupdf.open(path) as doc:
                return "\n".join(doc[index].get_text() for index in range(min(len(doc), 12)))
        if suffix in {".txt", ".html", ".htm"}:
            return path.read_text(encoding="utf-8", errors="ignore")[:50000]
    except Exception:
        return ""
    return ""


def _epub_language(path):
    import zipfile
    import xml.etree.ElementTree as ET
    try:
        with zipfile.ZipFile(path) as archive:
            container = ET.fromstring(archive.read("META-INF/container.xml"))
            package = ET.fromstring(archive.read(container.find(".//{*}rootfile").get("full-path")))
            node = package.find(".//{*}language")
            return node.text if node is not None else None
    except Exception:
        return None


def _pdf_metadata(path, cover_path):
    """Title/author from PDF metadata and a first-page cover image."""
    import pymupdf
    result = {}
    with pymupdf.open(path) as doc:
        meta = doc.metadata or {}
        if (meta.get("title") or "").strip():
            result["title"] = meta["title"].strip()
        if (meta.get("author") or "").strip():
            result["author"] = meta["author"].strip()
        if len(doc):
            pixmap = doc[0].get_pixmap(matrix=pymupdf.Matrix(0.6, 0.6))
            pixmap.save(cover_path)
            result["cover"] = Path(cover_path).name
    return result


def _slug(text):
    slug = re.sub(r"[^\w\-]+", "-", text, flags=re.UNICODE).strip("-").lower()
    return slug[:60] or "book"


def repeated_lines(text, minimum=3):
    """Short repeated lines without sentence punctuation: usually running page headers."""
    lines = [line.strip() for line in text.splitlines()]
    counts = Counter(line for line in lines if 3 <= len(line) <= 60 and len(line.split()) <= 8
                     and _has_letters(line) and not re.search(r"[.!?,;:\"“”]\s*$", line))
    return [{"line": line, "count": count} for line, count in counts.most_common(5) if count >= minimum]


def _has_letters(text):
    return bool(re.search(r"[^\W\d_]", text, re.UNICODE))


class Library:
    def __init__(self, root):
        self.root = Path(root)

    # --- records ----------------------------------------------------------------

    def book_dir(self, book_id):
        if not re.fullmatch(r"[\w\-]+", book_id or "", re.UNICODE):
            raise ValueError("Invalid book id.")
        path = (self.root / book_id).resolve()
        if path.parent != self.root.resolve():
            raise ValueError("Invalid book id.")
        return path

    def get(self, book_id):
        path = self.book_dir(book_id) / "book.json"
        try:
            book = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise KeyError(book_id) from None
        if book.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"Unsupported book record version in {path}")
        return book

    def save(self, book):
        book["updated_at"] = time.time()
        _write_json(self.book_dir(book["id"]) / "book.json", book)

    def books(self):
        if not self.root.is_dir():
            return []
        result = []
        for path in self.root.iterdir():
            if (path / "book.json").is_file():
                try:
                    result.append(self.get(path.name))
                except (ValueError, KeyError, OSError):
                    continue
        return sorted(result, key=lambda book: book.get("updated_at", 0), reverse=True)

    # --- import -------------------------------------------------------------------

    def create_from_file(self, source):
        """Copy a book into the library and write a draft record; extraction runs later."""
        source = Path(source).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"File not found: {source}")
        if source.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise ValueError("Supported files: PDF, EPUB, TXT and HTML.")
        self.root.mkdir(parents=True, exist_ok=True)
        book_id = f"{_slug(source.stem)}-{uuid.uuid4().hex[:6]}"
        folder = self.book_dir(book_id)
        (folder / "source").mkdir(parents=True)
        shutil.copy2(source, folder / "source" / source.name)
        book = {
            "schema_version": SCHEMA_VERSION, "id": book_id, "title": source.stem.replace("_", " "),
            "author": "", "source_name": source.name, "format": source.suffix.lower().lstrip("."),
            "language": None, "voice": None, "speed": 1.0, "cover": None,
            "state": "importing", "error": None, "chapters": [], "export": None,
            "created_at": time.time(),
        }
        self.save(book)
        return book

    def source_path(self, book):
        return self.book_dir(book["id"]) / "source" / book["source_name"]

    def text_dir(self, book):
        return self.book_dir(book["id"]) / "text"

    def audio_dir(self, book):
        return self.book_dir(book["id"]) / "chapters"

    def prepare_metadata(self, book):
        """Fill title, author, cover and language before extraction needs the language."""
        source = self.source_path(book)
        folder = self.book_dir(book["id"])
        metadata = {}
        if book["format"] == "epub":
            from audiobook_export import read_epub_metadata
            metadata = read_epub_metadata(source, folder)
            if metadata.get("cover_path"):
                metadata["cover"] = Path(metadata.pop("cover_path")).name
            language = voices.language_from_tag(_epub_language(source))
        elif book["format"] == "pdf":
            try:
                metadata = _pdf_metadata(source, folder / "cover.png")
            except Exception as error:
                print(f"Warning: PDF metadata unavailable: {error}")
            language = None
        else:
            language = None
        book.update({key: value for key, value in metadata.items() if key in {"title", "author", "cover"}})
        book["language"] = language or detect_language(_source_sample(source))
        book["voice"] = book.get("voice") or voices.default_voice(book["language"])
        self.save(book)
        return book

    def apply_extraction(self, book):
        """Build the chapter list from extract.py output; skip front matter by default."""
        from extract import extracted_chapter_paths, extracted_chapter_titles
        directory = self.text_dir(book)
        titles = {Path(name).stem: title for name, title in extracted_chapter_titles(directory).items()}
        chapters = []
        for index, path in enumerate(extracted_chapter_paths(directory), 1):
            path = Path(path)
            text = path.read_text(encoding="utf-8")
            title = (titles.get(path.stem) or _title_from_text(text) or f"Chapter {index}").strip()
            title = re.sub(r"(?<![A-Z][a-z])\.$", "", title) or f"Chapter {index}"
            words = count_words(text)
            chapters.append({
                "id": path.stem, "file": path.name, "title": title, "words": words,
                "sha256": text_sha256(text), "included": _narrate_by_default(title, words),
                "audio": None,
            })
        if not chapters:
            raise ValueError("No readable text was found in this book.")
        book["chapters"] = chapters
        book["state"] = "ready"
        book["error"] = None
        self.save(book)
        return book

    # --- chapters -----------------------------------------------------------------

    def chapter(self, book, chapter_id):
        for chapter in book["chapters"]:
            if chapter["id"] == chapter_id:
                return chapter
        raise KeyError(chapter_id)

    def chapter_text_path(self, book, chapter):
        return self.text_dir(book) / chapter["file"]

    def chapter_text(self, book, chapter_id):
        return self.chapter_text_path(book, self.chapter(book, chapter_id)).read_text(encoding="utf-8")

    def save_chapter_text(self, book, chapter_id, text):
        chapter = self.chapter(book, chapter_id)
        _write_text(self.chapter_text_path(book, chapter), text)
        chapter["sha256"] = text_sha256(text)
        chapter["words"] = count_words(text)
        self.save(book)
        return chapter

    def remove_line(self, book, chapter_id, line):
        text = self.chapter_text(book, chapter_id)
        kept = [row for row in text.splitlines() if row.strip() != line.strip()]
        return self.save_chapter_text(book, chapter_id, "\n".join(kept) + "\n")

    def set_included(self, book, chapter_ids, included):
        wanted = set(chapter_ids)
        for chapter in book["chapters"]:
            if chapter["id"] in wanted:
                chapter["included"] = bool(included)
        self.save(book)

    def skip_front_matter(self, book):
        for chapter in book["chapters"]:
            if not _narrate_by_default(chapter["title"], chapter["words"]):
                chapter["included"] = False
        self.save(book)

    def audio_path(self, book, chapter):
        return self.audio_dir(book) / (chapter["id"] + ".wav")

    def chapter_status(self, book, chapter):
        """done, redo (text or voice changed since its audio), waiting, or skipped."""
        audio = chapter.get("audio")
        if audio and self.audio_path(book, chapter).is_file():
            current = (audio.get("sha256") == chapter["sha256"] and audio.get("voice") == book["voice"]
                       and audio.get("speed") == book["speed"])
            if current:
                return "done"
            return "redo" if chapter["included"] else "skipped"
        return "waiting" if chapter["included"] else "skipped"

    def record_audio(self, book, chapter_id):
        import soundfile as sf
        chapter = self.chapter(book, chapter_id)
        info = sf.info(self.audio_path(book, chapter))
        chapter["audio"] = {"sha256": chapter["sha256"], "voice": book["voice"], "speed": book["speed"],
                            "seconds": round(info.duration, 2)}
        self.save(book)

    def summary(self, book):
        """Counts and durations for the UI; estimates use words when audio is missing."""
        statuses = Counter()
        seconds = 0.0
        for chapter in book["chapters"]:
            status = self.chapter_status(book, chapter)
            statuses[status] += 1
            if not chapter["included"]:
                continue
            if status == "done":
                seconds += chapter["audio"]["seconds"]
            else:
                seconds += estimated_seconds(chapter["words"], book["speed"])
        included = sum(1 for chapter in book["chapters"] if chapter["included"])
        return {
            "included": included, "total": len(book["chapters"]), "done": statuses["done"],
            "redo": statuses["redo"], "waiting": statuses["waiting"], "seconds": round(seconds),
            "words": sum(chapter["words"] for chapter in book["chapters"] if chapter["included"]),
        }

    def pending_chapters(self, book):
        return [chapter for chapter in book["chapters"]
                if chapter["included"] and self.chapter_status(book, chapter) in {"waiting", "redo"}]

    def finished_audio(self, book):
        """Audio for every included chapter, in book order; None until all are done."""
        chapters = [chapter for chapter in book["chapters"] if chapter["included"]]
        if not chapters or any(self.chapter_status(book, chapter) != "done" for chapter in chapters):
            return None
        return [(chapter, self.audio_path(book, chapter)) for chapter in chapters]


def _title_from_text(text):
    for line in text.splitlines():
        line = line.strip().rstrip(" .")
        if line:
            return line[:80] if len(line) <= 80 else None
    return None


def _narrate_by_default(title, words):
    return not FRONT_MATTER.search(title or "") and words >= MIN_NARRATED_WORDS
