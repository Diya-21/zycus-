"""src/ingestion.py — PDF ingestion using PyMuPDF (fitz).

Responsibilities:
- Load a PDF from disk.
- Render each page at a suitable DPI for multimodal LLM input.
- Detect and skip genuinely blank pages (all-white or near-white).
- Return structured page data for the extraction layer.
- Handle corrupted/unreadable PDFs gracefully.

Does NOT classify documents — that is the extractor's job.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# DPI for rendering — 150 dpi gives good quality for LLM vision at reasonable size.
_DEFAULT_DPI = 150
# Blank-page detection threshold: fraction of pixels that must be non-white.
_BLANK_THRESHOLD = 0.005  # 0.5% of pixels must differ from white


@dataclass
class PageData:
    """One rendered page from a PDF."""
    page_number: int          # 0-indexed
    image_bytes: bytes        # PNG bytes
    width_px: int
    height_px: int
    is_blank: bool


@dataclass
class IngestedDocument:
    """Result of loading and rendering a PDF."""
    filename: str             # e.g. "INV-01.pdf"
    path: Path
    pages: List[PageData] = field(default_factory=list)
    total_pages: int = 0
    blank_pages: int = 0
    error: Optional[str] = None  # set if the PDF failed to open

    @property
    def non_blank_pages(self) -> List[PageData]:
        return [p for p in self.pages if not p.is_blank]

    @property
    def is_usable(self) -> bool:
        return self.error is None and len(self.non_blank_pages) > 0


def _is_blank(image_bytes: bytes, threshold: float = _BLANK_THRESHOLD) -> bool:
    """Return True if the image is essentially all-white (blank page).

    Uses raw pixel data — avoids PIL dependency by checking PNG data directly
    via PyMuPDF's pixmap API. We check non-white pixel fraction.
    """
    try:
        import fitz  # PyMuPDF
        # Re-open the image as a fitz Pixmap to inspect pixels
        pix = fitz.Pixmap(fitz.open("pdf", b""), 0)  # dummy; we work with bytes below
    except Exception:
        pass

    # Fallback: use PIL if available, otherwise skip blank detection
    try:
        from PIL import Image
        import numpy as np
        img = Image.open(io.BytesIO(image_bytes)).convert("L")  # greyscale
        arr = np.array(img, dtype=np.uint8)
        non_white = float((arr < 250).sum()) / arr.size
        return non_white < threshold
    except ImportError:
        # No PIL/numpy — skip blank detection (page is considered non-blank)
        return False
    except Exception as exc:
        logger.debug("Blank detection failed: %s", exc)
        return False


def _is_blank_from_pixmap(pix) -> bool:
    """Detect blank pages directly from a PyMuPDF Pixmap (faster, no PIL)."""
    try:
        samples = pix.samples  # raw bytes: R G B or R G B A per pixel
        n = pix.n              # number of components per pixel
        total_pixels = pix.width * pix.height
        if total_pixels == 0:
            return True
        # Sample pixels rather than scanning all for performance
        # Decide on a sample_count cap to keep this fast on large pages
        sample_count = min(20000, total_pixels)
        step = max(1, total_pixels // sample_count)
        mv = memoryview(samples)
        non_white = 0
        checked = 0
        # samples are packed per pixel as n bytes
        for pix_idx in range(0, total_pixels, step):
            base = pix_idx * n
            chunk = mv[base: base + n]
            # check RGB channels
            if any(b < 250 for b in chunk[:3]):
                non_white += 1
            checked += 1
        fraction = non_white / max(1, checked)
        return fraction < _BLANK_THRESHOLD
    except Exception as exc:
        logger.debug("Pixmap blank detection failed: %s", exc)
        return False


def load_pdf(pdf_path: Path, dpi: int = _DEFAULT_DPI) -> IngestedDocument:
    """Load a PDF and render all pages to PNG bytes.

    Args:
        pdf_path: Path to the PDF file.
        dpi: Rendering resolution (default 150 dpi).

    Returns:
        IngestedDocument with pages rendered as PNG bytes.
        On failure, returns an IngestedDocument with error set.
    """
    filename = pdf_path.name
    doc = IngestedDocument(filename=filename, path=pdf_path)

    try:
        import fitz
    except ImportError:
        doc.error = "PyMuPDF (fitz) is not installed. Run: pip install pymupdf"
        logger.error(doc.error)
        return doc

    try:
        pdf = fitz.open(str(pdf_path))
    except Exception as exc:
        doc.error = f"Failed to open PDF: {exc}"
        logger.error("Cannot open %s: %s", pdf_path, exc)
        return doc

    doc.total_pages = len(pdf)
    zoom = dpi / 72.0  # fitz uses 72 dpi as base
    mat = fitz.Matrix(zoom, zoom)

    for page_num in range(len(pdf)):
        try:
            page = pdf.load_page(page_num)
            pix = page.get_pixmap(matrix=mat, alpha=False)

            # Blank detection using pixmap (fast, no PIL needed)
            blank = _is_blank_from_pixmap(pix)

            # Convert to PNG bytes
            png_bytes = pix.tobytes("png")

            pd = PageData(
                page_number=page_num,
                image_bytes=png_bytes,
                width_px=pix.width,
                height_px=pix.height,
                is_blank=blank,
            )
            doc.pages.append(pd)

            if blank:
                doc.blank_pages += 1
                logger.debug("%s page %d: blank (skipped)", filename, page_num)
            else:
                logger.debug("%s page %d: %dx%d", filename, page_num, pix.width, pix.height)

        except Exception as exc:
            logger.warning("Failed to render page %d of %s: %s", page_num, filename, exc)
            # Include a placeholder with error info but mark as blank so it won't be extracted
            pd = PageData(
                page_number=page_num,
                image_bytes=b"",
                width_px=0,
                height_px=0,
                is_blank=True,
            )
            doc.pages.append(pd)

    try:
        pdf.close()
    except Exception:
        pass

    non_blank = len(doc.non_blank_pages)
    logger.info(
        "Loaded %s: %d pages total, %d blank, %d usable",
        filename,
        doc.total_pages,
        doc.blank_pages,
        non_blank,
    )
    return doc
