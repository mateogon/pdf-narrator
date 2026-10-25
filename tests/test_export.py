import base64
import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf

from audiobook_export import export_m4b, read_epub_metadata


class ExportTests(unittest.TestCase):
    def test_reads_epub_metadata_and_safe_cover(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = root / "book.epub"
            with zipfile.ZipFile(book, "w") as archive:
                archive.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="EPUB/book.opf"/></rootfiles></container>')
                archive.writestr("EPUB/book.opf", '<package><metadata><title>Book</title><creator>Author</creator></metadata><manifest><item id="c" properties="cover-image" href="cover.jpg"/></manifest></package>')
                archive.writestr("EPUB/cover.jpg", b"cover bytes")
            result = read_epub_metadata(book, root)
            self.assertEqual(result["title"], "Book")
            self.assertEqual(result["author"], "Author")
            self.assertEqual(Path(result["cover_path"]).read_bytes(), b"cover bytes")

    def test_missing_epub_cover_preserves_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            book = root / "book.epub"
            with zipfile.ZipFile(book, "w") as archive:
                archive.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>')
                archive.writestr("book.opf", '<package><metadata><title>Book</title></metadata><manifest><item properties="cover-image" href="missing.jpg"/></manifest></package>')
            self.assertEqual(read_epub_metadata(book, root), {"title": "Book"})

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe unavailable")
    def test_actual_m4b_has_chapters_title_and_author(self):
        with tempfile.TemporaryDirectory(prefix="book's ") as temporary:
            root = Path(temporary)
            chapters = [root / "01 chapter.wav", root / "02 chapter.wav"]
            for chapter in chapters:
                sf.write(chapter, np.zeros(2400, dtype=np.float32), 24000)
            output = root / "book.m4b"
            cover = root / "cover.png"
            cover.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="))
            export_m4b(chapters, output, title="A #book=title", author="Author", cover_path=cover,
                       chapter_titles=["Arrival: the Sea & Sky", None])
            result = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_chapters", "-show_format", "-show_streams", "-of", "json", str(output)]))
            self.assertEqual(len(result["chapters"]), 2)
            self.assertEqual([chapter["tags"]["title"] for chapter in result["chapters"]],
                             ["Arrival: the Sea & Sky", "Chapter 2"])
            self.assertEqual(result["format"]["tags"]["title"], "A #book=title")
            self.assertEqual(result["format"]["tags"]["artist"], "Author")
            self.assertTrue(any(stream["codec_name"] == "aac" for stream in result["streams"]))
            self.assertTrue(any(stream.get("disposition", {}).get("attached_pic") == 1 for stream in result["streams"]))

    def test_invalid_chapter_titles_fail_before_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            chapter = root / "1_filename.wav"
            sf.write(chapter, np.zeros(2400, dtype=np.float32), 24000)
            for titles in ([], ["One", "Two"], [42], "A"):
                with self.subTest(titles=titles), patch("audiobook_export.shutil.which", return_value="ffmpeg"):
                    with self.assertRaises(ValueError):
                        export_m4b([chapter], root / "book.m4b", chapter_titles=titles)

    def test_failure_preserves_existing_m4b(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            chapter = root / "chapter.wav"
            sf.write(chapter, np.zeros(2400, dtype=np.float32), 24000)
            output = root / "book.m4b"
            output.write_bytes(b"existing")
            with patch("audiobook_export.shutil.which", return_value="ffmpeg"), patch(
                    "audiobook_export._run_ffmpeg", side_effect=InterruptedError()):
                with self.assertRaises(InterruptedError):
                    export_m4b([chapter], output)
            self.assertEqual(output.read_bytes(), b"existing")


if __name__ == "__main__":
    unittest.main()
