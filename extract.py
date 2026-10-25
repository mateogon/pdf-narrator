import regex as re
import os
import sys
import zipfile
import json
from pathlib import Path
import tempfile
import time
import unicodedata # For normalization
from bs4 import BeautifulSoup # For improved EPUB parsing and HTML extraction
from num2words import num2words
import traceback # For detailed error logging if needed
import pymupdf

# --- Configuration ---
HEADER_THRESHOLD = 50 # Pixels from top to ignore
FOOTER_THRESHOLD = 50 # Pixels from bottom to ignore
# MIN_BLOCK_WIDTH_RATIO = 0.1 # Minimum block width relative to page width (Removed for now, can be noisy)
# MIN_BLOCK_HEIGHT_RATIO = 0.1 # Minimum block height relative to page height (Removed for now, can be noisy)
OVERLAP_CHECK_LINES = 20 # Number of lines to check for overlap between chapters

# --- Text Cleaning and Processing Functions ---
# ... (Keep normalize_text, expand_abbreviations_and_initials, convert_numbers,
#      handle_sentence_ends_and_pauses, remove_artifacts, join_wrapped_lines,
#      basic_html_to_text, clean_pipeline - all UNCHANGED) ...

def normalize_text(text):
    """Apply Unicode normalization and fix common problematic characters."""
    # NFKC decomposes ligatures and compatibility characters
    text = unicodedata.normalize('NFKC', text)
    # Specific replacements for characters normalization might not handle as desired
    text = text.replace('—', ', ') # Em dash with comma space (often better for TTS pause)
    text = text.replace('–', ', ') # En dash
    text = text.replace('«', '"').replace('»', '"') # Guillemets to standard quotes
    # Replace various apostrophe/quote types with standard ones
    text = text.replace(chr(8216), "'").replace(chr(8217), "'") # ‘ ’ -> '
    text = text.replace(chr(8220), '"').replace(chr(8221), '"') # “ ” -> "
    # Add spaces around hyphens used as separators (like in ranges, if desired)
    # text = re.sub(r'(?<=\w)-(?=\w)', ' - ', text) # Optional: might affect compound words

    # Fix specific odd characters observed (add more if found)
    # text = text.replace('ĕ', 'e') # If 'ĕ' consistently represents 'e' due to font issues
    # text = text.replace('', 'Th') # If Th ligature consistently causes issues

    return text

def expand_abbreviations_and_initials(text):
    """Expand common abbreviations and fix spaced initials."""
    abbreviations = {
        r'\bMr\.': 'Mister', r'\bMrs\.': 'Misses', r'\bMs\.': 'Miss', r'\bDr\.': 'Doctor',
        r'\bProf\.': 'Professor', r'\bJr\.': 'Junior', r'\bSr\.': 'Senior',
        r'\bvs\.': 'versus', r'\betc\.': 'etcetera', r'\bi\.e\.': 'that is',
        r'\be\.g\.': 'for example', r'\bcf\.': 'compare', r'\bSt\.': 'Saint', # Changed St. -> Saint, more common? Or 'Street'? Needs context. Assume Saint for now.
        r'\bVol\.': 'Volume', r'\bNo\.': 'Number', r'\bpp\.': 'pages', r'\bp\.': 'page',
        # Add more domain-specific ones if needed
    }
    # Expand standard abbreviations
    for abbr, expansion in abbreviations.items():
        text = re.sub(abbr, expansion, text, flags=re.IGNORECASE)

    # Fix initials like "E. B. White" -> "E B White"
    # Looks for sequences of (CapitalLetter + Period + OptionalSpace) followed by another CapitalLetter
    # Using positive lookahead to handle sequences correctly.
    text = re.sub(r'([A-Z])\.(?=\s*[A-Z])', r'\1', text)
    # Clean up any potential double spaces left by the above
    text = re.sub(r' +', ' ', text)

    return text

def convert_numbers(text):
    """Convert English integers, ordinals and years; preserve decimals."""
    def replace_match(match):
        number = match.group(1).replace(',', '')
        suffix = match.group(2)
        if '.' in number:
            return match.group(0)
        try:
            value = int(number)
            if suffix:
                return num2words(value, to='ordinal')
            if 1500 <= value <= 2100:
                return num2words(value, to='year')
            return num2words(value)
        except ValueError:
            return match.group(0)

    pattern = r'(?<![\w.,])([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)(st|nd|rd|th)?(?!\w|[.,][0-9])'
    return re.sub(pattern, replace_match, text, flags=re.IGNORECASE)

def handle_sentence_ends_and_pauses(text):
    """Ensure sentences end cleanly and handle potential pauses."""
    # Add a space before punctuation if missing (helps TTS parsing)
    text = re.sub(r'(?<=\w)([.,!?;:])(?!\d)', r' \1', text)
    # Normalize multiple spaces
    text = re.sub(r' +', ' ', text)

    # Ensure common sentence endings have a period if missing (e.g. lists)
    # This is less aggressive than forcing periods everywhere
    lines = text.splitlines()
    processed_lines = []
    for line in lines:
        stripped_line = line.strip()
        # Add period if line isn't empty, doesn't end in punctuation, isn't a list item, and has enough words
        if stripped_line and \
           not re.search(r'[.!?;:]$', stripped_line) and \
           not re.match(r'^[-\*\u2022•\d+\.\s]+', stripped_line) and \
           len(stripped_line.split()) > 3:
             line += '.' # Add period only if it seems like a sentence fragment needing termination
        processed_lines.append(line)
    text = '\n'.join(processed_lines)

    # Replace semicolons with commas (often better for TTS pause)
    text = text.replace(';', ',')
    # Replace hyphens used as pauses (like in dialogues) with commas - more specific pattern
    text = re.sub(r'\s+-\s+', ', ', text) # Requires space around hyphen
    # Optional: Replace exclamation marks with periods if excitement is not desired
    # text = text.replace('!', '.')
    # Optional: Replace question marks with periods if intonation is not desired
    # text = text.replace('?', '.')

    # Add newline after sentence-ending punctuation for potential TTS break cues
    # Ensure space doesn't exist before newline, add it if needed for clarity.
    # This version adds newline *after* the punctuation and a space.
    text = re.sub(r'((?<!\d)[.!?:]|[.!?:](?!\d))\s*', r'\1\n', text) # Ensures newline separation after sentence ends

    return text

def remove_artifacts(text):
    """Remove common extraction artifacts like citations, excessive newlines etc."""
    # Remove bracketed numbers (citations, footnotes)
    text = re.sub(r'\[\s*\d+\s*\]', '', text)
    # Remove page numbers (simple standalone numbers on a line) - might need refinement
    text = re.sub(r'^\s*\d+\s*$', '', text, flags=re.MULTILINE)
    # Remove lines that are just punctuation (often artifacts)
    text = re.sub(r'^\s*[.,;:!?\-—–_]+\s*$', '', text, flags=re.MULTILINE)
    # Collapse multiple blank lines into a single blank line
    text = re.sub(r'\n\s*\n', '\n\n', text)
    # Remove leading/trailing whitespace from the whole text
    text = text.strip()
    return text

def join_wrapped_lines(text):
    """Join lines that seem to be wrapped mid-sentence. More robust."""
    lines = text.splitlines()
    result_lines = []
    if not lines:
        return ""

    buffer = lines[0]
    for i in range(1, len(lines)):
        current_line = lines[i]
        prev_line_stripped = buffer.strip() # Check the buffered content so far

        # Heuristic: Join if previous line doesn't end with sentence punctuation
        # AND current line doesn't look like a new paragraph/header/list item
        # This tries to avoid joining across paragraphs or list items.
        if (prev_line_stripped and # Don't join if buffer is empty
            not re.search(r'[.!?:)"»’]$', prev_line_stripped) and # Doesn't end like a sentence
            not re.match(r'^[\sA-Z\d"«‘\[\*\-\u2022•]', current_line.strip()) and # Current line doesn't start like a new para/header/list
            len(prev_line_stripped.split()) > 1): # Avoid joining single-word lines too eagerly
             # Join with a space
             buffer += " " + current_line.strip()
        else:
             # If previous line looks like end of sentence, or current line looks like start of new section
             result_lines.append(buffer) # Add completed buffer
             buffer = current_line # Start new buffer

    result_lines.append(buffer) # Add the last buffer content

    # Filter out potentially empty lines created during processing before joining
    return '\n'.join(filter(None, [line.strip() for line in result_lines]))


def basic_html_to_text(html_content):
    """Extract text from HTML using BeautifulSoup, removing scripts/styles."""
    soup = BeautifulSoup(html_content, 'html.parser')

    # Remove script and style elements
    for script_or_style in soup(["script", "style"]):
        script_or_style.decompose()

    # Get text, joining paragraphs/blocks with double newlines
    # Use strip=True to remove extra whitespace around tags
    # Use separator='\n' to ensure block elements get newlines between them
    text = soup.get_text(separator='\n', strip=True)

    # Collapse multiple spaces resulting from inline tags
    text = re.sub(r'[ \t]+', ' ', text)
    # Collapse multiple newlines into max two (paragraph break)
    text = re.sub(r'\n\s*\n', '\n\n', text)

    return text

def clean_pipeline(text, language='en'):
    """Apply the full cleaning pipeline in order."""
    if not text: return ""
    # print("--- Before clean ---\n", text[:500]) # Debug
    text = normalize_text(text)
    # print("--- After normalize ---\n", text[:500]) # Debug
    text = join_wrapped_lines(text) # Join lines SHOULD be early
    # print("--- After join lines ---\n", text[:500]) # Debug
    # These substitutions are English-specific; let Kokoro normalize other languages.
    if language.lower() in {'a', 'b', 'en', 'en-us', 'en-gb'}:
        text = expand_abbreviations_and_initials(text)
        text = convert_numbers(text)
        text = handle_sentence_ends_and_pauses(text)
    # print("--- After abbreviations ---\n", text[:500]) # Debug
    # print("--- After numbers ---\n", text[:500]) # Debug
    # print("--- After sentence ends ---\n", text[:500]) # Debug
    text = remove_artifacts(text)
    # print("--- After artifacts ---\n", text[:500]) # Debug
    # Final whitespace cleanup
    text = re.sub(r' +', ' ', text)
    # Consolidate newlines: single newlines for within-paragraph breaks, double for paragraph ends
    text = re.sub(r'\n\n+', '\n\n', text) # Ensure max 2 newlines
    text = text.strip()
    # print("--- After final cleanup ---\n", text[:500]) # Debug
    return text

# --- PDF Extraction ---

def extract_pdf_text_by_page(doc):
    """
    Extracts text page by page from PDF, filtering headers/footers.

    Returns:
        list[str]: A list where each element is the text content of a page.
    """
    all_pages_text = []
    for page_num in range(len(doc)):
        page = doc.load_page(page_num)
        page_height = page.rect.height
        # page_width = page.rect.width # Not currently used but available

        # Extract text blocks
        blocks = page.get_text("blocks", flags=pymupdf.TEXTFLAGS_TEXT) # Basic flags
        filtered_lines = []
        for block in blocks:
            x0, y0, x1, y1, text, *_ = block
            # Filter by position (header/footer)
            if y1 < HEADER_THRESHOLD or y0 > page_height - FOOTER_THRESHOLD:
                continue

            # Simple text cleaning per block (remove excess internal whitespace)
            cleaned_block_text = re.sub(r'\s+', ' ', text).strip()
            if cleaned_block_text:
                filtered_lines.append(cleaned_block_text)

        page_text = "\n".join(filtered_lines) # Join blocks with newline for structure within page
        all_pages_text.append(page_text)
    return all_pages_text

def get_pdf_type(file_path):
    result = {
        'is_scanned': False,
        'confidence': 'Low',
        'details': {}
    }
    try:
        doc = pymupdf.open(file_path)
        # Analyze first page (or more pages for better accuracy)
        page = doc[0]
        # Method 1: Check for text
        sentences = page.get_text().splitlines()
        text = ". ".join([s for s in sentences if all(["copywrite" not in s, "permission" not in s, "reproduce" not in s])])
        result['details']['text_length'] = len(text)
        # Method 2: Check for images
        image_list = page.get_images()
        result['details']['image_count'] = len(image_list)
        # Method 3: Check for fonts
        fonts = page.get_fonts()
        result['details']['font_count'] = len(fonts)
        # Analysis logic
        if len(text) < 10 and len(image_list) > 0:
            # If page has almost no text but has images, likely scanned
            result['is_scanned'] = True
            result['confidence'] = 'High'
        elif len(fonts) == 0 and len(image_list) > 0:
            # No fonts but has images, likely scanned
            result['is_scanned'] = True
            result['confidence'] = 'High'
        elif len(text) > 100 and len(fonts) > 0:
            # Substantial text and fonts present, likely native
            result['is_scanned'] = False
            result['confidence'] = 'High'
        # Method 4: Check page rotation
        # Scanned documents often have rotation metadata
        result['details']['rotation'] = page.rotation
        if page.rotation != 0 and result['is_scanned']:
            result['confidence'] = 'High'
        doc.close()
    except Exception as e:
        result['details']['error'] = str(e)
        result['confidence'] = 'Low'
    
    return result

def scanned_pdf(path):
    """Read scanned pages only when the optional OCR tools are installed."""
    try:
        import pytesseract
        from pdf2image import convert_from_path
    except ImportError as exc:
        raise RuntimeError(
            "Scanned PDF requires OCR: run 'uv run --extra ocr pdf-narrator', then install "
            "Tesseract and Poppler for your system."
        ) from exc
    from shutil import which
    if not which('tesseract') or not which('pdftocairo') or not which('pdfinfo'):
        raise RuntimeError(
            "Scanned PDF requires Tesseract and Poppler on PATH "
            "(tesseract, pdftocairo and pdfinfo). See README.md."
        )
    pages = convert_from_path(path, dpi=300, use_pdftocairo=True)
    out = []
    for page in pages:
        width, height = page.size
        cropped = page.crop((0, int(height * 0.1), width, int(height * 0.9)))
        binary = cropped.convert('L').point(lambda pixel: 255 if pixel > 200 else 0)
        text = pytesseract.image_to_string(binary, timeout=30)
        lines = text.splitlines()
        filtered = [line for line in lines if not line.strip().isdigit() and "copyright" not in line.lower()]
        out.append(" ".join(filtered))
    return ". ".join(out)

# --- TOC and Chapter Structuring ---

def get_toc(doc):
    """Extract TOC from PDF."""
    toc = doc.get_toc()
    if not toc:
        print("  No Table of Contents found in the document.")
        return []
    else:
        print(f"  Table of Contents extracted with {len(toc)} entries.")
        return toc

def deduplicate_toc(toc):
    """Removes TOC entries that point to the exact same page number."""
    seen_pages = set()
    deduplicated_toc = []
    for entry in toc:
        level, title, page_number = entry
        if page_number not in seen_pages:
            deduplicated_toc.append(entry)
            seen_pages.add(page_number)
        else:
            print(f"    Info: Duplicate TOC entry page removed: Level {level}, '{title}', Page {page_number}")
    return deduplicated_toc

def remove_overlap(prev_text, curr_text, num_lines=OVERLAP_CHECK_LINES):
    """
    Checks if the end of prev_text overlaps with the start of curr_text
    and returns prev_text with the overlap removed. Based on line comparison.
    """
    if not prev_text or not curr_text:
        return prev_text

    prev_lines = prev_text.splitlines()
    curr_lines = curr_text.splitlines()

    # Don't check if either text is too short
    if not prev_lines or not curr_lines:
        return prev_text

    max_possible_overlap = min(len(prev_lines), len(curr_lines), num_lines)

    for overlap_size in range(max_possible_overlap, 0, -1):
        # Compare last `overlap_size` lines of prev with first `overlap_size` lines of curr
        prev_suffix = prev_lines[-overlap_size:]
        curr_prefix = curr_lines[:overlap_size]

        # Basic check: are the lines identical?
        # More robust checks could involve fuzzy matching, but start simple.
        if prev_suffix == curr_prefix:
            print(f"    Overlap detected ({overlap_size} lines). Removing from previous chapter end.")
            # Return previous text excluding the overlapping lines
            return "\n".join(prev_lines[:-overlap_size])

    # No overlap found
    return prev_text


def structure_pdf_by_toc(deduplicated_toc, all_pages_text, language='en'):
    """
    Structures the PDF text into chapters based on TOC page numbers,
    applies cleaning pipeline per chapter, and removes overlap.

    Args:
        deduplicated_toc (list): List of [level, title, page_num] entries.
        all_pages_text (list[str]): List of text content for each page.

    Returns:
        list[dict]: List of chapters, each {'level': int, 'title': str, 'text': str}.
    """
    chapters = []
    num_pages_total = len(all_pages_text)
    print(f"  Structuring PDF text ({num_pages_total} pages) using {len(deduplicated_toc)} TOC entries...")

    last_processed_chapter = None # Store {'level': ..., 'title': ..., 'text': ...}

    for i, entry in enumerate(deduplicated_toc):
        level, title, start_page = entry
        start_page_idx = start_page - 1 # 0-based index

        # Determine end page index
        if i < len(deduplicated_toc) - 1:
            _, _, next_start_page = deduplicated_toc[i + 1]
            # End page is the page *before* the next chapter starts.
            # Handle cases where next chapter starts on the same page (use at least one page).
            end_page_idx = max(start_page_idx, next_start_page - 2) # -1 for index, -1 for previous page
        else:
            # Last chapter goes to the end of the document
            end_page_idx = num_pages_total - 1

        # Validate page indices
        if start_page_idx < 0 or start_page_idx >= num_pages_total:
            print(f"    Warning: Invalid start page index ({start_page_idx}) for TOC entry '{title}'. Skipping.")
            continue
        if end_page_idx < start_page_idx:
             print(f"    Info: Chapter '{title}' seems to have zero pages (start={start_page}, next={deduplicated_toc[i+1][2] if i < len(deduplicated_toc)-1 else 'End'}). Assigning one page.")
             end_page_idx = start_page_idx # Assign at least the start page
        elif end_page_idx >= num_pages_total:
             print(f"    Warning: Calculated end page index ({end_page_idx}) out of bounds. Clamping to max page ({num_pages_total - 1}).")
             end_page_idx = num_pages_total - 1


        # Extract pages for this chapter
        # Slicing is [start:end+1]
        chapter_pages = all_pages_text[start_page_idx : end_page_idx + 1]
        raw_chapter_text = "\n".join(chapter_pages) # Join pages for the chapter

        # Clean the extracted chapter text using the pipeline
        cleaned_chapter_text = clean_pipeline(raw_chapter_text, language=language)

        # Clean the title
        clean_title = title.strip()

        # --- Overlap Removal ---
        # If we have processed a previous chapter, check and remove overlap from IT
        if last_processed_chapter:
            # Compare the end of the *previous* chapter's cleaned text
            # with the start of the *current* chapter's cleaned text.
            previous_text_no_overlap = remove_overlap(
                last_processed_chapter['text'],
                cleaned_chapter_text
            )
            # Update the previous chapter's text in our list (or temp storage)
            last_processed_chapter['text'] = previous_text_no_overlap
            # Now add the potentially modified *previous* chapter to the final list
            chapters.append(last_processed_chapter)

        # Store the current chapter details (it will be checked for overlap by the *next* iteration)
        last_processed_chapter = {'level': level, 'title': clean_title, 'text': cleaned_chapter_text}

    # Add the very last processed chapter (which wasn't added in the loop)
    if last_processed_chapter:
        chapters.append(last_processed_chapter)

    # Filter out chapters that ended up empty after cleaning/overlap removal
    final_chapters = [chap for chap in chapters if chap.get('text')]
    print(f"  Finished structuring. Found {len(final_chapters)} non-empty chapters.")
    return final_chapters

# --- Heuristic Chapter Splitting (Fallback for PDF without TOC) ---
def split_text_into_heuristic_chapters(full_raw_text, language='en'):
    """
    Attempts to split raw text into chapters based on heuristics like
    multiple newlines or potential chapter-like headings.

    Args:
        full_raw_text (str): The combined raw text from all PDF pages.

    Returns:
        list[dict]: List of chapters [{'title': 'Chapter N', 'text': cleaned_chunk}, ...],
                    or an empty list if splitting fails or text is empty.
    """
    if not full_raw_text or not full_raw_text.strip():
        return []

    print("    Attempting heuristic chapter splitting...")

    # --- Strategy 1: Split by multiple newlines (common section break) ---
    # Use 3 or more newlines as a strong indicator of a major break.
    # This needs to happen *before* aggressive whitespace cleaning.
    potential_chunks = re.split(r'\n\s*\n+', full_raw_text) # 3+ newlines with optional whitespace

    # --- Refine Chunks (Basic filtering) ---
    chapters = []
    chapter_count = 0
    min_chunk_length = 100 # Avoid tiny fragments being called chapters

    for i, chunk in enumerate(potential_chunks):
        trimmed_chunk = chunk.strip()
        if len(trimmed_chunk) > min_chunk_length:
            chapter_count += 1
            # Apply the full cleaning pipeline *to each chunk*
            cleaned_chunk_text = clean_pipeline(trimmed_chunk, language=language)
            if cleaned_chunk_text: # Ensure cleaning didn't make it empty
                chapters.append({
                    'title': f'Chapter_{chapter_count}', # Generic filename
                    'display_title': None,
                    'level': None, # No level info available
                    'text': cleaned_chunk_text
                })
        # else: # Optional: Log discarded small chunks
            # print(f"      Discarding small chunk {i+1} (length {len(trimmed_chunk)})")


    # --- Alternative/Future Strategy (More Complex): Look for Header Patterns ---
    # This would involve regex for "CHAPTER X", "Part Y", lines in ALL CAPS, etc.
    # Requires more careful implementation to avoid false positives.
    # Example (conceptual, not fully implemented):
    # chapter_start_pattern = r'^\s*(?:CHAPTER|PART)\s+[IVXLCDM\d]+.*$|^\s*[A-Z\s]{5,}\s*$' # Regex for CHAPTER X or ALL CAPS lines
    # if not chapters: # Only if newline split failed
    #    lines = full_raw_text.splitlines()
    #    current_chapter_lines = []
    #    for line in lines:
    #        if re.match(chapter_start_pattern, line, re.IGNORECASE) and current_chapter_lines:
    #             # Found potential new chapter, process the previous one
    #             # ... (logic to save previous chapter, clean it, etc.) ...
    #             current_chapter_lines = [line]
    #        else:
    #             current_chapter_lines.append(line)
    #    # Process the last chapter

    if chapters:
        print(f"    Heuristically split into {len(chapters)} potential chapters.")
    else:
        print("    Heuristic splitting did not yield significant chapters.")

    return chapters

# --- EPUB Extraction ---
# ... (Keep parse_epub_content UNCHANGED) ...
def parse_epub_content(epub_path, progress_callback=None, language='en'):
    """
    Extracts and cleans text content from EPUB using BeautifulSoup.

    Returns:
        list[dict]: A list of chapters, each with 'title' (filename) and 'text'.
    """
    chapters = []
    print(f"  Processing EPUB: '{os.path.basename(epub_path)}'")
    extracted_files_count = 0

    try:
        with zipfile.ZipFile(epub_path, 'r') as epub_zip:
            # Find the OPF file (usually content.opf)
            opf_path = None
            container_xml = epub_zip.read('META-INF/container.xml').decode('utf-8')
            container_soup = BeautifulSoup(container_xml, 'xml')
            opf_relative_path = container_soup.find('rootfile')
            if opf_relative_path and opf_relative_path.get('full-path'):
                 opf_path = opf_relative_path.get('full-path')
            else: # Fallback: search manually
                for item in epub_zip.namelist():
                     if item.lower().endswith('.opf'):
                        opf_path = item
                        break

            if not opf_path:
                print("  Error: Could not find OPF file in EPUB via container.xml or direct search.")
                # Fallback: process all HTML/XHTML files naively
                content_files = sorted([f for f in epub_zip.namelist() if f.lower().endswith(('.html', '.xhtml', '.htm'))])
                manifest_items = {} # No manifest known
                spine_order = content_files # Assume alphabetical order is spine order
                opf_soup = None
                epub_base_path = '' # No reliable base path
                print(f"  Falling back to processing {len(content_files)} HTML files found.")

            else:
                print(f"  Found OPF file: '{opf_path}'")
                # Parse OPF to get manifest and spine
                opf_content = epub_zip.read(opf_path).decode('utf-8', errors='ignore')
                opf_soup = BeautifulSoup(opf_content, 'xml') # Use 'xml' parser for OPF
                manifest_items = {}
                for item in opf_soup.find('manifest').find_all('item'):
                    item_id = item.get('id')
                    item_href = item.get('href')
                    if item_id and item_href:
                         manifest_items[item_id] = {'href': item_href, 'media-type': item.get('media-type')}

                spine = opf_soup.find('spine')
                spine_order_refs = []
                if spine:
                    spine_order_refs = [item.get('idref') for item in spine.find_all('itemref')]
                else:
                     print("  Warning: Could not find <spine> in OPF. Extraction order might be incorrect.")
                     # Fallback: Use manifest items that are HTML, hoping order is somewhat meaningful
                     spine_order_refs = [id for id, item in manifest_items.items() if 'html' in item.get('media-type', '')]
                     # Attempt to sort by href, often includes numbers
                     spine_order_refs.sort(key=lambda idref: manifest_items[idref]['href'])


                print(f"  Found {len(manifest_items)} manifest items and {len(spine_order_refs)} spine references.")
                # Base path is the directory containing the OPF file
                epub_base_path = os.path.dirname(opf_path) if '/' in opf_path else ''

            # --- NCX/Nav TOC Parsing (Optional but good for titles) ---
            toc_map = {} # Map hrefs to titles
            nav_href = None
            # Try EPUB3 Nav document first
            nav_item = opf_soup.find('item', {'properties': 'nav'}) if opf_soup else None
            if nav_item:
                nav_href = nav_item.get('href')
            else: # Try EPUB2 NCX
                spine_toc_id = spine.get('toc') if spine else None
                if spine_toc_id and spine_toc_id in manifest_items:
                    nav_href = manifest_items[spine_toc_id].get('href')

            if nav_href:
                try:
                    nav_full_path = os.path.normpath(os.path.join(epub_base_path, nav_href)).replace('\\', '/')
                    nav_content = epub_zip.read(nav_full_path).decode('utf-8', errors='ignore')
                    nav_soup = BeautifulSoup(nav_content, 'xml') # NCX tag names are case-sensitive.

                    # EPUB3 Nav: Look for <nav type="toc"> links
                    nav_element = nav_soup.find('nav', {'epub:type': 'toc'}) or nav_soup.find('nav') # Fallback to first nav
                    if nav_element:
                         print(f"  Parsing EPUB3 Nav TOC from '{nav_full_path}'...")
                         for link in nav_element.find_all('a'):
                             href = link.get('href')
                             title = link.get_text(strip=True)
                             if href:
                                 # Resolve relative href against nav file path
                                 abs_href = os.path.normpath(os.path.join(os.path.dirname(nav_full_path), href)).replace('\\', '/')
                                 # Store base filename as key
                                 toc_map[abs_href.split('#')[0]] = title # Remove fragment
                    # EPUB2 NCX: Look for <navPoint> elements
                    elif nav_soup.find('navMap'):
                         print(f"  Parsing EPUB2 NCX TOC from '{nav_full_path}'...")
                         for nav_point in nav_soup.find_all('navPoint'):
                             content = nav_point.find('content')
                             nav_label = nav_point.find('navLabel')
                             if content and nav_label:
                                 src = content.get('src')
                                 title = nav_label.get_text(strip=True)
                                 if src:
                                     # Resolve relative src against NCX file path
                                     abs_src = os.path.normpath(os.path.join(os.path.dirname(nav_full_path), src)).replace('\\', '/')
                                     # Store base filename as key
                                     toc_map[abs_src.split('#')[0]] = title # Remove fragment
                except Exception as toc_e:
                    print(f"    Warning: Could not parse TOC file '{nav_href}': {toc_e}")


            # --- Process files in spine order ---
            total_files_in_spine = len(spine_order_refs)
            processed_spine_files = 0

            for i, idref in enumerate(spine_order_refs):
                item = manifest_items.get(idref)
                if not item:
                    # If using fallback where spine_order contains filenames directly
                    if idref in epub_zip.namelist() and idref.lower().endswith(('.html','.xhtml','.htm')):
                        content_path = idref
                        relative_href = idref # Use filename itself
                        item_media_type = 'application/xhtml+xml' # Assume HTML
                    else:
                         print(f"    Skipping spine item: ID '{idref}' not found in manifest.")
                         continue
                else:
                    item_media_type = item.get('media-type', '')
                    if 'html' not in item_media_type and 'xml' not in item_media_type: # Allow xhtml and xml
                        print(f"    Skipping non-HTML/XML spine item: {idref} ({item_media_type})")
                        continue
                    relative_href = item.get('href')
                    # Construct full path within zip relative to OPF directory
                    content_path = os.path.normpath(os.path.join(epub_base_path, relative_href)).replace('\\', '/')

                if progress_callback:
                    progress_callback(10 + int((processed_spine_files / max(1, total_files_in_spine)) * 80))

                try:
                    html_content = epub_zip.read(content_path).decode('utf-8', errors='ignore')
                    print(f"    [{processed_spine_files+1}/{total_files_in_spine}] Reading: '{content_path}'")
                    # Extract text using BeautifulSoup
                    raw_text = basic_html_to_text(html_content)
                    # Apply full cleaning pipeline
                    cleaned_text = clean_pipeline(raw_text, language=language)

                    if cleaned_text: # Only add chapter if it has content
                         # Use TOC title if available, otherwise fallback to filename
                         real_title = toc_map.get(content_path) or _html_chapter_title(html_content)
                         chapter_title = real_title or os.path.basename(relative_href)
                         chapters.append({
                             'title': chapter_title,
                             'display_title': real_title,
                             'text': cleaned_text
                         })
                         extracted_files_count += 1
                    else:
                         print(f"      No text content extracted from '{content_path}'.")
                    processed_spine_files += 1

                except KeyError:
                    print(f"    Error: File path not found in zip for idref '{idref}': '{content_path}'")
                except Exception as e:
                    print(f"    Error processing content file '{content_path}': {e}")
                    # traceback.print_exc() # Uncomment for detailed debug

            print(f"  Successfully extracted text from {extracted_files_count} content files.")
            if progress_callback: progress_callback(95) # Near end before saving

    except zipfile.BadZipFile:
        print(f"  Error: File is not a valid ZIP archive (or EPUB is corrupted): '{epub_path}'")
        raise ValueError(f"Corrupted or invalid EPUB file: {epub_path}") from None
    except Exception as e:
        print(f"  Error opening or processing EPUB file '{epub_path}': {e}")
        # traceback.print_exc() # Uncomment for detailed debug
        raise # Re-raise error

    return chapters

def _html_chapter_title(html_content):
    """Read a heading as metadata, without turning the filename into a title."""
    heading = BeautifulSoup(html_content, 'html.parser').find(['h1', 'h2', 'h3'])
    return heading.get_text(' ', strip=True) if heading is not None else None


# --- TXT Extraction ---
def extract_txt(path):
    with open(path, 'r', encoding="utf-8", errors="ignore") as fin:
        out = fin.read()
    return str(out)

# --- Saving Functions ---
EXTRACTION_MANIFEST = '.pdf-narrator-chapters.json'


def _manifest_chapter_path(directory, name):
    """Reject paths that could select files outside this extraction directory."""
    if not isinstance(name, str):
        raise ValueError("Invalid extraction manifest: chapter paths must be basenames.")
    if not name or any(separator in name for separator in ('/', '\\', ':', '\0')):
        raise ValueError("Invalid extraction manifest: chapter paths must be basenames.")
    if Path(name).suffix.lower() != '.txt':
        raise ValueError("Invalid extraction manifest: chapter paths must be .txt basenames.")
    chapter = directory / name
    if chapter.resolve().parent != directory or not chapter.is_file():
        raise ValueError(f"Invalid extraction manifest: chapter is missing or outside the directory: {name!r}")
    return str(chapter)


def extracted_chapter_paths(directory):
    """Return this extraction's chapters, or all text files in a manual folder."""
    directory = Path(directory).resolve()
    if not directory.is_dir():
        raise ValueError(f"Chapter directory does not exist: {directory}")
    manifest = directory / EXTRACTION_MANIFEST
    if not manifest.exists() and not manifest.is_symlink():
        return [str(path) for path in sorted(directory.iterdir())
                if path.is_file() and path.suffix.lower() == '.txt']
    data = _read_extraction_manifest(manifest)
    paths = [_manifest_chapter_path(directory, name) for name in data['chapters']]
    if len(paths) != len(set(paths)):
        raise ValueError(f"Invalid extraction manifest: duplicate chapters: {manifest}")
    return paths


def _read_extraction_manifest(manifest):
    """Validate the persisted file-list contract before selecting chapters."""
    try:
        data = json.loads(manifest.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Invalid extraction manifest: {manifest}") from exc
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('chapters'), list):
        raise ValueError(f"Invalid extraction manifest schema: {manifest}")
    _manifest_chapter_titles(data)
    return data


def _manifest_chapter_titles(data):
    titles = data.get('titles', {})
    if not isinstance(titles, dict):
        raise ValueError('Invalid extraction manifest: titles must be a mapping.')
    for name, title in titles.items():
        if name not in data['chapters'] or not isinstance(title, str) or not title.strip():
            raise ValueError('Invalid extraction manifest: titles must name current chapters and contain text.')
    return titles


def extracted_chapter_titles(directory):
    """Return real titles keyed by chapter basename; older/manual folders have none."""
    extracted_chapter_paths(directory)
    manifest = Path(directory) / EXTRACTION_MANIFEST
    if not manifest.exists():
        return {}
    return _manifest_chapter_titles(_read_extraction_manifest(manifest))


def _write_extraction_manifest(directory, paths, chapter_titles=None):
    """Publish current files and optional real titles without deleting older files."""
    manifest = Path(directory) / EXTRACTION_MANIFEST
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory,
                                         prefix='.pdf-narrator-chapters-', suffix='.tmp', delete=False) as temporary:
            temporary_path = temporary.name
            data = {'version': 1, 'chapters': [Path(path).name for path in paths]}
            if chapter_titles:
                data['titles'] = chapter_titles
            json.dump(data, temporary)
            temporary.write('\n')
        os.replace(temporary_path, manifest)
    finally:
        if temporary_path is not None:
            Path(temporary_path).unlink(missing_ok=True)


def save_chapters_generic(chapters, book_name, output_dir, chapter_titles=None):
    """Saves chapters (list of dicts with 'title', 'text') to files."""
    if not chapters:
        print("  No chapters found or extracted to save.")
        return []
    os.makedirs(output_dir, exist_ok=True)
    written_paths = []
    num_chapters = len(chapters)
    padding = len(str(num_chapters))
    print(f"  Saving {num_chapters} chapters to '{output_dir}'...")

    for idx, chapter in enumerate(chapters, 1):
        # Title can come from TOC (PDF/EPUB) or filename (EPUB fallback)
        # Level might exist for PDF chapters
        level = chapter.get('level', None) # Get level if available
        title = chapter.get('title') or f'Chapter_{idx}'
        text = chapter.get('text', '')

        # Create a safer filename from the title
        safe_title = re.sub(r'[^\w\s-]', '', title).strip() # Allow word chars, whitespace, hyphen
        safe_title = re.sub(r'\s+', '_', safe_title) # Replace whitespace with underscore
        if not safe_title: safe_title = f"chapter_{idx}"
        # Truncate long filenames if necessary
        max_len = 60 # Limit filename length slightly more generous
        safe_title = safe_title[:max_len]

        # Add level indicator to filename if present (e.g., for PDF subchapters)
        level_prefix = f"L{level}_" if level is not None else ""
        filename = f"{str(idx).zfill(padding)}_{level_prefix}{safe_title}.txt"
        filepath = os.path.join(output_dir, filename)
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(text)
        written_paths.append(filepath)
        real_title = (chapter.get('display_title', chapter.get('title')) or '').strip()
        if chapter_titles is not None and real_title:
            chapter_titles[filename] = real_title

    print(f"  Finished saving chapters.")
    return written_paths

def save_whole_book_text(full_text, book_name, output_dir, language='en'):
    """Cleans and saves the entire book text to a single file."""
    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, f"{book_name}_full_text.txt")
    print(f"  Cleaning full text...")
    cleaned_full_text = clean_pipeline(full_text, language=language) # Apply cleaning pipeline
    print(f"  Saving full text to '{output_file}'...")
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(cleaned_full_text)
    print(f"  Full text saved.")
    return [output_file]


# --- Main Extraction Function ---

def extract_book(file_path, use_toc=True, extract_mode="chapters", output_dir="extracted_books", progress_callback=None, language="en"):
    """
    Extracts text from PDF or EPUB files, cleans it, and saves chapters or whole text
    directly into the specified output_dir. A successful run atomically records
    its exact files in the extraction manifest; older and unrelated files remain.

    Args:
        file_path (str): Path to the input PDF or EPUB file.
        use_toc (bool): If True (and PDF), attempt to use the Table of Contents
                        to structure chapters. If False or TOC fails, behavior
                        depends on extract_mode.
        extract_mode (str): 'chapters' or 'whole'.
                            - 'chapters': Tries to save text into separate chapter files.
                              Uses TOC if available (PDF), spine order (EPUB), or
                              heuristic splitting (PDF fallback).
                            - 'whole': Saves the entire cleaned text into a single file.
        language (str): English defaults to normalization of numbers and abbreviations.
                        Other languages preserve them for the TTS engine.
        output_dir (str): The directory where the output file(s) will be saved.
                          The function will create this directory if it doesn't exist.
                          A subdirectory named after the book might be implicitly created
                          by saving functions depending on their implementation, but the
                          base is `output_dir`.
        progress_callback (callable, optional): A function to call with progress percentage
                                                (0-100) or None on error. Defaults to None.

    Returns:
        str: The absolute path to the output directory used.

    Raises:
        FileNotFoundError: If the input file does not exist.
        ValueError: If the file format is unsupported or the EPUB is invalid.
        Exception: Other errors during processing (e.g., PDF parsing issues).
    """
    start_time = time.time()
    if progress_callback: progress_callback(0)

    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"Input file not found: '{file_path}'")

    file_ext = os.path.splitext(file_path)[1].lower()
    book_name_base = os.path.splitext(os.path.basename(file_path))[0]
    safe_book_name = re.sub(r'[^\w\s-]', '', book_name_base).strip().replace(' ', '_')
    if not safe_book_name: safe_book_name = "unnamed_book"

    # Ensure the final output directory exists
    os.makedirs(output_dir, exist_ok=True)
    absolute_output_dir = os.path.abspath(output_dir) # Use absolute path for clarity

    print(f"--- Starting Extraction for: {os.path.basename(file_path)} ---")
    print(f"    Output directory       : {absolute_output_dir}")
    print(f"    Use TOC                : {use_toc}")
    print(f"    Extraction Mode        : {extract_mode}")

    written_paths = []
    chapter_titles = {}
    try:
        if file_ext == '.pdf':
            print("  Processing PDF file...")
            if progress_callback: progress_callback(5)

            pdf_type = get_pdf_type(file_path)
            print(f"  PDF Type Analysis: Scanned={pdf_type['is_scanned']}")

            if pdf_type['is_scanned']:
                if progress_callback: progress_callback(30)
                doc = scanned_pdf(file_path)
                print("  Performed OCR on scanned PDF.")
                if progress_callback: progress_callback(70)
                written_paths = save_whole_book_text(doc, safe_book_name, absolute_output_dir, language=language)
                _write_extraction_manifest(absolute_output_dir, written_paths, chapter_titles)
                if progress_callback: progress_callback(100)
                elapsed_time = time.time() - start_time
                print(f"--- Extraction completed in {elapsed_time:.2f} seconds ---")
                return absolute_output_dir

            doc = pymupdf.open(file_path)
            print(f"  Opened PDF. Pages: {len(doc)}")

            if progress_callback: progress_callback(10)
            # Always extract page by page first
            all_pages_text = extract_pdf_text_by_page(doc)
            print(f"  Extracted raw text from {len(all_pages_text)} pages.")
            if progress_callback: progress_callback(40)

            # --- Chapter Logic ---
            pdf_chapters = []
            toc_used = False

            if extract_mode == "chapters":
                toc = get_toc(doc)
                dedup_toc = deduplicate_toc(toc) if toc else []

                if use_toc and dedup_toc:
                    print("  Attempting to structure PDF by TOC...")
                    if progress_callback: progress_callback(50)
                    pdf_chapters = structure_pdf_by_toc(dedup_toc, all_pages_text, language=language)
                    if progress_callback: progress_callback(85)
                    if pdf_chapters:
                        toc_used = True
                    else:
                        print("  Structuring by TOC resulted in no chapters. Will attempt fallback.")
                else:
                    if not use_toc: print("  TOC usage disabled.")
                    elif not dedup_toc: print("  No usable TOC found.")
                    print("  Will attempt heuristic chapter splitting.")

                # --- Heuristic Fallback ---
                if not toc_used:
                    if progress_callback: progress_callback(50) # Show progress for heuristic attempt
                    full_raw_text = "\n".join(all_pages_text) # Combine raw pages
                    pdf_chapters = split_text_into_heuristic_chapters(full_raw_text, language=language)
                    if progress_callback: progress_callback(85)

                # --- Save Chapters (if found by either method) ---
                if pdf_chapters:
                    written_paths = save_chapters_generic(pdf_chapters, safe_book_name, absolute_output_dir, chapter_titles=chapter_titles)
                else:
                    # If STILL no chapters after TOC and heuristic, save as whole
                    print("  No chapters found via TOC or heuristics. Saving as whole book text.")
                    full_raw_text = "\n".join(all_pages_text) # Combine raw pages again (splitter might have failed)
                    written_paths = save_whole_book_text(full_raw_text, safe_book_name, absolute_output_dir, language=language) # save_whole cleans the text

            # --- Whole Book Mode ---
            else: # extract_mode == "whole"
                print("  Saving PDF as whole book text.")
                if progress_callback: progress_callback(60)
                full_text = "\n".join(all_pages_text) # Join all pages extracted earlier
                written_paths = save_whole_book_text(full_text, safe_book_name, absolute_output_dir, language=language) # save_whole cleans the text

            doc.close()
            if progress_callback: progress_callback(95)

        elif file_ext == '.epub':
            # --- EPUB Processing (largely unchanged) ---
            print("  Processing EPUB file...")
            epub_chapters = parse_epub_content(file_path, progress_callback, language=language)

            if not epub_chapters:
                 print("  Warning: No content extracted from EPUB.")

            if extract_mode == "chapters":
                 if epub_chapters:
                     written_paths = save_chapters_generic(epub_chapters, safe_book_name, absolute_output_dir, chapter_titles=chapter_titles)
                 else:
                     print("  No EPUB chapters extracted, nothing to save in chapter mode.")
            else: # extract_mode == "whole"
                 if epub_chapters:
                     print("  Combining EPUB chapters into whole book text...")
                     # Join chapters with double newline for paragraph separation between files
                     full_text = "\n\n".join([chap['text'] for chap in epub_chapters if chap.get('text')])
                     written_paths = save_whole_book_text(full_text, safe_book_name, absolute_output_dir, language=language) # save_whole cleans the text
                 else:
                      print("  No EPUB content extracted, nothing to save in whole book mode.")

        elif file_ext == '.txt':
            print("  Processing TXT file...")
            txt_content = extract_txt(file_path)
            if extract_mode == "whole":
                written_paths = save_whole_book_text(txt_content, safe_book_name, absolute_output_dir, language=language)
            else:
                # For TXT in chapter mode, just save as one chapter
                chapter = {'title': 'Full_Text', 'display_title': None, 'text': clean_pipeline(txt_content, language=language)}
                written_paths = save_chapters_generic([chapter], safe_book_name, absolute_output_dir, chapter_titles=chapter_titles)

        elif file_ext == '.html' or file_ext == '.htm':
            print("  Processing HTML file...")
            html_content = Path(file_path).read_text(encoding='utf-8', errors='ignore')
            content = basic_html_to_text(html_content)
            if extract_mode == "whole":
                written_paths = save_whole_book_text(content, safe_book_name, absolute_output_dir, language=language)
            else:
                chapter = {'title': 'Full_Text', 'display_title': _html_chapter_title(html_content), 'text': clean_pipeline(content, language=language)}
                written_paths = save_chapters_generic([chapter], safe_book_name, absolute_output_dir, chapter_titles=chapter_titles)

        else:
            raise ValueError(f"Unsupported file format: '{file_ext}'. Supported: .pdf, .epub, .txt, .html, .htm")

        _write_extraction_manifest(absolute_output_dir, written_paths, chapter_titles)
        elapsed_time = time.time() - start_time
        print(f"--- Extraction completed in {elapsed_time:.2f} seconds ---")
        if progress_callback: progress_callback(100)
        return absolute_output_dir # Return the absolute path

    except Exception as e:
        print(f"!!! Error during extraction for '{file_path}': {e}")
        traceback.print_exc() # Print full traceback for easier debugging
        if progress_callback: progress_callback(None) # Indicate error
        raise # Re-raise the exception


# --- Example Usage (commented out) ---
'''
if __name__ == "__main__":
    # --- Configuration for Testing ---
    # file_to_test = "path/to/your/test_book.pdf" # <--- CHANGE THIS
    file_to_test = "path/to/your/test_book.epub" # <--- OR THIS
    output_base = "extracted_test_output"
    book_base_name = os.path.splitext(os.path.basename(file_to_test))[0]
    specific_output_dir = os.path.join(output_base, book_base_name) # Simulate UI providing the final dir
    mode = "chapters" # "chapters" or "whole"
    use_pdf_toc = True # True or False
    # --------------------------------

    if not os.path.exists(file_to_test):
         print(f"Test file not found: {file_to_test}")
         sys.exit(1)

    def sample_progress(p):
         if p is None:
              print("Progress: Error!")
         else:
              # Simple progress bar
              bar_len = 40
              filled_len = int(round(bar_len * p / 100))
              bar = '=' * filled_len + '-' * (bar_len - filled_len)
              print(f'Progress: [{bar}] {p:.0f}%', end='\r')
              if p == 100:
                  print() # Newline at the end

    try:
        print(f"Running extraction on: {file_to_test}")
        print(f"Output will be in:   {specific_output_dir}")
        print(f"Mode: {mode}, Use TOC: {use_pdf_toc if file_to_test.endswith('.pdf') else 'N/A'}")

        result_dir = extract_book(
            file_path=file_to_test,
            use_toc=use_pdf_toc,
            extract_mode=mode,
            output_dir=specific_output_dir, # Pass the final target directory
            progress_callback=sample_progress
        )
        print(f"\nExtraction successful. Output saved in: {result_dir}")
    except Exception as e:
        print(f"\nExtraction failed: {e}")
        sys.exit(1)
'''