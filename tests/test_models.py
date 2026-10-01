"""tests/test_models.py — Unit tests for Autodraft models and extraction behavior.

Covers:
- Pydantic model validation using candidate_kit/sample_autodraft.json
- Canonical failure/normalization helpers in `src.extractor`
- Pipeline behavior for genuine non-payable vs provider failure
- Local Gemini extraction cache semantics

Note: tests avoid any real network calls by monkeypatching the google.genai Client.
"""

import json
import sys
import types
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE_ROOT))
sys.path.insert(0, str(WORKSPACE_ROOT / "candidate_kit"))

from erp import erp_book
from src.extractor import _extract_with_gemini, _normalize_gemini_failure, _normalize_gemini_payload
from src.ingestion import IngestedDocument, PageData
from src.models import AutodraftFile, DeclinedRecord, Payable
from src.pipeline import process_file


def test_load_sample_autodraft():
    sample_path = WORKSPACE_ROOT / "candidate_kit" / "sample_autodraft.json"
    assert sample_path.exists(), f"Sample file not found at {sample_path}"

    with open(sample_path, "r", encoding="utf-8") as handle:
        raw_data = json.load(handle)

    payable = Payable.model_validate(raw_data)

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

    serialized_dict = payable.model_dump()
    serialized_json = payable.model_dump_json(indent=2)
    assert isinstance(serialized_json, str)
    assert len(serialized_json) > 0

    erp_result = erp_book(serialized_dict)
    assert erp_result["will_book_gross"] == 438.00
    assert erp_result["currency"] == "EUR"


def test_autodraft_file_wrapper():
    sample_path = WORKSPACE_ROOT / "candidate_kit" / "sample_autodraft.json"
    with open(sample_path, "r", encoding="utf-8") as handle:
        raw_data = json.load(handle)

    payable = Payable.model_validate(raw_data)

    file_output = AutodraftFile(file="INV-01.pdf", payables=[payable], declined=[])
    dumped = file_output.model_dump()
    assert dumped["file"] == "INV-01.pdf"
    assert len(dumped["payables"]) == 1
    assert dumped["declined"] == []

    declined_file = AutodraftFile(
        file="DU-08.pdf",
        payables=[],
        declined=[DeclinedRecord(doc_type="REMINDER", reason="Dunning letter / payment reminder (Mahnung), not an invoice or credit memo")],
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


def test_standalone_amount_maps_to_quantity_one_and_unit_price():
    payload = {"line_items": [{"description": "Customer Charge", "amount": 299.78}]}
    normalized = _normalize_gemini_payload(payload)
    assert normalized["line_items"][0]["quantity"] == "1"
    assert normalized["line_items"][0]["unit_price"] == "299.78"


def test_tax_amount_preserved_and_tax_rate_left_empty_when_not_supported():
    payload = {"taxes": [{"name": "State Tax Adjustment", "amount": "-0.28"}]}
    normalized = _normalize_gemini_payload(payload)
    assert normalized["taxes"][0]["tax_amount"] == "-0.28"
    assert normalized["taxes"][0].get("tax_rate", "") == ""


def test_negative_adjustment_is_preserved():
    payload = {"line_items": [{"description": "ADJUSTMENT", "amount": -0.01}]}
    normalized = _normalize_gemini_payload(payload)
    assert normalized["line_items"][0]["quantity"] == "1"
    assert normalized["line_items"][0]["unit_price"] == "-0.01"


def test_numeric_gemini_values_are_stringified_for_schema():
    payload = {
        "gross_total": 17657.53,
        "line_items": [{
            "quantity": 475,
            "unit_price": 4.77,
            "taxes": [{"tax_rate": 5.0, "tax_amount": 12.34}],
        }],
    }
    normalized = _normalize_gemini_payload(payload)
    assert normalized["gross_total"] == "17657.53"
    assert normalized["line_items"][0]["quantity"] == "475"
    assert normalized["line_items"][0]["unit_price"] == "4.77"
    assert normalized["line_items"][0]["taxes"][0]["tax_rate"] == "5.0"
    assert normalized["line_items"][0]["taxes"][0]["tax_amount"] == "12.34"


def test_genuine_non_payable_document_is_declined(monkeypatch, tmp_path):
    pdf_path = tmp_path / "REMINDER.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    class FakeDoc:
        is_usable = True
        error = None
        non_blank_pages = [types.SimpleNamespace(page_number=0)]

    monkeypatch.setattr("src.pipeline.load_pdf", lambda _path: FakeDoc())
    monkeypatch.setattr("src.pipeline.extract_document", lambda _doc: {"doc_type": "REMINDER", "is_payable": False, "payables": []})

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    summary = process_file(pdf_path, out_dir)

    assert summary["status"] == "declined"
    payload = json.loads((out_dir / "REMINDER.json").read_text(encoding="utf-8"))
    assert payload["declined"][0]["doc_type"] == "REMINDER"


def test_provider_failure_is_not_declined_as_non_payable(monkeypatch, tmp_path):
    pdf_path = tmp_path / "FAIL.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    class FakeDoc:
        is_usable = True
        error = None
        non_blank_pages = [types.SimpleNamespace(page_number=0)]

    monkeypatch.setattr("src.pipeline.load_pdf", lambda _path: FakeDoc())
    monkeypatch.setattr("src.pipeline.extract_document", lambda _doc: {
        "doc_type": "EXTRACTION_FAILED",
        "is_payable": False,
        "payables": [],
        "gemini_error": "Gemini quota exhausted for gemini-2.5-flash",
    })

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    summary = process_file(pdf_path, out_dir)

    assert summary["status"] == "failed"
    payload = json.loads((out_dir / "FAIL.json").read_text(encoding="utf-8"))
    assert payload["declined"][0]["doc_type"] == "EXTRACTION_FAILED"
    assert "quota exhausted" in payload["declined"][0]["reason"].lower()


def test_successful_extraction_uses_cache_on_second_run(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_CACHE_ENABLED", "true")
    cache_dir = tmp_path / "gemini-cache"
    monkeypatch.setenv("GEMINI_CACHE_DIR", str(cache_dir))

    class FakeResponse:
        text = json.dumps({
            "doc_type": "INVOICE",
            "is_payable": True,
            "payables": [{
                "invoice_number": "INV-42",
                "invoice_date": "2026-03-01",
                "due_date": "2026-03-10",
                "invoice_type": "INVOICE",
                "currency": "USD",
                "supplier": {"name": "Acme", "vat_id": "VAT-1"},
                "payment_term_text": "Net 30",
                "po_number": "PO-9",
                "gross_total": "100.00",
                "subtotal": "100.00",
                "total_tax_amount": "0.00",
                "discount_amount": "",
                "freight_charges": "",
                "insurance_charges": "",
                "extra_charges": "",
                "excise_duties": "",
                "taxes": [],
                "line_items": [{"description": "Widget", "quantity": "1", "unit_price": "100.00"}],
            }],
        })

    class FakeClient:
        def __init__(self, api_key):
            self.api_key = api_key
            self.models = types.SimpleNamespace(generate_content=self._generate_content)

        def _generate_content(self, **_kwargs):
            return FakeResponse()

    fake_google = types.ModuleType("google")
    fake_genai = types.ModuleType("google.genai")
    fake_genai.types = types.SimpleNamespace(
        Part=types.SimpleNamespace(
            from_text=lambda text=None, **_kwargs: {"text": text},
            from_bytes=lambda data=None, mime_type=None, **_kwargs: {"bytes": data, "mime_type": mime_type},
        ),
        Content=lambda parts=None: {"parts": parts or []},
    )
    fake_genai.Client = FakeClient
    sys.modules["google"] = fake_google
    sys.modules["google.genai"] = fake_genai
    fake_google.genai = fake_genai

    doc = IngestedDocument(
        filename="INV-42.pdf",
        path=tmp_path / "INV-42.pdf",
        pages=[PageData(page_number=0, image_bytes=b"test", width_px=100, height_px=100, is_blank=False)],
    )
    first, meta1 = _extract_with_gemini(doc)
    second, meta2 = _extract_with_gemini(doc)

    assert first is not None
    assert second is not None
    assert meta1["success"] is True
    assert meta2["success"] is True
    assert meta2.get("from_cache") is True
    assert cache_dir.exists() and any(cache_dir.iterdir())


def test_failed_extraction_is_not_cached_as_success(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_CACHE_ENABLED", "true")
    cache_dir = tmp_path / "gemini-cache"
    monkeypatch.setenv("GEMINI_CACHE_DIR", str(cache_dir))

    class FakeClient:
        def __init__(self, api_key):
            self.api_key = api_key
            self.models = types.SimpleNamespace(generate_content=self._generate_content)

        def _generate_content(self, **_kwargs):
            raise RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")

    fake_google = types.ModuleType("google")
    fake_genai = types.ModuleType("google.genai")
    fake_genai.types = types.SimpleNamespace(
        Part=types.SimpleNamespace(
            from_text=lambda text=None, **_kwargs: {"text": text},
            from_bytes=lambda data=None, mime_type=None, **_kwargs: {"bytes": data, "mime_type": mime_type},
        ),
        Content=lambda parts=None: {"parts": parts or []},
    )
    fake_genai.Client = FakeClient
    sys.modules["google"] = fake_google
    sys.modules["google.genai"] = fake_genai
    fake_google.genai = fake_genai

    doc = IngestedDocument(
        filename="FAIL-42.pdf",
        path=tmp_path / "FAIL-42.pdf",
        pages=[PageData(page_number=0, image_bytes=b"test", width_px=100, height_px=100, is_blank=False)],
    )
    payload, meta = _extract_with_gemini(doc)

    assert payload is None
    assert meta["success"] is False
    assert not any(cache_dir.iterdir()) if cache_dir.exists() else True


if __name__ == "__main__":
    print("Run `pytest -q` to execute tests")
