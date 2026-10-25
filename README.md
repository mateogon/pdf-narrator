# PDF Narrator

Convert PDF, EPUB, TXT and HTML books into audiobooks with local [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) speech synthesis. Use the desktop interface or the command line.

> For a reader with synchronized EPUB text and audio, see [Cadence](https://github.com/mateogon/Cadence).

![Desktop interface](assets/demo.png)

## Install and run

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Git, then run:

```bash
git clone https://github.com/mateogon/pdf-narrator.git
cd pdf-narrator
uv run pdf-narrator
```

uv installs Python 3.12 and the locked dependencies automatically. You do not need to activate a virtual environment. The same commands work in PowerShell, bash, zsh and fish.

The default installation uses CPU PyTorch wheels on Windows and Linux. On Apple Silicon, PyTorch includes MPS support; the app selects it when available. NVIDIA support is optional below. Java, DeepSpeed, a CUDA Toolkit installation and manual eSpeak installation are not required by the default setup. The English tokenizer is installed with the app; Kokoro downloads its model and selected voices on first synthesis, so that first run needs internet access.

The desktop app opens in its own window through [pywebview](https://pywebview.flowrl.com/), which uses the system web view: WebKit on macOS and WebView2 on Windows 10/11 (included with the system). On Linux, install WebKitGTK and PyGObject first, for example `sudo apt install gir1.2-webkit2-4.1 python3-gi` on Debian or Ubuntu. The command line can run without a display.

The app keeps a library of your books in `~/Audiobooks/PDF Narrator` (you can change it in Settings). Each book has its own folder with the extracted text, chapter audio and exports. The original file you import is copied and never modified.

## Command line

```bash
uv run pdf-narrator book.epub --voice af_heart
uv run pdf-narrator book.pdf --voice ef_dora --lang e --output-dir ./audio
uv run pdf-narrator book.txt --device cpu --format mp3
uv run pdf-narrator book.epub --format m4b --title "My book" --author "An author"
uv run pdf-narrator book.epub --resume
uv run pdf-narrator --help
```

With no input argument, `pdf-narrator` opens the desktop app. `uv run pdf-narrator --ui classic` opens the previous Tk window, which needs a Python build with Tk support.

- Inputs: `.pdf`, `.epub`, `.txt`, `.html`, `.htm`, or a directory of extracted `.txt` chapters.
- Outputs: WAV by default, MP3, or M4B with chapter markers and EPUB metadata when present.
- M4B files: the `.m4b` stays in `OUTPUT_DIR`. Intermediate WAV files and resume sidecars stay in `OUTPUT_DIR/chapters/`, including after export. This lets `--resume` reuse them. M4B export requires [FFmpeg](https://ffmpeg.org/download.html) on `PATH`.
- Device: `auto`, `cpu`, `mps`, or `cuda`. `auto` selects an available accelerator and falls back to CPU.
- Language: inferred from the voice prefix unless `--lang` is supplied. English voices use `a` or `b`; Spanish voices use `e`. Match the voice to the language.
- Resume: completed chapters are reused only when the source, settings and audio hashes match. The app writes chapter audio incrementally to limit memory use.

Kokoro also provides French, Hindi, Italian and Brazilian Portuguese voices. Japanese and Mandarin require additional Misaki language dependencies; these are not part of the default installation. See [Kokoro's language documentation](https://github.com/hexgrad/kokoro). Indonesian is not supported by this engine.

English extraction expands common abbreviations and numbers. Other languages preserve those forms for the speech engine. PDF header/footer filtering and chapter detection are heuristic; check the extracted text for unusual layouts.

After extraction, the app records the current chapter files in `.pdf-narrator-chapters.json`. Preview and generation use that list; older chapters and unrelated files stay on disk. Folders without this manifest use all `.txt` files, including `.TXT`.

The optional `titles` field preserves real chapter titles from the PDF TOC, EPUB TOC or HTML headings. M4B chapter markers use those titles. Older manifests and untitled chapters use `Chapter N`.

## NVIDIA acceleration (optional)

On Windows or Linux with a supported NVIDIA GPU and driver, use the CUDA 12.8 PyTorch wheels:

```bash
uv run --no-default-groups --extra cuda pdf-narrator book.epub --device cuda
```

Use those uv flags each time you want the CUDA environment. Running the default command selects the CPU dependency group again. The wheels include the CUDA runtime; you do not need to install the CUDA Toolkit. You still need a compatible NVIDIA driver. See the [PyTorch installation guide](https://pytorch.org/get-started/locally/) for driver and GPU compatibility. This option is not for macOS; use MPS there.

## Scanned PDFs (optional OCR)

Text PDFs work with the default installation. Scanned PDFs require the OCR extra plus system tools:

```bash
uv run --extra ocr pdf-narrator scanned.pdf
```

Install Tesseract and Poppler first, and ensure `tesseract`, `pdftocairo` and `pdfinfo` are on `PATH`:

- macOS with Homebrew: `brew install tesseract poppler`
- Ubuntu/Debian: `sudo apt install tesseract-ocr poppler-utils`
- Windows: install [Tesseract](https://tesseract-ocr.github.io/tessdoc/Installation.html) and [Poppler](https://pdf2image.readthedocs.io/en/latest/installation.html), then add their executable directories to `PATH`.

The current OCR path uses Tesseract's default language and crops the top and bottom 10% of each page. Check the extraction before generating a full book.

## Development and verification

```bash
uv sync --locked --group dev
uv run --group dev pytest -q
```

Dependencies are declared in `pyproject.toml`; `uv.lock` records the resolved versions. There are no separate requirements files per operating system. The `preshed` compatibility pin prevents an English spaCy tokenizer load failure reproduced on macOS; the other native spaCy dependencies do not need old pins.

The CI workflow defines CPU tests with Python 3.12 on Ubuntu, Windows and macOS. The runners install FFmpeg; Ubuntu uses Xvfb for native Tk tests. Tests run with Hugging Face offline and do not download Kokoro weights. JUnit results are saved as workflow artifacts. See the [CI runs](https://github.com/mateogon/pdf-narrator/actions/workflows/ci.yml) for current platform results.

The renewal was checked locally on Apple Silicon and on Windows 11 with Python 3.12.12 and an RTX 3050. Windows CPU/CUDA CLI synthesis and the test suite passed. The Windows GUI was constructed through SSH with CUDA visible and enabled; its desktop appearance and interactions were not visually reviewed. CI covers CPU behavior; CUDA and MPS synthesis need separate hardware checks.

A separate Ubuntu job installs the OCR extra, Tesseract and Poppler, then extracts text from a generated image-only PDF. Locally, this test skips with a reason if the optional dependencies are absent. To require it, set `REQUIRE_OCR=1` and run `uv run --locked --group dev --extra ocr pytest -q tests/test_ocr_integration.py`. The CI job requires OCR and fails if its tools or English language data are missing.

## License

[MIT](LICENSE.md). Kokoro and its model weights have their own [Apache 2.0 license](https://huggingface.co/hexgrad/Kokoro-82M).
