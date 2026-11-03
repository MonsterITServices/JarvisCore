import csv
import io
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from threading import Lock

from docx import Document
from openpyxl import load_workbook
from PIL import Image, ImageOps
from pptx import Presentation
from pypdf import PdfReader
import pypdfium2
import rapidocr
from rapidocr import RapidOCR

from integrations.knowledge_store import KnowledgeStoreError


MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_EXTRACTED_CHARS = 500_000
MAX_PDF_PAGES = 50
MAX_IMAGE_PIXELS = 30_000_000
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_ARCHIVE_FILES = 2000
_OCR_MODEL_FILES = (
    "PP-OCRv6_det_small.onnx",
    "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
    "PP-OCRv6_rec_small.onnx",
)
_OCR_LOCK = Lock()
_OCR_ENGINE = None
SUPPORTED_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".lua", ".luau", ".html", ".htm", ".csv", ".pdf", ".docx", ".pptx", ".xlsx",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp",
}


class DocumentIngestError(Exception):
    pass


class _HtmlText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg", "iframe", "template"}:
            self.skip_depth += 1
        elif tag in {"p", "br", "div", "li", "h1", "h2", "h3", "tr"} and self.parts:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg", "iframe", "template"} and self.skip_depth:
            self.skip_depth -= 1
        elif tag in {"p", "div", "li", "h1", "h2", "h3", "tr"} and self.parts:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip_depth and data.strip():
            self.parts.append(data.strip())


def ocr_status():
    model_directory = Path(rapidocr.__file__).resolve().parent / "models"
    missing = [name for name in _OCR_MODEL_FILES if not (model_directory / name).is_file()]
    return {
        "available": not missing,
        "engine": "RapidOCR ONNX Runtime CPU",
        "missing_models": missing,
    }


def _ocr_engine():
    global _OCR_ENGINE
    if not ocr_status()["available"]:
        raise DocumentIngestError("The bundled local OCR models are not installed. Reinstall RapidOCR from requirements.txt.")
    if _OCR_ENGINE is None:
        with _OCR_LOCK:
            if _OCR_ENGINE is None:
                _OCR_ENGINE = RapidOCR()
    return _OCR_ENGINE


def _ocr_image(image):
    if image.width * image.height > MAX_IMAGE_PIXELS:
        raise DocumentIngestError("The image is too large to process safely.")
    try:
        result = _ocr_engine()(image)
        return "\n".join(result.txts or ()).strip()
    except DocumentIngestError:
        raise
    except Exception as error:
        raise DocumentIngestError("OCR timed out while reading the image.") from error


def _read_pdf(data):
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
    except Exception as error:
        raise DocumentIngestError("This PDF could not be opened.") from error
    if len(reader.pages) > MAX_PDF_PAGES:
        raise DocumentIngestError(f"PDFs are limited to {MAX_PDF_PAGES} pages per source.")
    title = str(reader.metadata.title or "").strip() if reader.metadata else ""
    parts = []
    rendered = None
    try:
        for index, page in enumerate(reader.pages):
            try:
                page_text = page.extract_text() or ""
            except Exception:
                page_text = ""
            if len(page_text.strip()) < 20:
                if not ocr_status()["available"]:
                    if not page_text.strip():
                        raise DocumentIngestError(
                            "This PDF appears scanned, but the bundled local OCR models are missing. Reinstall RapidOCR."
                        )
                else:
                    if rendered is None:
                        rendered = pypdfium2.PdfDocument(data)
                    bitmap = rendered[index].render(scale=1.5)
                    image = bitmap.to_pil()
                    try:
                        page_text = _ocr_image(image) or page_text
                    finally:
                        image.close()
                        bitmap.close()
            if page_text.strip():
                parts.append(page_text.strip())
            if sum(map(len, parts)) >= MAX_EXTRACTED_CHARS:
                break
    except DocumentIngestError:
        raise
    except Exception as error:
        raise DocumentIngestError("The PDF text or page image could not be read.") from error
    finally:
        if rendered is not None:
            rendered.close()
    return title, "\n\n".join(parts)


def _check_office_archive(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ARCHIVE_FILES or sum(entry.file_size for entry in entries) > MAX_ARCHIVE_BYTES:
                raise DocumentIngestError("Office documents have a 50 MB expanded-content limit.")
    except DocumentIngestError:
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise DocumentIngestError("This Office document is not a valid DOCX, PPTX, or XLSX file.") from error


def _read_docx(data):
    try:
        document = Document(io.BytesIO(data))
        parts = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
        for table in document.tables:
            for row in table.rows:
                values = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if values:
                    parts.append(" | ".join(values))
        return "", "\n".join(parts)
    except Exception as error:
        raise DocumentIngestError("This Word document could not be opened.") from error


def _read_pptx(data):
    try:
        presentation = Presentation(io.BytesIO(data))
        parts = []
        for slide_number, slide in enumerate(presentation.slides, start=1):
            parts.append(f"Slide {slide_number}")
            for shape in slide.shapes:
                if getattr(shape, "has_text_frame", False) and shape.text.strip():
                    parts.append(shape.text.strip())
                if getattr(shape, "has_table", False):
                    for row in shape.table.rows:
                        parts.append(" | ".join(cell.text.strip() for cell in row.cells))
                if sum(map(len, parts)) >= MAX_EXTRACTED_CHARS:
                    break
        return "", "\n".join(parts)
    except Exception as error:
        raise DocumentIngestError("This PowerPoint presentation could not be opened.") from error


def _read_xlsx(data):
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        parts = []
        try:
            for sheet in workbook.worksheets:
                parts.append(f"Sheet: {sheet.title}")
                for row in sheet.iter_rows(max_row=2000, max_col=100, values_only=True):
                    values = [str(value).strip() for value in row if value is not None and str(value).strip()]
                    if values:
                        parts.append(" | ".join(values))
                    if sum(map(len, parts)) >= MAX_EXTRACTED_CHARS:
                        break
                if sum(map(len, parts)) >= MAX_EXTRACTED_CHARS:
                    break
        finally:
            workbook.close()
        return "", "\n".join(parts)
    except Exception as error:
        raise DocumentIngestError("This spreadsheet could not be opened.") from error


def _read_image(data):
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise DocumentIngestError("The image is too large to process safely.")
            normalized = ImageOps.exif_transpose(image).convert("RGB")
            try:
                return _ocr_image(normalized)
            finally:
                normalized.close()
    except DocumentIngestError:
        raise
    except Exception as error:
        raise DocumentIngestError("This image could not be opened.") from error


def extract_document(filename, data):
    if not isinstance(filename, str) or not filename.strip():
        raise DocumentIngestError("A filename is required.")
    safe_name = Path(filename.replace("\\", "/")).name
    extension = Path(safe_name).suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise DocumentIngestError(f"Unsupported file type. Supported types: {supported}")
    if not isinstance(data, bytes) or not data:
        raise DocumentIngestError("The uploaded file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise DocumentIngestError("Files are limited to 25 MB.")

    if extension in {".txt", ".md", ".markdown", ".lua", ".luau"}:
        text = data.decode("utf-8-sig", errors="replace")
        title = ""
    elif extension in {".html", ".htm"}:
        parser = _HtmlText()
        parser.feed(data.decode("utf-8-sig", errors="replace"))
        text = "\n".join(parser.parts)
        title = ""
    elif extension == ".csv":
        try:
            rows = csv.reader(io.StringIO(data.decode("utf-8-sig", errors="replace")))
            text = "\n".join(" | ".join(value.strip() for value in row) for row in rows)
        except csv.Error as error:
            raise DocumentIngestError("This CSV file could not be read.") from error
        title = ""
    elif extension == ".pdf":
        title, text = _read_pdf(data)
    elif extension == ".docx":
        _check_office_archive(data)
        title, text = _read_docx(data)
    elif extension == ".pptx":
        _check_office_archive(data)
        title, text = _read_pptx(data)
    elif extension == ".xlsx":
        _check_office_archive(data)
        title, text = _read_xlsx(data)
    else:
        text = _read_image(data)
        title = ""

    text = re.sub(r"\x00", " ", text).strip()[:MAX_EXTRACTED_CHARS]
    if not text:
        raise DocumentIngestError("No readable text was found. Local OCR was attempted; the image may be blank or its text may be unreadable.")
    return {
        "title": (title or Path(safe_name).stem)[:300],
        "filename": safe_name,
        "document_type": extension.lstrip("."),
        "text": text,
    }
