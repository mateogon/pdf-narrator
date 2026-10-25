import time
import urllib.error
import urllib.request

import numpy as np
import pytest
import soundfile as sf

from jobs import JobService
from library import Library, Settings
from webui.api import Api
from webui.server import MediaServer

TEXT = "Chapter one\n\n" + "It was the best of times and it was the worst of times for the old house. " * 30


def _get(url, headers=None):
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), b""


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "library"
    (root / "book").mkdir(parents=True)
    (root / "book" / "audio.wav").write_bytes(bytes(range(256)) * 4)
    (tmp_path / "secret.txt").write_text("outside")
    media = MediaServer(lambda: root).start()
    yield media, root
    media.stop()


def test_media_requires_token_and_stays_inside_library(server):
    media, root = server
    base = f"http://127.0.0.1:{media.port}"
    url = media.media_url(root / "book" / "audio.wav")
    assert _get(base + url)[0] == 200
    assert _get(base + url.replace(media.token, "wrong"))[0] == 403
    assert _get(f"{base}/media/{media.token}/../secret.txt")[0] == 404
    assert _get(f"{base}/media/{media.token}/%2e%2e/secret.txt")[0] == 404
    assert media.media_url(root.parent / "secret.txt") is None


def test_media_supports_range_requests(server):
    media, root = server
    status, headers, body = _get(f"http://127.0.0.1:{media.port}" + media.media_url(root / "book" / "audio.wav"),
                                 {"Range": "bytes=10-19"})
    assert status == 206 and body == bytes(range(10, 20))
    assert headers["Content-Range"] == "bytes 10-19/1024"
    assert _get(f"http://127.0.0.1:{media.port}" + media.media_url(root / "book" / "audio.wav"),
                {"Range": "bytes=5000-"})[0] == 416


def test_app_files_are_served(server):
    media, _ = server
    status, headers, body = _get(media.app_url)
    assert status == 200 and b"PDF Narrator" in body
    assert _get(f"http://127.0.0.1:{media.port}/app/../server.py")[0] == 404


def _api(tmp_path):
    library = Library(tmp_path / "library")
    settings = Settings(tmp_path / "settings.json")
    media = MediaServer(lambda: library.root)
    jobs = JobService(library, settings, resolve_device=lambda device: "cpu")
    return library, jobs, Api(library, settings, jobs, media)


def _imported(tmp_path, library, jobs, api):
    source = tmp_path / "book.txt"
    source.write_text(TEXT, encoding="utf-8")
    [book_id] = api.import_paths([str(source)])
    deadline = time.time() + 10
    while (jobs.snapshot()["active"] or jobs.snapshot()["queued"]) and time.time() < deadline:
        time.sleep(0.02)
    return book_id


def test_import_and_book_view(tmp_path):
    library, jobs, api = _api(tmp_path)
    other = tmp_path / "notes.docx"
    other.write_text("x")
    assert api.import_paths([str(other)]) == []
    book_id = _imported(tmp_path, library, jobs, api)
    view = api.book(book_id)
    assert view["state"] == "ready" and view["voice_name"] == "Heart"
    assert view["chapters"][0]["status"] == "waiting" and view["chapters"][0]["audio_url"] is None
    assert len(api.paragraph(book_id).split()) >= 20
    assert any(book["id"] == book_id for book in api.state()["books"])


def test_update_book_validates_voice_language_and_rereads(tmp_path):
    library, jobs, api = _api(tmp_path)
    book_id = _imported(tmp_path, library, jobs, api)
    with pytest.raises(ValueError):
        api.update_book(book_id, {"voice": "ef_dora"})
    with pytest.raises(ValueError):
        api.update_book(book_id, {"speed": 9})
    assert api.update_book(book_id, {"language": "b"}) == {"reread": False}
    assert library.get(book_id)["voice"].startswith("b")
    assert api.update_book(book_id, {"language": "e"}) == {"reread": True}
    assert library.get(book_id)["voice"] == "ef_dora"


def test_done_chapters_get_audio_urls(tmp_path):
    library, jobs, api = _api(tmp_path)
    book_id = _imported(tmp_path, library, jobs, api)
    book = library.get(book_id)
    chapter = book["chapters"][0]
    path = library.audio_path(book, chapter)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, np.zeros(2400, dtype=np.float32), 24000)
    library.record_audio(library.get(book_id), chapter["id"])
    view = api.book(book_id)
    assert view["chapters"][0]["status"] == "done"
    assert view["chapters"][0]["audio_url"].startswith(f"/media/{api._server.token}/")


def test_remove_book_deletes_only_library_copy(tmp_path):
    library, jobs, api = _api(tmp_path)
    book_id = _imported(tmp_path, library, jobs, api)
    api.remove_book(book_id)
    assert not library.book_dir(book_id).exists()
    assert (tmp_path / "book.txt").exists()
