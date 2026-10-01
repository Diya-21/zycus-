"""src/extractor.py — Lightweight deterministic extractor using PyMuPDF text.

This extractor performs deterministic, evidence-backed extraction using the
page text produced by PyMuPDF. It prefers not to invent values: when a
field cannot be reliably found it returns empty strings or nulls as the
schema permits.

This implementation is intentionally conservative and does NOT call any
external LLM by default. It is designed to work offline for the supplied
candidate documents and to be easy to replace with a Gemini multimodal
call if required.
"""
from __future__ import annotations

import re
import logging
import time
import hashlib
from typing import List, Dict, Any
from pathlib import Path

from src.ingestion import IngestedDocument, PageData

logger = logging.getLogger(__name__)
import os
import json
from dotenv import load_dotenv

# Load .env (if present) so GEMINI_API_KEY can be stored there
load_dotenv()

# Gemini usage statistics
GEMINI_STATS = {"calls": 0, "success": 0, "failures": 0}


def _cache_enabled() -> bool:
    value = os.environ.get("GEMINI_CACHE_ENABLED", "true")
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def _document_cache_key(doc: IngestedDocument) -> str:
    # Use a stable content-based SHA-256 of the file bytes so the same PDF
    # content produces the same cache key regardless of filesystem path.
    try:
        p = Path(doc.path)
        if p.exists():
            data = p.read_bytes()
            return hashlib.sha256(data).hexdigest()
    except Exception:
        # Fall back to a filename-based key if file read fails (rare, testable)
        raw = f"{str(doc.path)}|{doc.filename}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _document_cache_dir() -> Path | None:
    if not _cache_enabled():
        return None
    cache_dir = os.environ.get("GEMINI_CACHE_DIR", str(Path.cwd() / ".gemini_cache"))
    cache_path = Path(cache_dir)
    cache_path.mkdir(parents=True, exist_ok=True)
    return cache_path


def _document_cache_path(doc: IngestedDocument) -> Path | None:
    cache_dir = _document_cache_dir()
    if cache_dir is None:
        return None
    return cache_dir / f"{_document_cache_key(doc)}.json"


def _load_cached_successful_extraction(doc: IngestedDocument) -> Dict[str, Any] | None:
    cache_path = _document_cache_path(doc)
    if cache_path is None or not cache_path.exists():
        return None
    try:
        with open(cache_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and data.get("status") == "success":
            return data.get("payload")
    except Exception:
        return None
    return None


def _store_successful_extraction(doc: IngestedDocument, payload: Dict[str, Any]) -> None:
    cache_path = _document_cache_path(doc)
    if cache_path is None:
        return
    try:
        cache_data = {"status": "success", "payload": payload}
        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump(cache_data, fh, ensure_ascii=False, sort_keys=True)
    except Exception:
        logger.debug("Failed to persist Gemini cache entry for %s", doc.path)


def _normalize_gemini_failure(exc: Any, model_name: str = "gemini-2.5-flash") -> str:
    """Normalize provider-side Gemini errors to stable, human-readable messages.

    The exact quota text is intentionally stable so downstream reporting can keep a
    clear per-document reason without inventing or hiding the real issue.
    """
    text = ""
    try:
        text = str(exc)
    except Exception:
        text = "Gemini API failure"

    status_value = None
    for attr in ("status_code", "http_status", "code", "status"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            status_value = val
            break
        if isinstance(val, str) and val.isdigit():
            status_value = int(val)
            break

    lower = text.lower()
    if status_value == 429 or "resource_exhausted" in lower or "rate limit" in lower:
        return f"Gemini quota exhausted for {model_name}"
    if status_value == 503 or "unavailable" in lower or "high demand" in lower or "try again later" in lower:
        return f"Gemini service unavailable for {model_name}; provider is temporarily overloaded"
    if "quota" in lower:
        return f"Gemini quota exhausted for {model_name}"
    if "unavailable" in lower or "overloaded" in lower or "try again later" in lower:
        return f"Gemini service unavailable for {model_name}; provider is temporarily overloaded"
    return f"Gemini extraction failed for {model_name}"


def _normalize_gemini_payload(payload: Any) -> Any:
    """Normalize Gemini output to the existing assignment schema expectations.

    Numeric values are converted to dot-decimal strings without rounding or
    inventing missing data. Standalone amounts are only mapped to quantity=1 and
    unit_price=amount when the document clearly supports that interpretation, and
    tax fields are preserved in the ERP-compatible shape (tax_rate / tax_amount).
    """
    str_fields = {
        "invoice_number",
        "invoice_date",
        "due_date",
        "invoice_type",
        "currency",
        "payment_term_text",
        "po_number",
        "gross_total",
        "subtotal",
        "total_tax_amount",
        "discount_amount",
        "freight_charges",
        "insurance_charges",
        "extra_charges",
        "excise_duties",
        "tax_name",
        "tax_type",
        "tax_rate",
        "tax_amount",
        "tax_type_code",
        "description",
        "item_type",
        "uom",
        "quantity",
        "unit_price",
        "total",
        "discount",
        "discount_percentage",
        "supplier_id",
        "address",
        "vat_id",
        "name",
        "company_code",
        "business_unit_code",
        "location_code",
        "doc_type",
        "reason",
        "file",
    }

    def to_decimal_string(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            s = value.strip()
            if s == "":
                return ""
            # Parentheses denote negative numbers in accounting formats
            negative = False
            if s.startswith("(") and s.endswith(")"):
                negative = True
                s = s[1:-1].strip()

            # Remove common currency prefixes (e.g. "$", "€", "USD ")
            s = re.sub(r"^[\$€£¥₹]\s*", "", s)
            s = re.sub(r"^[A-Z]{3}\s+", "", s)

            # Remove grouping commas
            s = s.replace(",", "")

            # Remove trailing percent if present
            if s.endswith("%"):
                s = s[:-1]

            s = s.strip()
            if negative and s:
                s = f"-{s}"
            return s
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        return str(value)

    def normalize(value: Any) -> Any:
        if isinstance(value, dict):
            for key, item in list(value.items()):
                if key in str_fields:
                    if item is None:
                        value[key] = ""
                    elif isinstance(item, (int, float)) and not isinstance(item, bool):
                        value[key] = str(item)
                    elif isinstance(item, str):
                        if key in {"quantity", "unit_price", "gross_total", "subtotal", "total_tax_amount", "discount_amount", "freight_charges", "insurance_charges", "extra_charges", "excise_duties", "discount", "discount_percentage", "tax_rate", "tax_amount", "total"}:
                            value[key] = to_decimal_string(item)
                        else:
                            value[key] = item
                    else:
                        value[key] = normalize(item)
                else:
                    value[key] = normalize(item)

            tax_like = any(key in value for key in ("tax_rate", "tax_name", "tax_type", "tax_type_code"))
            if "name" in value and "amount" in value and not tax_like:
                name = str(value.get("name", "")).lower()
                if "tax" in name:
                    tax_like = True

            amount = None
            if "amount" in value and value.get("quantity") in (None, "") and value.get("unit_price") in (None, ""):
                if tax_like:
                    value["tax_amount"] = to_decimal_string(value.get("amount"))
                    value.setdefault("tax_rate", "")
                else:
                    amount = value.get("amount")

            if amount is not None:
                amt_str = to_decimal_string(amount)
                if amt_str not in ("", "0", "0.0", "0.00"):
                    value["quantity"] = "1"
                    value["unit_price"] = amt_str
                    if "total" not in value:
                        value["total"] = amt_str

            if "amount" in value and "tax_amount" not in value and "quantity" not in value and "unit_price" not in value:
                if tax_like:
                    value["tax_amount"] = to_decimal_string(value.get("amount"))
                    value.setdefault("tax_rate", "")

            if "tax_amount" in value and "tax_rate" in value:
                value["tax_amount"] = to_decimal_string(value.get("tax_amount"))
                value["tax_rate"] = to_decimal_string(value.get("tax_rate"))
            elif "tax_amount" in value:
                value["tax_amount"] = to_decimal_string(value.get("tax_amount"))
                value.setdefault("tax_rate", "")

            if "taxes" in value and isinstance(value["taxes"], list):
                value["taxes"] = [normalize(item) for item in value["taxes"]]
            if "line_items" in value and isinstance(value["line_items"], list):
                value["line_items"] = [normalize(item) for item in value["line_items"]]
            return value

        if isinstance(value, list):
            return [normalize(item) for item in value]

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)

        return value

    return normalize(payload)


def _invoke_gemini_api(client: Any, model_name: str, contents: List[Any]) -> Any:
    return client.models.generate_content(model=model_name, contents=contents)


def _extract_with_gemini(doc: "IngestedDocument") -> tuple[dict | None, dict]:
    """Opt-in Gemini multimodal extraction.

    Returns a tuple `(payload, meta)` where `payload` is the extracted
    JSON-like dict when successful, or `None` on failure. `meta` is a
    dict with diagnostic flags and error messages; see logs for details.
    """
    meta = {
        "configured": False,
        "sdk_available": False,
        "client_initialized": False,
        "called": False,
        "success": False,
        "error": None,
        "from_cache": False,
    }

    cached_payload = _load_cached_successful_extraction(doc)
    if cached_payload is not None:
        meta["configured"] = bool(os.environ.get("GEMINI_API_KEY"))
        meta["success"] = True
        meta["from_cache"] = True
        logger.info("Using cached successful Gemini extraction for %s", doc.path)
        return cached_payload, meta

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        # dotenv.load_dotenv already attempted to populate environment
        meta["configured"] = False
        logger.info("Gemini configured: NO")
        return None, meta
    meta["configured"] = True
    logger.info("Gemini configured: YES")

    # Use the official GenAI client `genai.Client` (modern SDK)
    try:
        from google import genai

        meta["sdk_available"] = True
        logger.info("Gemini SDK available: YES")
    except Exception as exc:
        meta["sdk_available"] = False
        meta["error"] = f"SDK import failed: {exc}"
        logger.error("Gemini SDK available: NO (%s)", exc)
        return None, meta

    try:
        # Initialize client
        client = genai.Client(api_key=key)
        meta["client_initialized"] = True
        logger.info("Gemini client initialized: YES")

        # Build prompt and contents
        text_body = _join_text(doc)
        system = (
            "Extract invoice-like structured fields from the provided document using only evidence actually present in the PDF. "
            "Return a JSON object with keys: doc_type,is_payable,payables (list). "
            "Each payable must contain invoice_number,invoice_date,due_date,invoice_type,currency,"
            "supplier{name,vat_id},payment_term_text,po_number,gross_total,subtotal,total_tax_amount,"
            "discount_amount,freight_charges,insurance_charges,extra_charges,excise_duties,taxes,line_items. "
            "Use these accounting rules exactly: "
            "(1) ERP gross is recomputed as quantity * unit_price - line discounts + line taxes - header discount + header taxes + supported charges. "
            "(2) unit_price is NET, not tax-inclusive. "
            "(3) Do not replace quantity*unit_price with the document's printed line total unless the document explicitly shows a standalone amount for that line with no quantity/rate decomposition. In that case use quantity = 1 and unit_price = standalone amount, only when clearly supported. "
            "(4) Never convert taxes into ordinary line prices; taxes must remain tax objects with tax_rate and/or tax_amount in the schema expected by ERP. "
            "Named tax rows such as 'Sales Tax', 'Gross Receipts Tax', 'VAT', 'GST', or similarly labeled rows that contain only a tax amount should be placed into the 'taxes' array (with tax_name/tax_amount), not converted into ordinary line_items. "
            "Do NOT convert a tax amount into quantity=1 / unit_price=amount merely because it appears as a standalone amount. "
            "(5) If the document gives only a tax amount and no rate, keep the amount in tax_amount and leave tax_rate empty. "
            "(6) Preserve negative adjustments or negative charges exactly as shown. "
            "(7) Do not invent quantity, price, tax, discount, freight, or other charges. Leave fields empty when unsupported. "
            "(8) Separate ordinary line items, standalone charge/adjustment lines, tax lines, discount lines, and freight/other charge lines distinctly when present. "
            "(9) For non-payable documents, set is_payable to false and do not fabricate payable records. "
            "(10) A Gemini/API failure must not be classified as a non-payable document; return a clear gemini_error or explicit extraction failure payload. "
            "Output valid JSON only."
        )

        prompt_text = f"{system}\n\nDOCUMENT_TEXT:\n{text_body}"

        # Build contents as SDK `Content` objects with `Part`s (text + images)
        contents = []
        parts = []
        # text part
        from google import genai as _genai
        parts.append(_genai.types.Part.from_text(text=prompt_text))
        # image parts
        max_images = 6
        for p in doc.non_blank_pages[:max_images]:
            parts.append(_genai.types.Part.from_bytes(data=p.image_bytes, mime_type="image/png"))
        contents.append(_genai.types.Content(parts=parts))

        model_name = "gemini-2.5-flash"
        max_retries = 2
        for attempt in range(0, max_retries + 1):
            try:
                GEMINI_STATS["calls"] += 1
                meta["called"] = True
                response = _invoke_gemini_api(client, model_name, contents)
                break
            except Exception as exc:
                status_code = None
                for attr in ("status_code", "http_status", "code", "status"):
                    val = getattr(exc, attr, None)
                    if isinstance(val, int):
                        status_code = val
                        break
                    if isinstance(val, str) and val.isdigit():
                        status_code = int(val)
                        break

                message = _normalize_gemini_failure(exc, model_name)
                if status_code == 429:
                    GEMINI_STATS["failures"] += 1
                    meta["success"] = False
                    meta["error"] = message
                    logger.error("Gemini quota exhausted for %s: %s", model_name, exc)
                    return None, meta
                if status_code == 503 and attempt < max_retries:
                    delay = min(2 ** attempt, 4)
                    logger.warning("Gemini 503 for %s on attempt %d/%d; retrying in %ss", model_name, attempt + 1, max_retries + 1, delay)
                    time.sleep(delay)
                    continue
                if status_code == 503 or "unavailable" in str(exc).lower() or "high demand" in str(exc).lower():
                    GEMINI_STATS["failures"] += 1
                    meta["success"] = False
                    meta["error"] = message
                    logger.error("Gemini unavailable for %s after retries: %s", model_name, exc)
                    return None, meta
                GEMINI_STATS["failures"] += 1
                meta["success"] = False
                meta["error"] = message
                logger.error("Gemini extraction failed for %s: %s", model_name, exc)
                return None, meta

        # Extract textual output from response
        text = ""
        # response may be a rich object; coerce to string or dict safely
        try:
            if hasattr(response, "text"):
                text = response.text
            elif isinstance(response, dict):
                text = response.get("output", "") or response.get("text", "") or str(response)
            else:
                text = str(response)
        except Exception as exc:
            # Sanitized error
            GEMINI_STATS["failures"] += 1
            meta["success"] = False
            meta["error"] = _normalize_gemini_failure(exc, model_name)
            print(f"Gemini error type: {type(exc).__name__}")
            print(f"Gemini error: {exc}")
            return None, meta

        # The model is asked to emit JSON; try to parse it
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1:
            GEMINI_STATS["failures"] += 1
            meta["success"] = False
            meta["error"] = "No JSON object found in Gemini response"
            logger.error("Gemini extraction failed: no JSON in response")
            # Print sanitized parsing error (do not include request/headers/keys)
            print("Gemini error type: ParsingError")
            print("Gemini error: No JSON object found in Gemini response")
            return None, meta
        js = text[start : end + 1]
        try:
            payload = json.loads(js)
        except Exception as exc:
            GEMINI_STATS["failures"] += 1
            meta["success"] = False
            meta["error"] = f"JSON parse error: {exc}"
            logger.exception("Gemini JSON parse failed: %s", exc)
            # Print sanitized parsing exception type and message
            print(f"Gemini error type: {type(exc).__name__}")
            print(f"Gemini error: {exc}")
            return None, meta

        GEMINI_STATS["success"] += 1
        meta["success"] = True
        # images_count: parts after the first text part
        images_count = max(0, len(parts) - 1)
        logger.info("Gemini extraction succeeded for %s pages=%d", doc.path.name, images_count)

        # Normalize payload immediately after parsing, before downstream Pydantic validation.
        # This converts numeric values to strings without rounding or inventing missing data,
        # while preserving null only for fields that the current schema allows it.
        try:
            payload = _normalize_gemini_payload(payload)
        except Exception:
            # If normalization fails, surface the raw payload and let the caller handle it.
            pass

        _store_successful_extraction(doc, payload)

        # Print the extracted JSON for inspection (sanitized)
        try:
            print("Gemini extraction succeeded")
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        except Exception:
            print("Gemini extraction succeeded (payload not printable)")

        return payload, meta
    except Exception as exc:
        GEMINI_STATS["failures"] += 1
        meta["success"] = False
        meta["error"] = str(exc)
        logger.exception("Gemini extraction failed: %s", exc)
        # Print sanitized exception type and message for root cause diagnosis
        print(f"Gemini error type: {type(exc).__name__}")
        print(f"Gemini error: {exc}")
        return None, meta


def _join_text(doc: IngestedDocument) -> str:
    """Concatenate non-blank page text using PyMuPDF page extraction.

    Falls back to empty string if text cannot be extracted.
    """
    import fitz
    texts: List[str] = []
    try:
        pdf = fitz.open(str(doc.path))
    except Exception as exc:
        logger.debug("Extractor cannot open PDF for text extraction: %s", exc)
        return ""

    for pnum in range(len(pdf)):
        try:
            page = pdf.load_page(pnum)
            t = page.get_text("text")
            texts.append(t or "")
        except Exception as exc:
            logger.debug("Failed to extract text from page %d: %s", pnum, exc)
    try:
        pdf.close()
    except Exception:
        pass

    joined = "\n".join(texts)
    if joined.strip():
        return joined

    # If textual extraction yielded nothing, try OCR on rendered page images
    try:
        from PIL import Image
        import pytesseract
        import io
    except Exception:
        logger.debug("PIL/pytesseract not available for OCR fallback")
        return joined

    ocr_texts: List[str] = []
    for p in doc.non_blank_pages:
        try:
            img = Image.open(io.BytesIO(p.image_bytes))
            t = pytesseract.image_to_string(img)
            ocr_texts.append(t or "")
        except Exception as exc:
            logger.debug("OCR failed for page %d: %s", p.page_number, exc)
    return "\n".join(ocr_texts)


# Regex helpers (conservative patterns)
_RE_INVOICE = re.compile(r"invoice\s*(?:no\.?|number|#)?\s*[:\-]?\s*([A-Z0-9\-_/]+)", re.I)
_RE_CREDIT = re.compile(r"credit\s*memo|credit\s*note", re.I)
_RE_DATE = re.compile(r"(\d{4}-\d{2}-\d{2}|\d{1,2}[\./-]\d{1,2}[\./-]\d{2,4})")
_RE_TOTAL = re.compile(r"\b(total(?:\s+due|\s+amount|\s+amount\s+due)?|amount\s+due)[:\s\-]*([€$£₹]?\s*[0-9,]+(?:\.[0-9]{2})?)", re.I)
_RE_CURRENCY = re.compile(r"\b(EUR|USD|GBP|INR|€|\$|£)\b")
_RE_SUPPLIER = re.compile(r"supplier[:\s\-]{1,20}(.{1,200})", re.I)
_RE_VAT = re.compile(r"(vat|vat\s*id|tax\s*id)[:\s\-]*([A-Z0-9\-\s\.]+)", re.I)
_RE_PO = re.compile(r"\bPO(?:\s*No\.?|\s*#|:)\s*([A-Z0-9\-_/]+)", re.I)


def _find_first(regex: re.Pattern, text: str) -> str:
    m = regex.search(text)
    if not m:
        return ""
    # return the last capturing group if present
    if m.groups():
        return m.group(m.lastindex or 1).strip()
    return m.group(0).strip()


def extract_document(doc: IngestedDocument) -> Dict[str, Any]:
    """Produce an intermediate extraction dict for a single ingested document.

    The result contains:
      - doc_type: best-effort classification string
      - is_payable: bool
      - payables: list of dicts with extracted fields (partial)

    This function is intentionally conservative: where evidence is weak we
    return empty strings rather than guessing.
    """
    # If the GEMINI_API_KEY is present attempt a multimodal extraction.
    # Behavior:
    # - If Gemini is not configured, continue to deterministic extractor.
    # - If Gemini is configured and the call succeeds, return Gemini payload.
    # - If Gemini is configured but fails (SDK missing, init error, API error,
    #   malformed response) return an extraction dict containing
    #   `gemini_error` so the pipeline can treat this as an explicit failure
    #   (no silent fallback).
    gemini_payload, gemini_meta = _extract_with_gemini(doc)
    if gemini_meta is not None and gemini_meta.get("configured"):
        # Log diagnostic flags
        logger.info("Gemini configured: %s", "YES" if gemini_meta.get("configured") else "NO")
        logger.info("Gemini SDK available: %s", "YES" if gemini_meta.get("sdk_available") else "NO")
        logger.info("Gemini client initialized: %s", "YES" if gemini_meta.get("client_initialized") else "NO")
        logger.info("Gemini extraction called: %s", "YES" if gemini_meta.get("called") else "NO")
        logger.info("Gemini extraction succeeded: %s", "YES" if gemini_meta.get("success") else "NO")

        if not gemini_meta.get("success"):
            # Gemini was configured but failed — do NOT silently fall back.
            err = gemini_meta.get("error") or "unknown gemini failure"
            out = {
                "doc_type": "EXTRACTION_FAILED",
                "is_payable": False,
                "payables": [],
                "pages_considered": [p.page_number for p in doc.non_blank_pages],
                "gemini_error": err,
                "gemini_meta": gemini_meta,
            }
            return out

        if gemini_payload is not None:
            return gemini_payload

    text = _join_text(doc)
    out: Dict[str, Any] = {
        "doc_type": "UNKNOWN",
        "is_payable": False,
        "payables": [],
        "pages_considered": [p.page_number for p in doc.non_blank_pages],
    }

    if not text.strip():
        out["doc_type"] = "EMPTY"
        return out

    # classification: credit memo vs invoice vs other keywords
    if _RE_CREDIT.search(text):
        out["doc_type"] = "CREDIT_MEMO"
    elif re.search(r"invoice|tax invoice|invoice to|bill to", text, re.I):
        out["doc_type"] = "INVOICE"
    elif re.search(r"delivery note|packing list|reminder|statement|quotation|estimate", text, re.I):
        out["doc_type"] = "NON_PAYABLE"
    else:
        out["doc_type"] = "UNKNOWN"

    # Basic payable detection
    if out["doc_type"] in ("INVOICE", "CREDIT_MEMO"):
        out["is_payable"] = True

    # Extract top-level fields
    invoice_no = _find_first(_RE_INVOICE, text)
    invoice_date = _find_first(_RE_DATE, text)
    po_no = _find_first(_RE_PO, text)
    supplier = _find_first(_RE_SUPPLIER, text)
    vat = _find_first(_RE_VAT, text)

    # Currency: prefer ISO words or symbols
    cur = _find_first(_RE_CURRENCY, text)
    if cur in ("€", "$", "£"):
        cur_map = {"€": "EUR", "$": "USD", "£": "GBP"}
        currency = cur_map.get(cur, "")
    else:
        currency = cur.upper() if cur else ""

    # Total: pick the last total-like occurrence (conservative)
    tots = list(_RE_TOTAL.finditer(text))
    gross = ""
    if tots:
        # prefer the last match which is often the final total
        gross = tots[-1].group(2).replace(" ", "").replace(",", "")

    payable = {
        "invoice_number": invoice_no,
        "invoice_date": invoice_date,
        "due_date": "",
        "invoice_type": "CREDIT_MEMO" if out["doc_type"] == "CREDIT_MEMO" else "INVOICE",
        "currency": currency,
        "supplier": {
            "name": supplier or "",
            "vat_id": vat or "",
        },
        "payment_term_id": "",
        "po_number": po_no or "",
        # totals and header fields
        "gross_total": gross,
        "subtotal": "",
        "total_tax_amount": "",
        "discount_amount": "",
        "freight_charges": "",
        "insurance_charges": "",
        "extra_charges": "",
        "excise_duties": "",
        "taxes": [],
        "line_items": [],
    }

    # Attempt a simple line-item extraction: look for lines containing qty x unit_price
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    li_pattern = re.compile(r"(\d+(?:[\.,]\d+)?)[ \t]*[xX\*][ \t]*(?:@)?[ \t]*([0-9,]+(?:\.[0-9]{2})?)")
    for ln in lines:
        m = li_pattern.search(ln)
        if m:
            qty = m.group(1).replace(",", ".")
            up = m.group(2).replace(",", "")
            # description is the line up to the match
            desc = ln[: m.start()].strip()[:200]
            li = {
                "description": desc,
                "item_type": "SERVICE",
                "uom": "",
                "quantity": qty,
                "unit_price": up,
                "total": "",
                "discount": "",
                "discount_percentage": "",
                "tax_rate": "",
                "tax_amount": "",
                "taxes": [],
            }
            payable["line_items"].append(li)

    # Conservative fallback: some invoices print a single charge amount without quantity
    # or unit price (e.g. "Customer Charge  |  299.78"). ERP expects qty x unit_price to
    # recompute line bases. If the document shows a standalone description followed by
    # a single monetary amount and there is no explicit qty x price on that snippet,
    # represent it as quantity=1 and unit_price=amount. This is conservative: we only
    # apply it when we can detect a clear description+amount pair and avoid inventing
    # values for complex lines that already contain qty/unit markers.
    simple_amount_re = re.compile(r"(?P<desc>[A-Za-z0-9 &\-\(\)\/]{3,120}?)\s*[\|\t]\s*[$€£]?\s*(?P<amt>-?\d{1,3}(?:,\d{3})*(?:\.\d+))")
    for ln in lines:
        # skip snippets that already contained a qty x price (we already captured those)
        if li_pattern.search(ln):
            continue
        m2 = simple_amount_re.search(ln)
        if not m2:
            continue
        desc = m2.group('desc').strip()
        amt = m2.group('amt').replace(',', '')
        # avoid picking up header totals (look for keywords that suggest a line item)
        if len(desc) < 3 or any(k in desc.lower() for k in ('total', 'amount due', 'invoice', 'balance')):
            continue
        # ensure we don't duplicate an existing line with same description
        exists = any(li.get('description', '').strip().lower() == desc.lower() for li in payable['line_items'])
        if exists:
            continue
        # map to qty=1 and unit_price=amt (as strings, matching normalization expectations)
        li = {
            'description': desc,
            'item_type': 'SERVICE',
            'uom': '',
            'quantity': '1',
            'unit_price': amt,
            'total': '',
            'discount': '',
            'discount_percentage': '',
            'tax_rate': '',
            'tax_amount': '',
            'taxes': [],
        }
        payable['line_items'].append(li)

    out["payables"].append(payable)
    return out
