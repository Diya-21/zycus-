"""src/pipeline.py — Connect ingestion, extraction, matching, reconciliation, and serialization.

This pipeline uses the conservative extractor (no LLM) and the deterministic
master-data matcher to populate the final Pydantic models. It writes one JSON
output file per input PDF into the provided output directory.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, Any, List

from src.ingestion import load_pdf, IngestedDocument
from src.extractor import extract_document, GEMINI_STATS
from src.matcher import (
    match_supplier,
    match_po,
    match_payment_term,
    match_tax_code,
    resolve_buyer,
)
from src.models import AutodraftFile, Payable, Supplier, Buyer
from src.reconciler import run_erp_validation

logger = logging.getLogger(__name__)


def _build_payable(extracted: Dict[str, Any]) -> Dict[str, Any]:
    """Take the intermediate extraction and apply master-data matching to
    yield a JSON-compatible payable dict that matches the AUTODRAFT schema.
    """
    p = extracted
    sup = p.get("supplier", {}) or {}
    supplier_name = sup.get("name", "")
    supplier_vat = sup.get("vat_id", "")
    supplier_id = match_supplier(vat_id=supplier_vat, name=supplier_name)

    # Buyer resolution: best-effort using no country evidence here
    buyer = resolve_buyer()

    po_id = ""
    if p.get("po_number"):
        po_id = match_po(p.get("po_number"))

    # Payment term: attempt text/dates match
    pt = match_payment_term(text=p.get("payment_term_text", ""), invoice_date=p.get("invoice_date", ""), due_date=p.get("due_date", ""))

    payable = {
        "invoice_number": p.get("invoice_number", ""),
        "invoice_date": p.get("invoice_date", ""),
        "due_date": p.get("due_date", ""),
        "invoice_type": p.get("invoice_type", "INVOICE"),
        "currency": p.get("currency", ""),
        "supplier": {
            "name": supplier_name,
            "supplier_id": supplier_id or "",
            "address": sup.get("address", ""),
            "vat_id": supplier_vat or "",
        },
        "buyer": buyer.model_dump() if hasattr(buyer, "model_dump") else {
            "company_code": "",
            "business_unit_code": "",
            "location_code": "",
        },
        "payment_term_id": pt or "",
        "po_number": p.get("po_number", ""),
        "po_id": po_id or "",
        # Totals & charges
        "gross_total": p.get("gross_total", ""),
        "subtotal": p.get("subtotal", ""),
        "total_tax_amount": p.get("total_tax_amount", ""),
        "discount_amount": p.get("discount_amount", ""),
        "freight_charges": p.get("freight_charges", ""),
        "insurance_charges": p.get("insurance_charges", ""),
        "extra_charges": p.get("extra_charges", ""),
        "excise_duties": p.get("excise_duties", ""),
        "taxes": p.get("taxes", []),
        "line_items": p.get("line_items", []),
    }
    return payable


def process_file(input_path: Path, output_dir: Path) -> Dict[str, Any]:
    """Process a single PDF file and write output JSON to output_dir.

    Returns a summary dict with processing diagnostics.
    """
    doc = load_pdf(input_path)
    out_file = AutodraftFile(file=input_path.name)
    summary: Dict[str, Any] = {"file": input_path.name, "status": "ok", "errors": [], "payable_records": 0, "erp_failures": 0, "gemini_called": False}

    if not doc.is_usable:
        summary["status"] = "failed"
        summary["errors"].append(doc.error or "no usable pages")
        # Write declined with reason using DeclinedRecord to avoid Pydantic warnings
        from src.models import DeclinedRecord
        out_file.declined.append(DeclinedRecord(doc_type="UNREADABLE", reason=doc.error or "Unreadable or blank PDF"))
        out_path = output_dir / (input_path.stem + ".json")
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(out_file.model_dump_json(indent=2))
        return summary

    # Snapshot GEMINI stats before extraction
    gem_before = dict(GEMINI_STATS)
    extracted = extract_document(doc)
    gem_after = dict(GEMINI_STATS)
    summary["gemini_called"] = gem_after.get("calls", 0) > gem_before.get("calls", 0)

    # If extraction reported a Gemini-specific error, treat as explicit failure
    if extracted.get("gemini_error"):
        err = extracted.get("gemini_error")
        summary["status"] = "failed"
        summary["errors"].append(f"Gemini extraction error: {err}")
        from src.models import DeclinedRecord
        out_file.declined.append(DeclinedRecord(doc_type="EXTRACTION_FAILED", reason=err))
        out_path = output_dir / (input_path.stem + ".json")
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(out_file.model_dump_json(indent=2))
        return summary

    if not extracted.get("is_payable"):
        from src.models import DeclinedRecord
        out_file.declined.append(DeclinedRecord(doc_type=extracted.get("doc_type", "UNKNOWN"), reason="Document is not a payable"))
        out_path = output_dir / (input_path.stem + ".json")
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(out_file.model_dump_json(indent=2))
        summary["status"] = "declined"
        return summary

    # For each extracted payable create full payable and validate
    for p in extracted.get("payables", []):
        payable_json = _build_payable(p)
        # Run ERP validation
        erp_diag = run_erp_validation(payable_json)
        payable_json["erp_validation"] = erp_diag
        # Count ERP mismatches
        if not erp_diag.get("match", False):
            summary["erp_failures"] += 1
        summary["payable_records"] += 1

        # Validate with Pydantic Payable model
        try:
            pay_model = Payable.model_validate(payable_json)
            out_file.payables.append(pay_model)
        except Exception as exc:
            logger.exception("Pydantic validation failed for %s: %s", input_path, exc)
            summary["status"] = "failed"
            summary["errors"].append(str(exc))

    # Write output JSON
    out_path = output_dir / (input_path.stem + ".json")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(out_file.model_dump_json(indent=2))

    return summary
