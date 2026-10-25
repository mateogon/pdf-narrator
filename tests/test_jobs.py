import threading
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from jobs import JobService
from library import Library, Settings

TEXT = "Chapter one\n\n" + "It was the best of times and it was the worst of times for the old house. " * 30


def _fake_generate(calls, gate=None):
    def generate(input_dir, language, voice, device, output_dir, audio_format, speed, progress_callback,
                 cancellation_flag, pause_event, resume, selected_files):
        calls.append([Path(path).name for path in selected_files])
        total = len(selected_files)
        for index, path in enumerate(selected_files, 1):
            progress_callback(100 * (index - 1) / total, Path(path).name, index, total)
            if gate is not None:
                gate.wait(5)
            while not pause_event.wait(0.01):
                if cancellation_flag():
                    raise InterruptedError
            if cancellation_flag():
                raise InterruptedError
            target = Path(output_dir) / (Path(path).stem + ".wav")
            target.parent.mkdir(parents=True, exist_ok=True)
            sf.write(target, np.zeros(24000, dtype=np.float32), 24000)
            progress_callback(100 * index / total, Path(path).name, index, total)
        progress_callback(100, "Completed", total, total)
    return generate


def _wait_idle(service, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        snapshot = service.snapshot()
        if snapshot["active"] is None and not snapshot["queued"]:
            return
        time.sleep(0.02)
    raise AssertionError("jobs did not finish")


def _service(tmp_path, **fakes):
    library = Library(tmp_path / "library")
    settings = Settings(tmp_path / "settings.json")
    fakes.setdefault("resolve_device", lambda device: "cpu")
    return library, JobService(library, settings, **fakes)


def _imported_book(tmp_path, library, service, chapters=3):
    source = tmp_path / "book.txt"
    source.write_text("\n\n".join(f"Chapter {index}\n\n{TEXT}" for index in range(1, chapters + 1)), encoding="utf-8")
    book = library.create_from_file(source)
    service.submit("import", book["id"])
    _wait_idle(service)
    return library.get(book["id"])


def test_import_extracts_chapters_with_real_extractor(tmp_path):
    library, service = _service(tmp_path)
    book = _imported_book(tmp_path, library, service)
    assert book["state"] == "ready"
    assert book["language"] == "a" and book["voice"] == "af_heart"
    assert library.summary(book)["included"] >= 1
    assert any("Found" in entry["text"] for entry in service.activity_since(0))


def test_create_records_each_chapter_and_skips_up_to_date_ones(tmp_path):
    calls = []
    library, service = _service(tmp_path, generate=_fake_generate(calls))
    book = _imported_book(tmp_path, library, service)
    service.submit("create", book["id"])
    _wait_idle(service)
    book = library.get(book["id"])
    summary = library.summary(book)
    assert summary["done"] == summary["included"]
    assert all(chapter["audio"]["seconds"] == 1.0 for chapter in book["chapters"] if chapter["included"])

    first = next(chapter for chapter in book["chapters"] if chapter["included"])
    library.save_chapter_text(book, first["id"], TEXT + " Edited.")
    service.submit("create", book["id"])
    _wait_idle(service)
    assert calls[-1] == [first["file"]]


def test_pause_resume_and_cancel(tmp_path):
    gate = threading.Event()
    calls = []
    library, service = _service(tmp_path, generate=_fake_generate(calls, gate))
    book = _imported_book(tmp_path, library, service)
    service.submit("create", book["id"])
    time.sleep(0.1)
    service.pause()
    assert service.snapshot()["active"]["state"] == "paused"
    gate.set()
    time.sleep(0.1)
    assert library.summary(library.get(book["id"]))["done"] == 0
    service.resume()
    service.cancel(book["id"])
    _wait_idle(service)
    assert library.summary(library.get(book["id"]))["done"] < library.summary(library.get(book["id"]))["included"]
    assert any(entry["text"] == "Stopped." for entry in service.activity_since(0))


def test_duplicate_submissions_share_one_job(tmp_path):
    gate = threading.Event()
    library, service = _service(tmp_path, generate=_fake_generate([], gate))
    book = _imported_book(tmp_path, library, service)
    first = service.submit("create", book["id"])
    assert service.submit("create", book["id"]) == first
    gate.set()
    _wait_idle(service)


def test_export_requires_all_chapters_and_writes_record(tmp_path):
    exported = []

    def export_m4b(audio_files, output_path, **options):
        exported.append((audio_files, options["chapter_titles"]))
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"m4b")
        return str(output_path)

    library, service = _service(tmp_path, generate=_fake_generate([]), export_m4b=export_m4b)
    book = _imported_book(tmp_path, library, service)
    service.submit("export", book["id"], format="m4b")
    _wait_idle(service)
    assert "Create every chapter" in library.get(book["id"])["error"]

    service.submit("create", book["id"])
    service.submit("export", book["id"], format="m4b")
    _wait_idle(service)
    book = library.get(book["id"])
    assert book["export"]["format"] == "m4b" and not Path(book["export"]["path"]).is_absolute()
    assert (library.book_dir(book["id"]) / book["export"]["path"]).is_file()
    assert len(exported[0][0]) == library.summary(book)["included"]


def test_mp3_export_writes_one_file_per_chapter(tmp_path):
    library, service = _service(tmp_path, generate=_fake_generate([]))
    book = _imported_book(tmp_path, library, service, chapters=2)
    service.submit("create", book["id"])
    service.submit("export", book["id"], format="mp3")
    _wait_idle(service)
    folder = library.book_dir(book["id"]) / library.get(book["id"])["export"]["path"]
    assert len(list(folder.glob("*.mp3"))) == library.summary(library.get(book["id"]))["included"]


def test_samples_are_cached_by_voice_speed_and_text(tmp_path):
    made = []

    def sample(text, voice, path, language, device, speed):
        made.append(voice)
        sf.write(path, np.zeros(2400, dtype=np.float32), 24000)

    library, service = _service(tmp_path, sample=sample)
    book = _imported_book(tmp_path, library, service)
    state = service.request_sample(book["id"], "af_heart", 1.0, "Hello there.")
    deadline = time.time() + 5
    while service.sample_state(state["key"])["state"] != "ready" and time.time() < deadline:
        time.sleep(0.02)
    assert service.request_sample(book["id"], "af_heart", 1.0, "Hello there.")["state"] == "ready"
    assert made == ["af_heart"]
    with pytest.raises(ValueError):
        service.request_sample(book["id"], "af_heart", 1.0, "   ")


def test_interrupted_import_is_reported_on_start(tmp_path):
    library = Library(tmp_path / "library")
    source = tmp_path / "book.txt"
    source.write_text(TEXT, encoding="utf-8")
    book = library.create_from_file(source)
    JobService(library, Settings(tmp_path / "settings.json"))
    assert library.get(book["id"])["state"] == "error"
