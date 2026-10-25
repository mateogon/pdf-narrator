"""UI contracts tested with native Tk widgets, without loading a TTS model."""
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import ttkbootstrap as tb

import ui


@pytest.fixture(scope="module")
def window():
    try:
        root = tb.Window(themename="flatly")
    except ui.tk.TclError as exc:
        pytest.skip(f"Native Tk display is unavailable: {exc}")
    root.withdraw()
    yield root
    root.destroy()


def app_stub():
    return SimpleNamespace(
        after=lambda _delay, callback, *args: callback(*args),
        post_to_ui=lambda callback, *args: callback(*args),
        confirm_chapters=Mock(), is_running=False,
        audio_frame=SimpleNamespace(get_device=lambda: "cpu"),
    )


def test_native_picker_cancel_preserves_source(window, monkeypatch):
    app = app_stub()
    app.update_audio_output_dir_display = Mock()
    source = ui.SourceFrame(window, app)
    source.pdf_path.set("original.epub")
    picker = Mock(return_value="")
    monkeypatch.setattr(ui.filedialog, "askopenfilename", picker)
    source._browse_single_pdf()
    assert source.pdf_path.get() == "original.epub"
    assert picker.call_args.kwargs["parent"] == source


def test_restore_unavailable_device_uses_auto(window, monkeypatch):
    monkeypatch.setattr(ui, "available_devices", lambda: ["cpu", "mps"])
    frame = ui.AudioFrame(window, app_stub())
    frame.set_config({"device": "cuda"})
    assert frame.device.get() == "auto"
    assert frame.get_config()["resume"] is False
    frame.set_config({"device": "mps", "resume": True})
    assert frame.device.get() == "mps"
    assert frame.get_config()["resume"] is True


def test_review_edits_and_exclusions_preserve_source_files(window, tmp_path):
    first = tmp_path / "01.txt"
    second = tmp_path / "02.txt"
    first.write_text("Original text", encoding="utf-8")
    second.write_text("Excluded text", encoding="utf-8")
    frame = ui.ChapterReviewFrame(window, app_stub())
    frame.load_chapters([(str(tmp_path), str(tmp_path / "audio"))])
    frame.editor.delete("1.0", "end")
    frame.editor.insert("1.0", "Edited narration")
    assert frame.save_current()
    frame.chapter_list.selection_clear(0, "end")
    frame.chapter_list.selection_set(1)
    frame._select_chapter()
    frame.include_current.set(False)
    frame._set_included()
    assert frame.selected_files() == [str(first)]
    assert first.read_text(encoding="utf-8") == "Edited narration"
    assert second.read_text(encoding="utf-8") == "Excluded text"
    assert not list(tmp_path.glob("*.tmp"))


def test_review_failed_replace_keeps_original_and_dirty_editor(window, tmp_path, monkeypatch):
    chapter = tmp_path / "01.txt"
    chapter.write_text("Original text", encoding="utf-8")
    frame = ui.ChapterReviewFrame(window, app_stub())
    frame.load_chapters([(str(tmp_path), "unused")])
    frame.editor.insert("end", " edits")
    monkeypatch.setattr(ui.os, "replace", Mock(side_effect=OSError("disk full")))
    monkeypatch.setattr(ui.messagebox, "showerror", Mock())
    assert frame.save_current() is False
    assert chapter.read_text(encoding="utf-8") == "Original text"
    assert frame.editor.edit_modified()
    assert list(tmp_path.iterdir()) == [chapter]


def test_failed_voice_sample_never_reports_completed(window, tmp_path, monkeypatch):
    app = app_stub()
    frame = ui.VoiceTestFrame(window, app)
    complete = Mock()
    monkeypatch.setattr(frame, "_sample_completed", complete)
    monkeypatch.setattr(ui, "test_single_voice_kokoro", lambda **kwargs: None)
    monkeypatch.setattr(ui.messagebox, "showerror", Mock())
    frame._run_test_thread("Hello", {"mode": "single", "output_dir": str(tmp_path), "device": "cpu", "voice": "af_heart"})
    assert "failed" in frame.status_text.get().lower()
    complete.assert_not_called()


def test_voice_cancellation_is_distinct_from_failure(window, tmp_path, monkeypatch):
    frame = ui.VoiceTestFrame(window, app_stub())
    def cancelled(**kwargs):
        raise InterruptedError("cancelled")
    monkeypatch.setattr(ui, "test_single_voice_kokoro", cancelled)
    error = Mock()
    monkeypatch.setattr(ui.messagebox, "showerror", error)
    frame._run_test_thread("Hello", {"mode": "single", "output_dir": str(tmp_path), "device": "cpu", "voice": "af_heart"})
    assert frame.status_text.get() == "Test cancelled."
    error.assert_not_called()


def test_mac_sample_uses_argument_list_and_stops_owned_player(window, tmp_path, monkeypatch):
    app = app_stub()
    app.after = Mock()
    frame = ui.VoiceTestFrame(window, app)
    sample = tmp_path / "sample with spaces.wav"
    sample.write_bytes(b"test")
    frame.sample_path.set(str(sample))
    player = Mock()
    player.poll.return_value = None
    start_player = Mock(return_value=player)
    monkeypatch.setattr(ui.sys, "platform", "darwin")
    monkeypatch.setattr(ui.subprocess, "Popen", start_player)
    frame._play_sample()
    assert start_player.call_args.args[0] == ["afplay", str(sample)]
    assert frame.status_text.get() == "Playing sample"
    frame.stop_playback()
    player.terminate.assert_called_once()


def test_confirmation_blocks_zero_included_chapters(monkeypatch):
    warn = Mock()
    monkeypatch.setattr(ui.messagebox, "showwarning", warn)
    app = SimpleNamespace(review_frame=SimpleNamespace(selected_files=lambda: []), review_ready=threading.Event())
    ui.AudiobookApp.confirm_chapters(app)
    assert not app.review_ready.is_set()
    warn.assert_called_once()


def test_worker_posts_without_calling_tk_then_main_drains():
    import queue
    owner = threading.get_ident()
    calls = []
    app = SimpleNamespace(closing=False, ui_queue=queue.Queue(), after=Mock(), report_callback_exception=Mock())
    thread = threading.Thread(target=ui.AudiobookApp.post_to_ui, args=(app, lambda: calls.append(threading.get_ident())))
    thread.start()
    thread.join()
    assert calls == []
    app.after.assert_not_called()
    app._drain_ui_queue = lambda: None
    ui.AudiobookApp._drain_ui_queue(app)
    assert calls == [owner]
    app.after.assert_called_once()


@pytest.mark.parametrize("source_option", ["single", "batch"])
def test_extraction_uses_voice_language_and_preserves_engine_error(tmp_path, monkeypatch, source_option):
    source = tmp_path / "book.txt"
    source.write_text("Tengo 25 libros.", encoding="utf-8")
    calls = []
    def extraction(path, **kwargs):
        calls.append(kwargs["language"])
        kwargs["progress_callback"](None)
        raise RuntimeError("OCR dependency unavailable")
    monkeypatch.setattr(ui, "extract_book", extraction)
    show_error = Mock()
    monkeypatch.setattr(ui.messagebox, "showerror", show_error)
    app = SimpleNamespace(
        config_data={
            "source": {"source_option": source_option, "pdf_path": str(source), "pdf_folder": str(tmp_path), "use_toc": True, "extract_mode": "chapters"},
            "audio": {"voicepack": "ef_dora"},
            "extracted_text_output": str(tmp_path / "extracted"), "audio_output": str(tmp_path / "audio"),
        },
        cancellation_flag=False, pause_event=threading.Event(),
        _update_gui_progress=Mock(), post_to_ui=lambda callback, *args: callback(*args),
        review_frame=SimpleNamespace(finish=Mock()), control_frame=SimpleNamespace(set_button_states=Mock()),
    )
    ui.AudiobookApp._run_process_thread(app)
    assert calls == ["e"]
    assert "OCR dependency unavailable" in show_error.call_args.args[1]
    assert any(call.kwargs.get("status") == "Failed" for call in app._update_gui_progress.call_args_list)


def test_preview_uses_extraction_manifest_and_natural_order(window, tmp_path, monkeypatch):
    selected = [tmp_path / "10_last.txt", tmp_path / "2_second.txt", tmp_path / "1_first.txt"]
    for path in selected + [tmp_path / "old_chapter.txt"]:
        path.write_text(path.name, encoding="utf-8")
    monkeypatch.setattr(ui, "extracted_chapter_paths", lambda directory: [str(path) for path in selected])
    frame = ui.ChapterReviewFrame(window, app_stub())
    frame.load_chapters([(str(tmp_path), "unused")])
    assert [Path(path).name for path in frame.paths] == ["1_first.txt", "2_second.txt", "10_last.txt"]


def test_loading_saved_settings_does_not_reapply_initialized_theme(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text('{"theme":"flatly","audio_settings":{"device":"mps"}}', encoding="utf-8")
    monkeypatch.setattr(ui, "CONFIG_FILE", str(config))
    app = SimpleNamespace(source_frame=SimpleNamespace(set_config=Mock()), audio_frame=SimpleNamespace(set_config=Mock()), theme_var=Mock(), style=Mock())
    ui.AudiobookApp.load_config(app)
    app.audio_frame.set_config.assert_called_once_with({"device": "mps"})
    app.style.theme_use.assert_not_called()
