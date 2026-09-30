import os
from pathlib import Path
from src.ingestion import load_pdf
from src.matcher import reset_index, match_supplier


def test_ingestion_loads_sample_pdf():
    sample = Path(__file__).resolve().parent.parent / "candidate_kit" / "documents"
    # pick any PDF present
    candidates = list(sample.glob("*.pdf"))
    assert candidates, "No sample PDFs found in candidate_kit/documents"
    doc = load_pdf(candidates[0])
    assert doc.total_pages >= 1
    # Either has non-blank pages or an error explaining why
    assert doc.is_usable or doc.error is not None


def test_matcher_supplier_by_vat():
    reset_index()
    # Known supplier in master data
    vid = "DE209177122"
    sid = match_supplier(vat_id=vid)
    assert sid == "2845695"
