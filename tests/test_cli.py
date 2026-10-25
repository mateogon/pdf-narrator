import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

import cli
import generate_audiobook_kokoro


class CliTests(unittest.TestCase):
    def test_no_arguments_opens_desktop(self):
        run = Mock(return_value=0)
        with patch.dict("sys.modules", {"webui.app": types.SimpleNamespace(run=run)}):
            self.assertEqual(cli.main([]), 0)
        run.assert_called_once_with(dev=False)

    def test_classic_ui_opens_tk_window(self):
        app = Mock()
        with patch.dict("sys.modules", {"ui": types.SimpleNamespace(AudiobookApp=Mock(return_value=app))}):
            self.assertEqual(cli.main(["--ui", "classic"]), 0)
        app.mainloop.assert_called_once()

    def test_book_uses_shared_extraction_and_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            book = Path(temporary) / "book.epub"
            book.write_bytes(b"fixture")
            extract = Mock(return_value=temporary)
            synth = Mock(return_value=["chapter.wav"])
            with patch.dict("sys.modules", {"extract": types.SimpleNamespace(extract_book=extract)}), patch(
                    "generate_audiobook_kokoro.generate_audiobooks_kokoro", synth):
                self.assertEqual(cli.main([str(book), "--voice", "ef_dora", "--resume"]), 0)
            self.assertEqual(extract.call_args.kwargs["language"], "e")
            self.assertTrue(synth.call_args.kwargs["resume"])
            self.assertEqual(synth.call_args.args[1:3], ("e", "ef_dora"))

    def test_directory_skips_extraction(self):
        with tempfile.TemporaryDirectory() as temporary:
            extract = Mock()
            with patch.dict("sys.modules", {"extract": types.SimpleNamespace(extract_book=extract)}), patch(
                    "generate_audiobook_kokoro.generate_audiobooks_kokoro", return_value=["chapter.mp3"]):
                self.assertEqual(cli.main([temporary, "--format", "mp3"]), 0)
            extract.assert_not_called()

    def test_m4b_keeps_intermediate_chapters_for_verified_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.txt"
            source.write_text("A short story.", encoding="utf-8")
            output = root / "audio"
            pipeline = lambda *args, **kwargs: iter([("A short story", "", np.zeros(2400))])
            def export(audio_files, output_path, **metadata):
                self.assertEqual(metadata["chapter_titles"], [None])
                Path(output_path).write_bytes(b"test export")
                return str(output_path)
            with patch("generate_audiobook_kokoro._pipeline", return_value=pipeline), patch(
                    "generate_audiobook_kokoro.generate_audio_for_file_kokoro",
                    wraps=generate_audiobook_kokoro.generate_audio_for_file_kokoro) as synth, patch(
                    "audiobook_export.export_m4b", side_effect=export), patch("cli.shutil.which", return_value="ffmpeg"):
                args = [str(source), "--format", "m4b", "--device", "cpu", "--output-dir", str(output)]
                self.assertEqual(cli.main(args), 0)
                self.assertEqual(cli.main(args + ["--resume"]), 0)
                self.assertEqual(synth.call_count, 1)
            self.assertEqual({path.name for path in output.iterdir()}, {"book.m4b", "chapters"})
            self.assertEqual({path.name for path in (output / "chapters").iterdir()},
                             {"1_Full_Text.wav", "1_Full_Text.wav.resume.json"})

    def test_m4b_titles_follow_generated_audio_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "1_First.txt").write_text("A first chapter.")
            (root / "2_Second.txt").write_text("A second chapter.")
            (root / ".pdf-narrator-chapters.json").write_text(
                '{"version":1,"chapters":["1_First.txt","2_Second.txt"],"titles":{"1_First.txt":"A Real Title"}}')
            output = root / "audio"
            pipeline = lambda *args, **kwargs: iter([("A chapter", "", np.zeros(2400))])
            with patch("generate_audiobook_kokoro._pipeline", return_value=pipeline), patch(
                    "audiobook_export.export_m4b", return_value="book.m4b") as export, patch("cli.shutil.which", return_value="ffmpeg"):
                self.assertEqual(cli.main([str(root), "--format", "m4b", "--output-dir", str(output)]), 0)
            self.assertEqual(export.call_args.kwargs["chapter_titles"], ["A Real Title", None])

    def test_help_explains_m4b_intermediate_retention(self):
        help_text = cli._parser().format_help()
        self.assertIn("chapters/", help_text)
        self.assertIn("retains WAV", help_text)

    def test_failure_and_cancel_return_nonzero(self):
        for error, status in ((RuntimeError("failed"), 1), (InterruptedError(), 130), (KeyboardInterrupt(), 130)):
            with self.subTest(error=error), patch.object(cli, "_generate", side_effect=error):
                self.assertEqual(cli.main(["book.txt"]), status)


if __name__ == "__main__":
    unittest.main()
