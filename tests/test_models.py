"""tests/test_models.py — Unit tests for Autodraft Pydantic models.

Tests:
1. Loading and validating candidate_kit/sample_autodraft.json
2. Serializing back to JSON and verifying integrity
3. Compatibility with candidate_kit/erp.py oracle
4. Validating AutodraftFile container with both payable and declined items
"""
import json
import os
import sys
from pathlib import Path

# Ensure workspace root and candidate_kit are in sys.path
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE_ROOT))
sys.path.insert(0, str(WORKSPACE_ROOT / "candidate_kit"))

from src.models import Payable, AutodraftFile, DeclinedRecord
from src.extractor import _normalize_gemini_failure
from erp import erp_book


def test_load_sample_autodraft():
    sample_path = WORKSPACE_ROOT / "candidate_kit" / "sample_autodraft.json"
    assert sample_path.exists(), f"Sample file not found at {sample_path}"

    with open(sample_path, "r", encoding="utf-8") as f:
        raw_data = json.load(f)

    # Validate using Pydantic Payable model
    payable = Payable.model_validate(raw_data)

    # Basic field assertions
    assert payable.invoice_number == "265345"
    assert payable.invoice_date == "2026-02-02"
    assert payable.currency == "EUR"
    assert payable.gross_total == "438.00"
    assert payable.supplier.name == "Phocus Direct Communication GmbH"
    assert payable.supplier.supplier_id == "2845695"
    assert payable.buyer.company_code == "BOLTGROUP"
    assert payable.payment_term_id == "Net_10"
    assert len(payable.taxes) == 1
    assert payable.taxes[0].tax_type == "VAT"
    assert payable.taxes[0].tax_rate == "0"
    assert len(payable.line_items) == 2
    assert payable.line_items[0].quantity == "4"
    assert payable.line_items[0].unit_price == "73.00"

    # Serialize back to dict and JSON string
    serialized_dict = payable.model_dump()
    serialized_json = payable.model_dump_json(indent=2)
    assert isinstance(serialized_json, str)
    assert len(serialized_json) > 0

    # Ensure serialized dict can be fed into ERP oracle without modification
    erp_result = erp_book(serialized_dict)
    assert erp_result["will_book_gross"] == 438.00
    assert erp_result["currency"] == "EUR"


def test_autodraft_file_wrapper():
    sample_path = WORKSPACE_ROOT / "candidate_kit" / "sample_autodraft.json"
    with open(sample_path, "r", encoding="utf-8") as f:
        raw_data = json.load(f)

    payable = Payable.model_validate(raw_data)

    # Test file with 1 payable
    file_output = AutodraftFile(
        file="INV-01.pdf",
        payables=[payable],
        declined=[]
    )
    dumped = file_output.model_dump()
    assert dumped["file"] == "INV-01.pdf"
    assert len(dumped["payables"]) == 1
    assert dumped["declined"] == []

    # Test file with declined entry
    declined_file = AutodraftFile(
        file="DU-08.pdf",
        payables=[],
        declined=[
            DeclinedRecord(
                doc_type="REMINDER",
                reason="Dunning letter / payment reminder (Mahnung), not an invoice or credit memo"
            )
        ]
    )
    declined_dumped = declined_file.model_dump()
    assert declined_dumped["file"] == "DU-08.pdf"
    assert len(declined_dumped["payables"]) == 0
    assert len(declined_dumped["declined"]) == 1
    assert declined_dumped["declined"][0]["doc_type"] == "REMINDER"


def test_gemini_quota_failure_message_is_canonical():
    msg = _normalize_gemini_failure("429 RESOURCE_EXHAUSTED: The request has been rate limited for gemini-2.5-flash")
    assert msg == "Gemini quota exhausted for gemini-2.5-flash"


def test_gemini_unavailable_failure_message_is_transient():
    msg = _normalize_gemini_failure("503 UNAVAILABLE: model is currently experiencing high demand")
    assert "Gemini service unavailable" in msg
    assert "gemini-2.5-flash" in msg


if __name__ == "__main__":
    test_load_sample_autodraft()
    test_autodraft_file_wrapper()
    test_gemini_quota_failure_message_is_canonical()
    test_gemini_unavailable_failure_message_is_transient()
    print("All models tests passed successfully!")
