import builtins
import json
import sys
from types import SimpleNamespace
from pathlib import Path

from PIL import Image
import zipfile

import pymupdf
import pytest

from extract import (EXTRACTION_MANIFEST, clean_pipeline, convert_numbers, extract_book,
                     extracted_chapter_paths, extracted_chapter_titles, scanned_pdf, split_text_into_heuristic_chapters)


@pytest.mark.parametrize(
    ('text', 'expected'),
    [
        ('1st 2nd 3rd 21st', 'first second third twenty-first'),
        ('12 birds in 1984.', 'twelve birds in nineteen eighty-four.'),
        ('1,234 birds', 'one thousand, two hundred and thirty-four birds'),
        ('3.14 and 1,234.50 and 1,5', '3.14 and 1,234.50 and 1,5'),
        ('A12 12B version1.2', 'A12 12B version1.2'),
    ],
)
def test_english_numbers(text, expected):
    assert convert_numbers(text) == expected


def test_cleaning_preserves_decimals():
    assert '3.14' in clean_pipeline('It costs 3.14 dollars.')


def test_non_english_cleaning_preserves_numbers_and_abbreviations():
    text = 'El Dr. López tiene 12 libros y cuesta 1,5 euros.'
    assert clean_pipeline(text, language='e') == text
    assert 'Doctor' in clean_pipeline('Dr. Smith has 12 books.', language='a')
    assert 'twelve' in clean_pipeline('Dr. Smith has 12 books.', language='b')


def test_pdf_heuristic_chapters_preserve_language():
    text = 'El Dr. López tiene 12 libros. ' * 6
    chapter = split_text_into_heuristic_chapters(text, language='e')[0]
    assert 'Dr.' in chapter['text']
    assert '12' in chapter['text']


def write_book(path, text):
    if path.suffix == '.pdf':
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((72, 100), text)
            document.set_toc([[1, 'First chapter', 1]])
            document.save(path)
    elif path.suffix == '.epub':
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('mimetype', 'application/epub+zip')
            archive.writestr('META-INF/container.xml', '''
                <container><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>
            ''')
            archive.writestr('OEBPS/content.opf', '''
                <package><manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/></manifest>
                <spine><itemref idref="chapter"/></spine></package>
            ''')
            archive.writestr('OEBPS/chapter.xhtml', f'<html><body><p>{text}</p></body></html>')
    elif path.suffix == '.html':
        path.write_text(f'<html><style>hidden</style><body><p>{text}</p><script>hidden</script></body></html>')
    else:
        path.write_text(text)


@pytest.mark.parametrize('extension', ['.txt', '.html', '.pdf', '.epub'])
@pytest.mark.parametrize('mode', ['whole', 'chapters'])
@pytest.mark.parametrize('language', ['en', 'e'])
def test_extract_book_formats_and_language(tmp_path, extension, mode, language):
    source = tmp_path / f'book{extension}'
    write_book(source, 'Dr. Alex has 12 books. The first book tells a story about the sea.')
    output = tmp_path / 'output'
    progress = []
    assert extract_book(source, extract_mode=mode, output_dir=output,
                        language=language, progress_callback=progress.append) == str(output)
    files = list(output.glob('*.txt'))
    assert files
    text = '\n'.join(path.read_text() for path in files)
    assert ('twelve' if language == 'en' else '12') in text
    assert ('Doctor' if language == 'en' else 'Dr.') in text
    assert 'hidden' not in text
    assert progress[0] == 0
    assert progress[-1] == 100


def test_ocr_pillow_preprocessing_and_line_filter(monkeypatch):
    page = Image.new('RGB', (20, 10), (201, 201, 201))
    page.putpixel((0, 1), (200, 200, 200))
    seen = []

    def recognize(image, timeout):
        seen.append(image)
        assert timeout == 30
        return '12\nHello world\nCopyright example'

    monkeypatch.setitem(sys.modules, 'pytesseract', SimpleNamespace(image_to_string=recognize))
    monkeypatch.setitem(sys.modules, 'pdf2image', SimpleNamespace(convert_from_path=lambda *args, **kwargs: [page]))
    monkeypatch.setattr('shutil.which', lambda command: '/mock/' + command)
    assert scanned_pdf('scan.pdf') == 'Hello world'
    assert seen[0].size == (20, 8)
    assert seen[0].getpixel((0, 0)) == 0
    assert seen[0].getpixel((1, 0)) == 255


def test_ocr_missing_extra_has_actionable_error(monkeypatch):
    original_import = builtins.__import__

    def import_without_ocr(name, *args, **kwargs):
        if name == 'pytesseract':
            raise ImportError('not installed')
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', import_without_ocr)
    with pytest.raises(RuntimeError, match='uv run --extra ocr pdf-narrator'):
        scanned_pdf('scanned.pdf')


def test_ocr_missing_system_tools_has_actionable_error(monkeypatch):
    pytest.importorskip('pytesseract')
    pytest.importorskip('pdf2image')
    monkeypatch.setattr('shutil.which', lambda command: None)
    with pytest.raises(RuntimeError, match='Tesseract and Poppler on PATH'):
        scanned_pdf('scanned.pdf')


def test_unsupported_and_missing_files(tmp_path):
    with pytest.raises(FileNotFoundError):
        extract_book(tmp_path / 'missing.txt')
    source = tmp_path / 'unsupported.doc'
    source.write_text('A test book.')
    with pytest.raises(ValueError, match='Unsupported file format'):
        extract_book(source, output_dir=tmp_path / 'output')


def test_reextract_manifest_excludes_stale_and_preserves_unrelated_files(tmp_path, monkeypatch):
    source = tmp_path / 'book.epub'
    source.touch()
    output = tmp_path / 'chapters'
    chapters = [{'title': 'First', 'text': 'First chapter.'},
                {'title': 'Second', 'text': 'Second chapter.'}]
    monkeypatch.setattr('extract.parse_epub_content', lambda *args, **kwargs: chapters)
    extract_book(source, output_dir=output)
    old_paths = extracted_chapter_paths(output)
    assert len(old_paths) == 2
    unrelated = output / 'user-notes.txt'
    unrelated.write_text('Keep my notes.')

    chapters.pop()
    assert extract_book(source, output_dir=output) == str(output)
    assert extracted_chapter_paths(output) == old_paths[:1]
    assert Path(old_paths[1]).read_text() == 'Second chapter.'
    assert unrelated.read_text() == 'Keep my notes.'
    manifest = json.loads((output / EXTRACTION_MANIFEST).read_text())
    assert manifest == {'version': 1, 'chapters': ['1_First.txt'], 'titles': {'1_First.txt': 'First'}}


def test_manual_chapter_folder_without_manifest(tmp_path):
    (tmp_path / 'b.txt').write_text('Second')
    (tmp_path / 'c.TXT').write_text('Uppercase extension')
    (tmp_path / 'a.txt').write_text('First')
    (tmp_path / 'folder.txt').mkdir()
    (tmp_path / 'audio.wav').write_bytes(b'')
    assert extracted_chapter_paths(str(tmp_path)) == [str(tmp_path / name) for name in ('a.txt', 'b.txt', 'c.TXT')]


@pytest.mark.parametrize('data', [None, [], {}, {'version': 2, 'chapters': []},
                                   {'version': 1, 'chapters': 'chapter.txt'}])
def test_manifest_rejects_invalid_schema(tmp_path, data):
    (tmp_path / EXTRACTION_MANIFEST).write_text(json.dumps(data))
    with pytest.raises(ValueError, match='Invalid extraction manifest'):
        extracted_chapter_paths(tmp_path)


def test_manifest_rejects_invalid_json(tmp_path):
    (tmp_path / EXTRACTION_MANIFEST).write_text('{')
    with pytest.raises(ValueError, match='Invalid extraction manifest'):
        extracted_chapter_paths(tmp_path)


@pytest.mark.parametrize('name', ['../outside.txt', '/outside.txt', r'C:\outside.txt',
                                  r'folder\chapter.txt', 'folder/chapter.txt', 'missing.txt',
                                  'audio.wav', '', None, 12, 'bad\0.txt', 'chapter.txt:stream.txt'])
def test_manifest_rejects_invalid_outside_and_missing_paths(tmp_path, name):
    (tmp_path / EXTRACTION_MANIFEST).write_text(json.dumps({'version': 1, 'chapters': [name]}))
    with pytest.raises(ValueError, match='Invalid extraction manifest'):
        extracted_chapter_paths(tmp_path)


def test_manifest_rejects_symlink_outside_directory(tmp_path):
    outside = tmp_path / 'outside.txt'
    outside.write_text('Outside')
    directory = tmp_path / 'chapters'
    directory.mkdir()
    try:
        (directory / 'link.txt').symlink_to(outside)
    except OSError:
        pytest.skip('Symlinks require unavailable platform privileges')
    (directory / EXTRACTION_MANIFEST).write_text(json.dumps({'version': 1, 'chapters': ['link.txt']}))
    with pytest.raises(ValueError, match='outside'):
        extracted_chapter_paths(directory)


def test_failed_save_preserves_previous_manifest(tmp_path, monkeypatch):
    source = tmp_path / 'book.epub'
    source.touch()
    output = tmp_path / 'chapters'
    chapters = [{'title': 'First', 'text': 'First chapter.'},
                {'title': 'Second', 'text': 'Second chapter.'}]
    monkeypatch.setattr('extract.parse_epub_content', lambda *args, **kwargs: chapters)
    extract_book(source, output_dir=output)
    manifest = output / EXTRACTION_MANIFEST
    previous_manifest = manifest.read_bytes()
    original_open = builtins.open

    def fail_second_chapter(path, mode='r', *args, **kwargs):
        if mode == 'w' and Path(path).name == '2_Second.txt':
            raise OSError('Disk full')
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, 'open', fail_second_chapter)
    with pytest.raises(OSError, match='Disk full'):
        extract_book(source, output_dir=output)
    assert manifest.read_bytes() == previous_manifest
    assert not list(output.glob('.pdf-narrator-chapters-*.tmp'))


def test_failed_manifest_publish_preserves_previous_manifest(tmp_path, monkeypatch):
    source = tmp_path / 'book.txt'
    source.write_text('A book.')
    output = tmp_path / 'chapters'
    extract_book(source, output_dir=output)
    manifest = output / EXTRACTION_MANIFEST
    previous_manifest = manifest.read_bytes()

    def fail_replace(*args):
        raise OSError('Publish failed')

    monkeypatch.setattr('extract.os.replace', fail_replace)
    with pytest.raises(OSError, match='Publish failed'):
        extract_book(source, output_dir=output)
    assert manifest.read_bytes() == previous_manifest
    assert not list(output.glob('.pdf-narrator-chapters-*.tmp'))


def test_manifest_rejects_duplicate_chapters(tmp_path):
    (tmp_path / 'chapter.txt').write_text('A chapter.')
    (tmp_path / EXTRACTION_MANIFEST).write_text(json.dumps({
        'version': 1, 'chapters': ['chapter.txt', 'chapter.txt']}))
    with pytest.raises(ValueError, match='duplicate chapters'):
        extracted_chapter_paths(tmp_path)


def test_empty_extraction_records_no_chapters(tmp_path, monkeypatch):
    source = tmp_path / 'empty.epub'
    source.touch()
    output = tmp_path / 'chapters'
    output.mkdir()
    unrelated = output / 'old.txt'
    unrelated.write_text('An older chapter.')
    monkeypatch.setattr('extract.parse_epub_content', lambda *args, **kwargs: [])
    extract_book(source, output_dir=output)
    assert extracted_chapter_paths(output) == []
    assert unrelated.read_text() == 'An older chapter.'


def test_scanned_pdf_publishes_manifest(tmp_path, monkeypatch):
    source = tmp_path / 'scanned.pdf'
    source.touch()
    output = tmp_path / 'chapters'
    monkeypatch.setattr('extract.get_pdf_type', lambda path: {'is_scanned': True})
    monkeypatch.setattr('extract.scanned_pdf', lambda path: 'A scanned book with twelve words.')
    extract_book(source, output_dir=output)
    assert extracted_chapter_paths(output) == [str(output / 'scanned_full_text.txt')]


@pytest.mark.parametrize('extension', ['.pdf', '.html', '.epub'])
def test_extraction_preserves_real_chapter_titles(tmp_path, extension):
    source = tmp_path / f'book{extension}'
    title = 'Arrival: the Sea & Sky'
    if extension == '.pdf':
        write_book(source, 'A chapter about the sea and sky.')
        expected = 'First chapter'
    elif extension == '.html':
        source.write_text(f'<html><body><h1>{title}</h1><p>A story.</p></body></html>')
        expected = title
    else:
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('OEBPS/heading.xhtml', f'<html><body><h1>{title}</h1><p>A story.</p></body></html>')
            archive.writestr('OEBPS/nav.xhtml', '<html><body><nav><a href="heading.xhtml">The Real TOC Title</a></nav></body></html>')
            archive.writestr('META-INF/container.xml', '<container><rootfiles><rootfile full-path="OEBPS/heading.opf"/></rootfiles></container>')
            archive.writestr('OEBPS/heading.opf', '<package><manifest><item id="chapter" href="heading.xhtml" media-type="application/xhtml+xml"/><item id="nav" properties="nav" href="nav.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="chapter"/></spine></package>')
        expected = 'The Real TOC Title'
    output = tmp_path / 'output'
    extract_book(source, output_dir=output)
    paths = extracted_chapter_paths(output)
    assert extracted_chapter_titles(output) == {Path(paths[0]).name: expected}


def test_epub_heading_is_used_when_toc_is_absent(tmp_path):
    source = tmp_path / 'book.epub'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('META-INF/container.xml', '<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>')
        archive.writestr('book.opf', '<package><manifest><item id="chapter" href="part01.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="chapter"/></spine></package>')
        archive.writestr('part01.xhtml', '<html><body><h2>At the Shore</h2><p>The story begins.</p></body></html>')
    output = tmp_path / 'output'
    extract_book(source, output_dir=output)
    assert list(extracted_chapter_titles(output).values()) == ['At the Shore']


def test_untitled_books_and_old_manifests_do_not_guess_titles(tmp_path):
    for extension in ['.txt', '.epub']:
        source = tmp_path / f'book{extension}'
        write_book(source, 'An untitled story.')
        output = tmp_path / extension.lstrip('.')
        extract_book(source, output_dir=output)
        assert extracted_chapter_titles(output) == {}
    output = tmp_path / 'manual'
    output.mkdir()
    (output / '1_My_File.txt').write_text('A chapter.')
    assert extracted_chapter_titles(output) == {}
    (output / EXTRACTION_MANIFEST).write_text(json.dumps({'version': 1, 'chapters': ['1_My_File.txt']}))
    assert extracted_chapter_titles(output) == {}


@pytest.mark.parametrize('titles', [[], None, {'missing.txt': 'Wrong owner'}, {'chapter.txt': 2}, {'chapter.txt': '   '}])
def test_manifest_rejects_invalid_titles(tmp_path, titles):
    (tmp_path / 'chapter.txt').write_text('A chapter.')
    (tmp_path / EXTRACTION_MANIFEST).write_text(json.dumps({'version': 1, 'chapters': ['chapter.txt'], 'titles': titles}))
    with pytest.raises(ValueError, match='Invalid extraction manifest'):
        extracted_chapter_paths(tmp_path)


def test_epub2_ncx_title_wins_over_heading_and_filename(tmp_path):
    source = tmp_path / 'book.epub'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('META-INF/container.xml', '<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>')
        archive.writestr('book.opf', '<package><manifest><item id="chapter" href="part01.xhtml" media-type="application/xhtml+xml"/><item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/></manifest><spine toc="ncx"><itemref idref="chapter"/></spine></package>')
        archive.writestr('toc.ncx', '<ncx><navMap><navPoint id="one"><navLabel><text>A NCX-only Title: Arrival</text></navLabel><content src="part01.xhtml#start"/></navPoint></navMap></ncx>')
        archive.writestr('part01.xhtml', '<html><body><h1>Wrong Heading</h1><p>The story begins.</p></body></html>')
    output = tmp_path / 'output'
    extract_book(source, output_dir=output)
    assert list(extracted_chapter_titles(output).values()) == ['A NCX-only Title: Arrival']
