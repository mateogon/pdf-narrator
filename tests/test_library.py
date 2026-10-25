import json

import numpy as np
import pytest
import soundfile as sf

import voices
from library import Library, Settings, detect_language, repeated_lines

SPANISH = "Capítulo uno\n\n" + "El caballero de la triste figura salió por la mañana con su escudero. " * 20
ENGLISH = "Chapter one\n\n" + "It was the best of times and it was the worst of times for the old house. " * 20


def _book_with_chapters(tmp_path, chapters):
    library = Library(tmp_path / "library")
    source = tmp_path / "novel.txt"
    source.write_text("placeholder", encoding="utf-8")
    book = library.create_from_file(source)
    text_dir = library.text_dir(book)
    text_dir.mkdir()
    names = []
    for index, (title, text) in enumerate(chapters, 1):
        name = f"{index:02d}_{title.replace(' ', '_')}.txt"
        (text_dir / name).write_text(text, encoding="utf-8")
        names.append(name)
    (text_dir / ".pdf-narrator-chapters.json").write_text(
        json.dumps({"version": 1, "chapters": names, "titles": {name: title for name, (title, _) in zip(names, chapters)}}))
    book["language"], book["voice"] = "a", "af_heart"
    library.save(book)
    return library, library.apply_extraction(library.get(book["id"]))


def _write_audio(library, book, chapter, seconds=1.0):
    path = library.audio_path(book, chapter)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, np.zeros(int(24000 * seconds), dtype=np.float32), 24000)
    library.record_audio(library.get(book["id"]), chapter["id"])


def test_import_copies_source_and_detects_language(tmp_path):
    source = tmp_path / "quijote.txt"
    source.write_text(SPANISH, encoding="utf-8")
    library = Library(tmp_path / "library")
    book = library.create_from_file(source)
    assert library.source_path(book).read_text(encoding="utf-8") == SPANISH
    book = library.prepare_metadata(book)
    assert book["language"] == "e"
    assert book["voice"] == "ef_dora"


def test_rejects_unsupported_files_and_unsafe_ids(tmp_path):
    library = Library(tmp_path / "library")
    other = tmp_path / "notes.docx"
    other.write_text("x")
    with pytest.raises(ValueError):
        library.create_from_file(other)
    with pytest.raises(ValueError):
        library.book_dir("../outside")


def test_front_matter_and_tiny_sections_are_skipped_by_default(tmp_path):
    library, book = _book_with_chapters(tmp_path, [
        ("Copyright", "All rights reserved. " * 30), ("Contents", "Chapter one " * 30),
        ("Chapter 1", ENGLISH), ("Epigraph", "Short line."),
        ("THE FULL PROJECT GUTENBERG LICENSE", "Terms of use. " * 40)])
    included = {chapter["title"]: chapter["included"] for chapter in book["chapters"]}
    assert included == {"Copyright": False, "Contents": False, "Chapter 1": True, "Epigraph": False,
                        "THE FULL PROJECT GUTENBERG LICENSE": False}
    assert library.summary(book)["included"] == 1


def test_editing_text_or_voice_marks_audio_for_redo(tmp_path):
    library, book = _book_with_chapters(tmp_path, [("Chapter 1", ENGLISH), ("Chapter 2", ENGLISH)])
    first, second = book["chapters"]
    _write_audio(library, book, first, seconds=2)
    book = library.get(book["id"])
    assert library.chapter_status(book, library.chapter(book, first["id"])) == "done"
    assert [chapter["id"] for chapter in library.pending_chapters(book)] == [second["id"]]

    library.save_chapter_text(book, first["id"], ENGLISH + "One more sentence.")
    book = library.get(book["id"])
    assert library.chapter_status(book, library.chapter(book, first["id"])) == "redo"

    _write_audio(library, book, library.chapter(book, first["id"]))
    book = library.get(book["id"])
    book["voice"] = "af_bella"
    assert library.chapter_status(book, library.chapter(book, first["id"])) == "redo"


def test_summary_uses_real_durations_for_done_chapters(tmp_path):
    library, book = _book_with_chapters(tmp_path, [("Chapter 1", ENGLISH), ("Chapter 2", ENGLISH)])
    _write_audio(library, book, book["chapters"][0], seconds=3)
    summary = library.summary(library.get(book["id"]))
    assert summary["done"] == 1 and summary["waiting"] == 1
    assert summary["seconds"] > 3
    assert library.finished_audio(library.get(book["id"])) is None


def test_remove_line_drops_repeated_headers(tmp_path):
    text = "\n".join(["PRIDE AND PREJUDICE", "Real sentence one."] * 4)
    library, book = _book_with_chapters(tmp_path, [("Chapter 1", text + "\n" + ENGLISH)])
    chapter_id = book["chapters"][0]["id"]
    assert repeated_lines(library.chapter_text(book, chapter_id))[0]["line"] == "PRIDE AND PREJUDICE"
    library.remove_line(book, chapter_id, "PRIDE AND PREJUDICE")
    assert "PRIDE AND PREJUDICE" not in library.chapter_text(library.get(book["id"]), chapter_id)


def test_header_tip_ignores_single_words_and_sentences():
    text = "\n".join(["You", "Swallow, little Swallow,", "Real sentence one."] * 5)
    assert repeated_lines(text) == []
    assert repeated_lines("\n".join(["PRIDE", "Body text here."] * 4))[0]["line"] == "PRIDE"


def test_standard_ebooks_front_matter_is_skipped(tmp_path):
    library, book = _book_with_chapters(tmp_path, [
        ("Titlepage", "Pride and Prejudice " * 30), ("Uncopyright", "Public domain. " * 40), ("XIV", ENGLISH)])
    assert [chapter["included"] for chapter in book["chapters"]] == [False, False, True]
    assert book["chapters"][2]["title"] == "Chapter XIV"


def test_detect_language():
    assert detect_language(ENGLISH) == "a"
    assert detect_language(SPANISH) == "e"
    assert detect_language("") == "a"


def test_settings_validate_and_persist(tmp_path):
    settings = Settings(tmp_path / "settings.json")
    settings.update(export_format="mp3", theme="dark")
    assert Settings(tmp_path / "settings.json").values["export_format"] == "mp3"
    with pytest.raises(ValueError):
        settings.update(device="tpu")
    with pytest.raises(ValueError):
        settings.update(unknown=True)


def test_voice_catalog():
    english = voices.voices_for("a")
    assert english[0]["id"] == "af_heart" and english[0]["grade"] == "A"
    assert voices.describe("bm_george")["language_label"] == "English (UK)"
    assert voices.language_from_tag("en-GB") == "b"
    assert voices.language_from_tag("es-ES") == "e"
    assert voices.language_from_tag("xx") is None
