"""
brain/attachments.py - Local Document, Code, PDF & Screenshot Attachment Extractor.

Extracts text locally from code files, logs, Markdown, JSON, CSV, PDFs (pypdf),
and screenshots/images (Pillow + pytesseract / Windows WinRT OCR fallback)
into structured markdown context blocks for LLM prompt injection.

ARCHITECTURAL CONSTRAINTS:
1. All attachment/image/OCR extraction logic lives in this module (brain/attachments.py).
2. NEVER create tools/ocr.py, tools/screen.py, or tools/screenshot.py.
3. NEVER add SCREENSHOT or OCR to Intent enum.
4. Preserves Orange API's text-only AssistRequest contract; never passes images to assist().
"""

import os
import io
import re
import base64
import logging
import tempfile
import subprocess
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger("brain.attachments")

MAX_EXTRACTION_CHARS = 16_000

# Supported code/text extensions and markdown language specifiers
EXTENSION_LANG_MAP: Dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "jsx",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".rs": "rust",
    ".toml": "toml",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".md": "markdown",
    ".txt": "text",
    ".log": "text",
    ".csv": "csv",
    ".html": "html",
    ".css": "css",
    ".sql": "sql",
    ".ps1": "powershell",
    ".sh": "bash",
    ".bat": "batch",
    ".cmd": "batch",
    ".xml": "xml",
    ".ini": "ini",
    ".cfg": "ini",
    ".conf": "text",
    ".env": "text",
}

TEXT_DECODE_ENCODINGS = ("utf-8", "utf-8-sig", "cp1252", "latin-1")


def _decode_raw_bytes(attachment: Dict[str, Any]) -> Tuple[Optional[bytes], Optional[str]]:
    """Extract raw bytes from base64_data, content, or path."""
    b64 = attachment.get("base64_data")
    if b64:
        try:
            # Strip data URL prefix if present (e.g. data:image/png;base64,...)
            clean_b64 = re.sub(r"^data:[^;]+;base64,", "", str(b64).strip())
            return base64.b64decode(clean_b64), None
        except Exception as e:
            return None, f"Failed decoding base64 data: {e}"

    path = attachment.get("path")
    if path and os.path.exists(path):
        try:
            with open(path, "rb") as f:
                return f.read(), None
        except Exception as e:
            return None, f"Failed reading file at {path}: {e}"

    content = attachment.get("content")
    if content is not None:
        if isinstance(content, str):
            return content.encode("utf-8"), None
        elif isinstance(content, (bytes, bytearray)):
            return bytes(content), None

    return None, "No data provided in attachment (missing base64_data, path, or content)."


def _extract_text_document(name: str, raw_bytes: bytes, ext: str) -> Dict[str, Any]:
    """Extract and format text, code, logs, and structured data files."""
    lang = EXTENSION_LANG_MAP.get(ext, "text")

    decoded_text = None
    for enc in TEXT_DECODE_ENCODINGS:
        try:
            decoded_text = raw_bytes.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue

    if decoded_text is None:
        decoded_text = raw_bytes.decode("utf-8", errors="replace")

    orig_len = len(decoded_text)
    truncated = False
    if orig_len > MAX_EXTRACTION_CHARS:
        omitted = orig_len - MAX_EXTRACTION_CHARS
        decoded_text = decoded_text[:MAX_EXTRACTION_CHARS] + f"\n\n[Truncated: {omitted} chars omitted]"
        truncated = True

    formatted_block = f"### File: {name}\n```{lang}\n{decoded_text}\n```"

    return {
        "name": name,
        "kind": "file",
        "mime_type": "text/plain",
        "char_count": len(decoded_text),
        "extracted_text": decoded_text,
        "preview_meta": f"{len(decoded_text.splitlines())} lines",
        "truncated": truncated,
        "formatted_block": formatted_block,
        "error": None,
    }


def _extract_pdf_document(name: str, raw_bytes: bytes) -> Dict[str, Any]:
    """Extract and format text from a PDF document using pypdf."""
    try:
        import pypdf
    except ImportError:
        err_msg = "[PDF extraction unavailable: pypdf library is not installed]"
        return {
            "name": name,
            "kind": "pdf",
            "mime_type": "application/pdf",
            "char_count": len(err_msg),
            "extracted_text": err_msg,
            "preview_meta": "0 pages",
            "truncated": False,
            "formatted_block": f"### PDF Document: {name}\n{err_msg}",
            "error": "pypdf not installed",
        }

    try:
        stream = io.BytesIO(raw_bytes)
        reader = pypdf.PdfReader(stream)

        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                pass
            if reader.is_encrypted:
                note = "[Encrypted PDF: Password required for text extraction]"
                return {
                    "name": name,
                    "kind": "pdf",
                    "mime_type": "application/pdf",
                    "char_count": len(note),
                    "extracted_text": note,
                    "preview_meta": "Encrypted",
                    "truncated": False,
                    "formatted_block": f"### PDF Document: {name}\n{note}",
                    "error": "PDF is password-encrypted",
                }

        page_count = len(reader.pages)
        pages_content: List[str] = []
        total_chars = 0

        for idx, page in enumerate(reader.pages):
            try:
                p_text = page.extract_text() or ""
            except Exception as e:
                p_text = f"[Error extracting page {idx + 1}: {e}]"

            cleaned = p_text.strip()
            if cleaned:
                pages_content.append(f"[Page {idx + 1}]\n{cleaned}")
                total_chars += len(cleaned)

        if not pages_content:
            note = f"[PDF contains 0 extractable text characters; document may be scanned or image-only (Total pages: {page_count})]"
            return {
                "name": name,
                "kind": "pdf",
                "mime_type": "application/pdf",
                "char_count": len(note),
                "extracted_text": note,
                "preview_meta": f"{page_count} pages",
                "truncated": False,
                "formatted_block": f"### PDF Document: {name} (Pages: {page_count})\n{note}",
                "error": None,
            }

        full_pdf_text = "\n\n".join(pages_content)
        truncated = False
        if len(full_pdf_text) > MAX_EXTRACTION_CHARS:
            omitted = len(full_pdf_text) - MAX_EXTRACTION_CHARS
            full_pdf_text = full_pdf_text[:MAX_EXTRACTION_CHARS] + f"\n\n[Truncated: {omitted} chars omitted]"
            truncated = True

        formatted_block = f"### PDF Document: {name} (Pages: {page_count})\n{full_pdf_text}"

        return {
            "name": name,
            "kind": "pdf",
            "mime_type": "application/pdf",
            "char_count": len(full_pdf_text),
            "extracted_text": full_pdf_text,
            "preview_meta": f"{page_count} pages",
            "truncated": truncated,
            "formatted_block": formatted_block,
            "error": None,
        }

    except Exception as e:
        logger.warning("PDF extraction error for %s: %s", name, e)
        note = f"[Error parsing PDF: {e}]"
        return {
            "name": name,
            "kind": "pdf",
            "mime_type": "application/pdf",
            "char_count": len(note),
            "extracted_text": note,
            "preview_meta": "Error",
            "truncated": False,
            "formatted_block": f"### PDF Document: {name}\n{note}",
            "error": str(e),
        }


def _run_winrt_ocr_fallback(img) -> str:
    """Fallback OCR using Windows native WinRT OcrEngine via PowerShell."""
    temp_img_path = None
    temp_ps_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            temp_img_path = f.name
            img.save(temp_img_path, format="PNG")

        # Escaped PowerShell script that loads Windows.Media.Ocr and reads the image file
        ps_script = """param([string]$ImagePath)
[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null

try {
    $file = [Windows.Storage.StorageFile]::GetFileFromPathAsync($ImagePath).GetAwaiter().GetResult()
    $stream = $file.OpenAsync([Windows.Storage.FileAccessMode]::Read).GetAwaiter().GetResult()
    $decoder = [Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream).GetAwaiter().GetResult()
    $bitmap = $decoder.GetSoftwareBitmapAsync().GetAwaiter().GetResult()
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    if (-not $engine) {
        $lang = [Windows.Globalization.Language]::new('en-US')
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
    }
    if ($engine) {
        $result = $engine.RecognizeAsync($bitmap).GetAwaiter().GetResult()
        Write-Output $result.Text
    }
} catch {
}
"""
        with tempfile.NamedTemporaryFile(suffix=".ps1", mode="w", encoding="utf-8", delete=False) as ps_f:
            temp_ps_path = ps_f.name
            ps_f.write(ps_script)

        res = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", temp_ps_path, temp_img_path],
            capture_output=True,
            text=True,
            timeout=8,
        )
        return res.stdout.strip()
    except Exception as e:
        logger.debug("WinRT OCR fallback error: %s", e)
        return ""
    finally:
        if temp_img_path and os.path.exists(temp_img_path):
            try:
                os.unlink(temp_img_path)
            except Exception:
                pass
        if temp_ps_path and os.path.exists(temp_ps_path):
            try:
                os.unlink(temp_ps_path)
            except Exception:
                pass


def _extract_image_attachment(name: str, raw_bytes: bytes, kind: str) -> Dict[str, Any]:
    """Extract metadata and OCR text from an image or screenshot."""
    from PIL import Image

    try:
        stream = io.BytesIO(raw_bytes)
        img = Image.open(stream)
        width, height = img.size
        img_format = img.format or "image"
        mode = img.mode
        preview_meta = f"{width}x{height}"
    except Exception as e:
        note = f"[Error opening image {name}: {e}]"
        return {
            "name": name,
            "kind": kind or "image",
            "mime_type": "image/png",
            "char_count": len(note),
            "extracted_text": note,
            "preview_meta": "Corrupt",
            "truncated": False,
            "formatted_block": f"### Image: {name}\n{note}",
            "error": str(e),
        }

    # 1. Primary OCR: pytesseract
    ocr_text = ""
    try:
        import pytesseract
        ocr_text = pytesseract.image_to_string(img).strip()
    except Exception as e:
        logger.debug("pytesseract extraction error: %s", e)
        ocr_text = ""

    # 2. Secondary OCR: WinRT OcrEngine fallback
    if not ocr_text and os.name == "nt":
        ocr_text = _run_winrt_ocr_fallback(img)

    # 3. Text formatting and diagnostic summary
    truncated = False
    if ocr_text:
        if len(ocr_text) > MAX_EXTRACTION_CHARS:
            omitted = len(ocr_text) - MAX_EXTRACTION_CHARS
            ocr_text = ocr_text[:MAX_EXTRACTION_CHARS] + f"\n\n[Truncated: {omitted} chars omitted]"
            truncated = True
        extracted_content = f"[OCR Text Extracted ({width}x{height})]:\n{ocr_text}"
    else:
        extracted_content = (
            f"[Image Metadata: {width}x{height} {img_format}, Mode: {mode}. "
            "No visible text detected or OCR unavailable.]"
        )

    title_label = "Screenshot" if kind == "screenshot" or "screenshot" in name.lower() else "Image"
    formatted_block = f"### {title_label}: {name} ({width}x{height})\n{extracted_content}"

    return {
        "name": name,
        "kind": kind or ("screenshot" if "screenshot" in name.lower() else "image"),
        "mime_type": f"image/{(img_format or 'png').lower()}",
        "char_count": len(extracted_content),
        "extracted_text": extracted_content,
        "preview_meta": preview_meta,
        "truncated": truncated,
        "formatted_block": formatted_block,
        "error": None,
    }


def extract_attachment_context(attachment: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract text locally from code files, logs, Markdown, JSON, CSV, PDFs,
    and screenshots/images into structured context blocks.

    Guarantees: Never raises unhandled exceptions.
    """
    if not isinstance(attachment, dict):
        return {
            "name": "unknown",
            "kind": "unknown",
            "mime_type": "text/plain",
            "char_count": 0,
            "extracted_text": "[Invalid attachment format]",
            "preview_meta": "0 B",
            "truncated": False,
            "formatted_block": "### File: unknown\n[Invalid attachment format]",
            "error": "Attachment must be a dict",
        }

    name = str(attachment.get("name") or "attachment").strip()
    kind = str(attachment.get("kind") or "").strip().lower()
    mime = str(attachment.get("mime_type") or "").strip().lower()

    # If already extracted and has formatted_block, return directly
    if attachment.get("formatted_block") and attachment.get("extracted_text") is not None:
        return attachment

    # Retrieve raw bytes
    raw_bytes, err = _decode_raw_bytes(attachment)
    if err or raw_bytes is None:
        note = f"[Attachment {name} error: {err or 'Empty file'}]"
        return {
            "name": name,
            "kind": kind or "file",
            "mime_type": mime or "application/octet-stream",
            "char_count": len(note),
            "extracted_text": note,
            "preview_meta": "0 B",
            "truncated": False,
            "formatted_block": f"### File: {name}\n{note}",
            "error": err,
        }

    _, ext = os.path.splitext(name.lower())

    # Branch 1: PDF Document
    if ext == ".pdf" or mime == "application/pdf" or kind == "pdf":
        return _extract_pdf_document(name, raw_bytes)

    # Branch 2: Image / Screenshot
    image_exts = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tiff", ".ico"}
    if ext in image_exts or mime.startswith("image/") or kind in ("screenshot", "image"):
        return _extract_image_attachment(name, raw_bytes, kind)

    # Branch 3: Text / Code / Log / Data
    return _extract_text_document(name, raw_bytes, ext)


def capture_screen_attachment() -> Dict[str, Any]:
    """
    Capture the primary display using Pillow, resize if wider than 1920px,
    run OCR extraction immediately, and return structured attachment dict.

    Gracefully handles headless environments / environments without interactive desktop.
    """
    from PIL import Image

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"screenshot-{ts}.png"

    img = None
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab()
    except Exception as e:
        logger.warning("ImageGrab.grab failed (likely headless/service environment): %s", e)
        # Create a graceful diagnostic image
        img = Image.new("RGB", (1280, 720), color=(30, 32, 40))

    # Resize if wider than 1920px for high-speed local processing
    if img.width > 1920:
        target_width = 1920
        target_height = max(1, int(img.height * (1920.0 / img.width)))
        try:
            resample = Image.Resampling.LANCZOS
        except AttributeError:
            resample = Image.LANCZOS
        img = img.resize((target_width, target_height), resample)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    raw_bytes = buf.getvalue()
    b64_str = base64.b64encode(raw_bytes).decode("ascii")
    data_uri = f"data:image/png;base64,{b64_str}"

    extracted = extract_attachment_context({
        "name": filename,
        "kind": "screenshot",
        "mime_type": "image/png",
        "base64_data": b64_str,
    })

    extracted["base64_data"] = data_uri
    return extracted


def format_attachments_for_prompt(attachments: Optional[List[Dict[str, Any]]]) -> str:
    """
    Combine extracted attachment blocks into a clean markdown section
    ready for LLM prompt context injection.
    """
    if not attachments or not isinstance(attachments, list):
        return ""

    blocks: List[str] = []
    for att in attachments:
        if not isinstance(att, dict):
            continue
        # Extract if not already extracted
        if not att.get("formatted_block"):
            att_extracted = extract_attachment_context(att)
            block = att_extracted.get("formatted_block")
        else:
            block = att.get("formatted_block")

        if block and block.strip():
            blocks.append(block.strip())

    if not blocks:
        return ""

    joined_blocks = "\n\n".join(blocks)
    return (
        "=== ATTACHED FILES & SCREENSHOTS ===\n"
        f"{joined_blocks}\n"
        "===================================="
    )
