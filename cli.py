"""One entry point for desktop and command-line audiobook generation."""

import argparse
import math
import shutil
import sys
import tempfile
from pathlib import Path


def _parser():
    parser = argparse.ArgumentParser(description="Convert EPUB, PDF, TXT or HTML into a local audiobook.")
    parser.add_argument("input", nargs="?", help="Book file or directory containing extracted .txt chapters")
    parser.add_argument("--voice", default="af_heart")
    parser.add_argument("--lang", choices=list("abefhijpz"), help="Kokoro language code; defaults to the voice prefix")
    parser.add_argument("--device", choices=["auto", "cpu", "cuda", "mps"], default="auto")
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--format", choices=["wav", "mp3", "m4b"], default="wav",
                        help="Output format; M4B retains WAV audio and resume sidecars in chapters/")
    parser.add_argument("--resume", action="store_true", help="Reuse chapters only when source/settings/audio hashes match")
    parser.add_argument("--title")
    parser.add_argument("--author")
    parser.add_argument("--ui", choices=["desktop", "classic"], default="desktop",
                        help="Without an input file: open the desktop app, or the classic Tk window")
    parser.add_argument("--dev", action="store_true", help=argparse.SUPPRESS)
    return parser


def _validate(args, voices):
    if args.format == "m4b" and shutil.which("ffmpeg") is None:
        raise RuntimeError("M4B export requires ffmpeg on PATH.")
    if args.voice not in voices:
        raise ValueError(f"Unknown voice: {args.voice}")
    if not math.isfinite(args.speed) or args.speed <= 0:
        raise ValueError("Speed must be a positive finite number.")
    if args.lang and args.lang != args.voice[0]:
        raise ValueError("Language must match the voice prefix.")


def _generate(args):
    from extract import extract_book
    from generate_audiobook_kokoro import available_voices, generate_audiobooks_kokoro

    _validate(args, available_voices())
    source = Path(args.input).resolve()
    output_dir = args.output_dir or source.with_name(source.stem + "_audio")
    output_dir = output_dir.resolve()
    language = args.lang or args.voice[0]
    if not source.exists():
        raise FileNotFoundError(f"Input not found: {source}")
    with tempfile.TemporaryDirectory(prefix="pdf-narrator-") as temporary:
        chapters = str(source) if source.is_dir() else extract_book(
            str(source), output_dir=str(Path(temporary) / "chapters"), language=language)
        if not chapters:
            raise RuntimeError("Book extraction failed.")
        chapter_output = output_dir / "chapters" if args.format == "m4b" else output_dir
        audio_files = generate_audiobooks_kokoro(
            chapters, language, args.voice, device=args.device, output_dir=str(chapter_output),
            audio_format=".wav" if args.format == "m4b" else "." + args.format,
            speed=args.speed, resume=args.resume)
        if args.format == "m4b":
            _export(args, source, output_dir, audio_files, temporary, chapters)
        else:
            print(f"Generated {len(audio_files)} chapters in {output_dir}")
    return 0


def _export(args, source, output_dir, audio_files, temporary, chapters):
    from extract import extracted_chapter_titles
    from audiobook_export import export_m4b, read_epub_metadata
    metadata = read_epub_metadata(source, temporary)
    metadata["title"] = args.title or metadata.get("title") or source.stem
    metadata["author"] = args.author or metadata.get("author")
    titles = {Path(name).stem: title for name, title in extracted_chapter_titles(chapters).items()}
    chapter_titles = [titles.get(Path(path).stem) for path in audio_files]
    print(export_m4b(audio_files, output_dir / (source.stem + ".m4b"), chapter_titles=chapter_titles, **metadata))


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.input is None:
        if args.ui == "desktop":
            from webui.app import run
            return run(dev=args.dev)
        from ui import AudiobookApp
        AudiobookApp().mainloop()
        return 0
    try:
        return _generate(args)
    except (KeyboardInterrupt, InterruptedError):
        print("Cancelled.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
