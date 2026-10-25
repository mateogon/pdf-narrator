"""Real OCR coverage; system tools are optional locally and required in the OCR CI job."""
import io
import os
import shutil
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
import pymupdf
import pytest

from extract import extract_book, extracted_chapter_paths


def test_scanned_pdf_extracts_real_text(tmp_path):
    missing = [tool for tool in ('tesseract', 'pdftocairo', 'pdfinfo') if not shutil.which(tool)]
    try:
        import pdf2image  # noqa: F401
        import pytesseract
    except ImportError as exc:
        missing.append(str(exc))
    if missing:
        reason = 'Real OCR requires the ocr extra and Tesseract/Poppler on PATH: ' + ', '.join(missing)
        if os.environ.get('REQUIRE_OCR') == '1':
            pytest.fail(reason)
        pytest.skip(reason)
    assert 'eng' in pytesseract.get_languages(), 'Tesseract English data is missing'

    image = Image.new('RGB', (1600, 500), 'white')
    draw = ImageDraw.Draw(image)
    draw.text((100, 200), 'The lighthouse guides ships safely.',
              font=ImageFont.load_default(size=64), fill='black')
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    source = tmp_path / 'scanned.pdf'
    with pymupdf.open() as document:
        page = document.new_page(width=800, height=250)
        page.insert_image(page.rect, stream=buffer.getvalue())
        assert page.get_text().strip() == ''
        document.save(source)

    output = tmp_path / 'extracted'
    assert extract_book(source, output_dir=output) == str(output)
    chapters = extracted_chapter_paths(output)
    assert len(chapters) == 1
    text = Path(chapters[0]).read_text(encoding='utf-8')
    assert 'the lighthouse guides ships safely' in ' '.join(text.lower().split())
