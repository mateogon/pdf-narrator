"""Audiobook metadata and atomic M4B export through local ffmpeg."""

import os
import posixpath
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import soundfile as sf


def read_epub_metadata(source, cover_dir):
    """Read EPUB title/author and an optional bounded cover without extracting paths."""
    result = {}
    if Path(source).suffix.lower() != ".epub":
        return result
    with zipfile.ZipFile(source) as archive:
        container = ET.fromstring(archive.read("META-INF/container.xml"))
        package_path = container.find(".//{*}rootfile").get("full-path")
        package = ET.fromstring(archive.read(package_path))
        for field, tag in (("title", "title"), ("author", "creator")):
            node = package.find(f".//{{*}}{tag}")
            if node is not None and node.text:
                result[field] = node.text.strip()
        cover_id = next((node.get("content") for node in package.findall(".//{*}meta")
                         if node.get("name") == "cover"), None)
        items = package.findall(".//{*}manifest/{*}item")
        cover = next((item for item in items if "cover-image" in item.get("properties", "").split()
                      or (cover_id and item.get("id") == cover_id)), None)
        if cover is not None:
            result.update(_read_cover(archive, package_path, cover, cover_dir))
    return result


def _read_cover(archive, package_path, cover, cover_dir):
    try:
        path = posixpath.normpath(posixpath.join(posixpath.dirname(package_path), cover.get("href", "")))
        info = archive.getinfo(path)
        suffix = Path(path).suffix.lower()
        if info.file_size > 10 * 1024 * 1024 or suffix not in {".jpg", ".jpeg", ".png"}:
            print("Warning: EPUB cover is too large or unsupported; exporting without cover.")
            return {}
        cover_path = Path(cover_dir) / ("cover" + suffix)
        cover_path.write_bytes(archive.read(info))
        return {"cover_path": str(cover_path)}
    except (KeyError, OSError, ValueError) as error:
        print(f"Warning: EPUB cover is unavailable; exporting without cover: {error}")
        return {}


def _metadata_value(value):
    value = str(value).replace("\r", " ").replace("\n", " ")
    for char in ("\\", "=", ";", "#"):
        value = value.replace(char, "\\" + char)
    return value


def _write_chapters(audio_files, destination, title, author, chapter_titles):
    lines = [";FFMETADATA1", "title=" + _metadata_value(title), "artist=" + _metadata_value(author),
             "album=" + _metadata_value(title)]
    offset = 0
    for index, path in enumerate(audio_files):
        info = sf.info(path)
        if info.frames <= 0:
            raise ValueError(f"Empty chapter audio: {path}")
        end = offset + round(info.duration * 1000)
        lines.extend(["[CHAPTER]", "TIMEBASE=1/1000", f"START={offset}", f"END={end}",
                      "title=" + _metadata_value(chapter_titles[index] or f"Chapter {index + 1}")])
        offset = end
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _chapter_titles(titles, count):
    if titles is None:
        return [None] * count
    if isinstance(titles, str):
        raise ValueError("Provide one chapter title per audio file.")
    titles = list(titles)
    if len(titles) != count:
        raise ValueError("Provide one chapter title per audio file.")
    if any(title is not None and not isinstance(title, str) for title in titles):
        raise ValueError("Chapter titles must be text or None.")
    return [title.strip() if title else None for title in titles]


def _run_ffmpeg(command, cancellation_flag, error_log):
    if cancellation_flag and cancellation_flag():
        raise InterruptedError("M4B export cancelled.")
    with error_log.open("w+") as errors:
        with subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=errors) as process:
            try:
                while process.poll() is None:
                    if cancellation_flag and cancellation_flag():
                        raise InterruptedError("M4B export cancelled.")
                    time.sleep(0.1)
            except BaseException:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise
            if process.returncode:
                errors.seek(0)
                raise RuntimeError("ffmpeg export failed: " + errors.read()[-4000:])


def export_m4b(audio_files, output_path, title=None, author=None, cover_path=None,
               cancellation_flag=None, chapter_titles=None):
    """Encode chapters atomically; missing display titles use Chapter N."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("M4B export requires ffmpeg on PATH.")
    audio_files = [Path(path).resolve() for path in audio_files]
    if not audio_files:
        raise ValueError("No audio chapters to export.")
    chapter_titles = _chapter_titles(chapter_titles, len(audio_files))
    output_path = Path(output_path).resolve()
    if output_path in audio_files:
        raise ValueError("M4B output cannot overwrite a chapter input.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output_path.parent) as temporary:
        temporary = Path(temporary)
        playlist = temporary / "chapters.txt"
        entries = ["file '" + str(path).replace("'", "'\\''") + "'" for path in audio_files]
        playlist.write_text("\n".join(entries) + "\n", encoding="utf-8")
        metadata = temporary / "metadata.txt"
        _write_chapters(audio_files, metadata, title or output_path.stem, author or "", chapter_titles)
        staged = temporary / "book.m4b"
        command = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
                   "-safe", "0", "-i", str(playlist), "-f", "ffmetadata", "-i", str(metadata)]
        if cover_path:
            command.extend(["-i", str(Path(cover_path).resolve())])
        command.extend(["-map", "0:a:0", "-map_metadata", "1", "-map_chapters", "1", "-c:a", "aac", "-b:a", "64k"])
        if cover_path:
            command.extend(["-map", "2:v:0", "-c:v", "copy", "-disposition:v:0", "attached_pic"])
        command.extend(["-movflags", "+faststart", "-f", "ipod", str(staged)])
        _run_ffmpeg(command, cancellation_flag, temporary / "ffmpeg.log")
        if cancellation_flag and cancellation_flag():
            raise InterruptedError("M4B export cancelled.")
        os.replace(staged, output_path)
    return str(output_path)
