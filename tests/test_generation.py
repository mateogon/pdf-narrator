import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf
import torch

import generate_audiobook_kokoro as generation


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "chapter.txt"
        self.source.write_text("Hello.\n***\nWorld!", encoding="utf-8")
        self.output = self.root / "chapter.wav"

    def generate(self, pipeline, **kwargs):
        return generation.generate_audio_for_file_kokoro(self.source, pipeline, "af_heart", self.output, **kwargs)

    def test_streams_chunks_filters_punctuation_and_detaches_tensor(self):
        def pipeline(text, **kwargs):
            self.assertEqual(text, "Hello.\nWorld!")
            yield "Hello", "", torch.ones(100, requires_grad=True) * 0.2
            yield "***", "", np.ones(1000)
            yield "World", "", np.ones(200) * 2
        self.assertTrue(self.generate(pipeline))
        audio, rate = sf.read(self.output)
        self.assertEqual((len(audio), rate), (300, 24000))
        self.assertAlmostEqual(audio[0], 0.2, places=4)
        self.assertLessEqual(audio.max(), 1)

    def test_failure_and_cancel_preserve_previous_output(self):
        self.output.write_bytes(b"previous audio")
        def failing(text, **kwargs):
            yield "Hello", "", np.ones(100)
            raise RuntimeError("engine failed")
        self.assertFalse(self.generate(failing))
        self.assertEqual(self.output.read_bytes(), b"previous audio")
        cancelled = False
        def cancel_after_chunk(chars, duration):
            nonlocal cancelled
            cancelled = True
        with self.assertRaises(InterruptedError):
            self.generate(lambda *args, **kwargs: iter([("Hello", "", np.ones(100))]),
                          cancellation_flag=lambda: cancelled, chunk_progress_callback=cancel_after_chunk)
        self.assertEqual(self.output.read_bytes(), b"previous audio")
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["chapter.txt", "chapter.wav"])

    def test_invalid_audio_never_publishes(self):
        for audio in (None, np.array([]), np.array([np.nan]), np.array([np.inf]), np.ones((4, 2))):
            with self.subTest(audio=audio):
                self.assertFalse(self.generate(lambda *args, **kwargs: iter([("Hello", "", audio)])))
                self.assertFalse(self.output.exists())

    def test_empty_spoken_chunk_after_audio_is_failure(self):
        self.output.write_bytes(b"previous audio")
        def pipeline(*args, **kwargs):
            yield "Hello", "", np.ones(100)
            yield "World", "", None
        self.assertFalse(self.generate(pipeline))
        self.assertEqual(self.output.read_bytes(), b"previous audio")

    def test_punctuation_input_never_calls_pipeline(self):
        self.source.write_text("***\n___\n...", encoding="utf-8")
        self.assertFalse(self.generate(lambda *args, **kwargs: self.fail("pipeline called")))

    def test_cancel_while_paused(self):
        with self.assertRaises(InterruptedError):
            generation._checkpoint(lambda: True, threading.Event())

    def test_device_resolution(self):
        with patch.object(generation, "available_devices", return_value=["cpu", "mps"]):
            self.assertEqual(generation.resolve_device(), "mps")
            self.assertEqual(generation.resolve_device("cuda"), "cpu")
            self.assertEqual(generation.resolve_device("cpu"), "cpu")
            with self.assertRaises(ValueError):
                generation.resolve_device("unknown")

    def test_resume_checks_source_voice_and_audio(self):
        output_dir = self.root / "audio"
        pipeline = lambda *args, **kwargs: iter([("Hello", "", np.ones(100) * 0.1)])
        with patch.object(generation, "KPipeline", return_value=pipeline) as factory:
            kwargs = dict(input_dir=self.root, lang_code="a", voice="af_heart", device="cpu", output_dir=output_dir)
            generated = generation.generate_audiobooks_kokoro(**kwargs)
            factory.assert_called_with(lang_code="a", device="cpu", repo_id=generation.MODEL_REPO)
            with patch.object(generation, "generate_audio_for_file_kokoro", wraps=generation.generate_audio_for_file_kokoro) as synth:
                generation.generate_audiobooks_kokoro(**kwargs, resume=True)
                synth.assert_not_called()
                self.source.write_text("Changed text", encoding="utf-8")
                generation.generate_audiobooks_kokoro(**kwargs, resume=True)
                self.assertEqual(synth.call_count, 1)
                kwargs["voice"] = "am_adam"
                generation.generate_audiobooks_kokoro(**kwargs, resume=True)
                self.assertEqual(synth.call_count, 2)
                Path(generated[0]).write_bytes(b"corrupt")
                generation.generate_audiobooks_kokoro(**kwargs, resume=True)
                self.assertEqual(synth.call_count, 3)
                generation.generate_audiobooks_kokoro(**kwargs)
                self.assertEqual(synth.call_count, 4)

    def test_directory_failure_and_cancel_do_not_report_completed(self):
        progress = []
        kwargs = dict(input_dir=self.root, lang_code="a", voice="af_heart", device="cpu", output_dir=self.root / "audio",
                      progress_callback=lambda *event: progress.append(event))
        with patch.object(generation, "_pipeline", return_value=lambda *args, **kw: iter([])):
            with self.assertRaises(RuntimeError):
                generation.generate_audiobooks_kokoro(**kwargs)
            with self.assertRaises(InterruptedError):
                generation.generate_audiobooks_kokoro(**kwargs, cancellation_flag=lambda: True)
        self.assertFalse(any(event[1] == "Completed" for event in progress))

    def test_voice_samples_choose_each_language_and_cancel_before_loading(self):
        pipeline = lambda *args, **kwargs: iter([("Hello", "", np.ones(100) * 0.1)])
        with patch.object(generation, "_pipeline", return_value=pipeline) as factory:
            generation.generate_audio_for_all_voices_kokoro(
                self.source, "a", ["af_heart", "ef_dora"], self.root / "voices", device="cpu")
            self.assertEqual(factory.call_args_list[0].args, ("a", "cpu"))
            self.assertEqual(factory.call_args_list[1].args, ("e", "cpu"))
            factory.reset_mock()
            with self.assertRaises(InterruptedError):
                generation.generate_audio_for_all_voices_kokoro(
                    self.source, "a", ["af_heart"], self.root, cancellation_flag=lambda: True)
            with self.assertRaises(InterruptedError):
                generation.test_single_voice_kokoro("Hello", "af_heart", self.output, cancellation_flag=lambda: True)
            factory.assert_not_called()

    def test_natural_chapter_order(self):
        names = ["10_ten.txt", "2_two.txt", "1_one.txt"]
        self.assertEqual(sorted(names, key=generation.natural_chapter_key), ["1_one.txt", "2_two.txt", "10_ten.txt"])

    def test_extraction_manifest_excludes_stale_chapters_without_deleting_files(self):
        fresh = self.root / "1_Fresh.txt"
        stale = self.root / "2_Stale.txt"
        fresh.write_text("Fresh chapter", encoding="utf-8")
        stale.write_text("Old extraction", encoding="utf-8")
        manifest = self.root / ".pdf-narrator-chapters.json"
        manifest.write_text(json.dumps({"version": 1, "chapters": [fresh.name]}), encoding="utf-8")
        self.assertEqual(generation._selected_chapters(self.root, None), [fresh.resolve()])
        self.assertEqual(generation._selected_chapters(self.root, [fresh]), [fresh.resolve()])
        with self.assertRaises(ValueError):
            generation._selected_chapters(self.root, [stale])
        self.assertEqual(stale.read_text(encoding="utf-8"), "Old extraction")
        self.assertTrue(self.source.is_file())

    def test_selection_rejects_outside_files_and_empty_list(self):
        for selected in ([], [self.root / "outside.txt"]):
            with self.assertRaises(ValueError):
                generation.generate_audiobooks_kokoro(self.root, "a", "af_heart", selected_files=selected)


if __name__ == "__main__":
    unittest.main()
