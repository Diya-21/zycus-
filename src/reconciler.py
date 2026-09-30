"""src/reconciler.py — Accounting reconciler and ERP validator.

Uses the supplied candidate_kit/erp.py oracle to recompute the ERP booked
gross for each payable and compares it with the document-printed gross.
The reconciler produces a short diagnostics dict describing any mismatch.
"""
from __future__ import annotations

from typing import Dict, Any
import logging
import json
from pathlib import Path

logger = logging.getLogger(__name__)


def run_erp_validation(payable_obj: Dict[str, Any]) -> Dict[str, Any]:
    """Call the candidate_kit erp.py oracle to recompute will_book_gross.

    Returns a dict with keys:
      - erp: the raw oracle result
      - printed_gross: parsed float of payable_obj['gross_total'] (or None)
      - match: True/False whether they are equal (2dp)
      - diff: numeric difference (erp - printed)
    """
    # Import locally to avoid circular imports in tests
    import candidate_kit.erp as erp

    # ERP expects a mapping with numeric strings etc.; pass through
    erp_res = erp.erp_book(payable_obj)

    def _to_float(s):
        try:
            if s is None or str(s).strip() == "":
                return None
            return float(str(s).strip().replace(",", ""))
        except Exception:
            return None

    printed = _to_float(payable_obj.get("gross_total"))
    will_book = erp_res.get("will_book_gross")

    match = False
    diff = None
    if printed is None:
        match = False
        diff = None
    else:
        diff = round(will_book - float(printed), 2)
        match = abs(diff) < 0.005

    out = {
        "erp": erp_res,
        "printed_gross": printed,
        "match": match,
        "diff": diff,
    }
    return out
