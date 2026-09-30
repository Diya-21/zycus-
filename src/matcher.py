"""src/matcher.py — Deterministic master-data lookup engine.

Loads the five master-data JSON files and exposes clean lookup functions.
All matching is deterministic — no LLM involved.

Strategy priority per entity:
  Supplier  : exact VAT ID → exact IBAN → normalised name
  Tax       : exact (country, tax_type, rate) → country + rate only
  Payment   : text-alias match (case-insensitive) → date-delta
  PO        : exact po_number string
  Buyer     : country code → BU match, address keyword match

Design note: master files are small today but the pattern must work at
millions-of-rows scale (in that case, swap the in-memory dicts for a DB).
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.models import Buyer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path resolution helpers
# ---------------------------------------------------------------------------

def _find_master_data_dir() -> Path:
    """Return the master_data directory regardless of working directory."""
    # Try relative to this file
    here = Path(__file__).resolve().parent.parent
    candidate = here / "candidate_kit" / "master_data"
    if candidate.is_dir():
        return candidate
    # Try cwd-relative
    cwd_candidate = Path.cwd() / "candidate_kit" / "master_data"
    if cwd_candidate.is_dir():
        return cwd_candidate
    raise FileNotFoundError(f"Cannot find master_data directory (tried {candidate} and {cwd_candidate})")


def _load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

def _normalise(text: str) -> str:
    """Lowercase, strip accents, collapse whitespace, remove punctuation."""
    if not text:
        return ""
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _norm_iban(iban: str) -> str:
    return re.sub(r"\s+", "", iban or "").upper()


def _norm_vat(vat: str) -> str:
    return re.sub(r"[\s\-\.]", "", vat or "").upper()


# ---------------------------------------------------------------------------
# MasterDataIndex — loaded once and cached
# ---------------------------------------------------------------------------

class MasterDataIndex:
    """In-memory index of all five master-data files."""

    def __init__(self, master_data_dir: Optional[Path] = None) -> None:
        d = master_data_dir or _find_master_data_dir()
        self._load_suppliers(d / "suppliers.json")
        self._load_taxes(d / "tax_master.json")
        self._load_payment_terms(d / "payment_terms.json")
        self._load_po_master(d / "po_master.json")
        self._load_chart_of_books(d / "chart_of_books.json")

    # ------------------------------------------------------------------
    # Suppliers
    # ------------------------------------------------------------------

    def _load_suppliers(self, path: Path) -> None:
        data = _load_json(path)
        self._sup_by_vat: Dict[str, dict] = {}
        self._sup_by_iban: Dict[str, dict] = {}
        self._sup_by_name: Dict[str, dict] = {}
        self._sup_by_email: Dict[str, dict] = {}

        for s in data.get("suppliers", []):
            vat = _norm_vat(s.get("vat_id", ""))
            if vat:
                self._sup_by_vat[vat] = s

            iban = _norm_iban(s.get("bank_iban", ""))
            if iban:
                self._sup_by_iban[iban] = s

            name_key = _normalise(s.get("name", ""))
            if name_key:
                self._sup_by_name[name_key] = s

            email = (s.get("email", "") or "").lower().strip()
            if email:
                self._sup_by_email[email] = s

        logger.debug("Loaded %d suppliers", len(self._sup_by_name))

    def match_supplier(
        self,
        *,
        vat_id: str = "",
        iban: str = "",
        name: str = "",
        email: str = "",
    ) -> str:
        """Return supplier_id if found, else empty string.

        Matching order: exact VAT → exact IBAN → exact email → normalised name.
        """
        if vat_id:
            norm = _norm_vat(vat_id)
            hit = self._sup_by_vat.get(norm)
            if hit:
                logger.debug("Supplier matched by VAT: %s → %s", vat_id, hit["supplier_id"])
                return hit["supplier_id"]

        if iban:
            norm = _norm_iban(iban)
            hit = self._sup_by_iban.get(norm)
            if hit:
                logger.debug("Supplier matched by IBAN: %s → %s", iban, hit["supplier_id"])
                return hit["supplier_id"]

        if email:
            norm = email.lower().strip()
            hit = self._sup_by_email.get(norm)
            if hit:
                logger.debug("Supplier matched by email: %s → %s", email, hit["supplier_id"])
                return hit["supplier_id"]

        if name:
            norm = _normalise(name)
            # Exact normalised match
            hit = self._sup_by_name.get(norm)
            if hit:
                logger.debug("Supplier matched by exact name: %s → %s", name, hit["supplier_id"])
                return hit["supplier_id"]
            # Substring match (document may print a shortened name)
            for key, s in self._sup_by_name.items():
                if norm in key or key in norm:
                    logger.debug("Supplier matched by substring: %s → %s", name, s["supplier_id"])
                    return s["supplier_id"]

        logger.debug("No supplier match for vat=%r name=%r iban=%r", vat_id, name, iban)
        return ""

    # ------------------------------------------------------------------
    # Tax master
    # ------------------------------------------------------------------

    def _load_taxes(self, path: Path) -> None:
        data = _load_json(path)
        # Primary key: (country_upper, tax_type_upper, rate_normalised)
        self._tax_by_country_type_rate: Dict[Tuple[str, str, float], dict] = {}
        # Secondary: (country_upper, rate_normalised) — for when type is unclear
        self._tax_by_country_rate: Dict[Tuple[str, float], list] = {}

        for t in data.get("taxes", []):
            country = (t.get("country") or "").upper()
            tax_type = (t.get("tax_type") or "").upper()
            rate = float(t.get("rate", 0))
            key1 = (country, tax_type, rate)
            self._tax_by_country_type_rate[key1] = t

            key2 = (country, rate)
            self._tax_by_country_rate.setdefault(key2, []).append(t)

        logger.debug("Loaded %d tax records", len(self._tax_by_country_type_rate))

    def match_tax_code(
        self,
        *,
        country: str,
        tax_type: str = "",
        rate: float,
    ) -> str:
        """Return tax_type_code if found, else empty string."""
        c = country.upper()
        tt = tax_type.upper()
        r = round(float(rate), 4)

        if tt:
            hit = self._tax_by_country_type_rate.get((c, tt, r))
            if hit:
                return hit["code"]

        # Try without type (use rate + country only)
        candidates = self._tax_by_country_rate.get((c, r), [])
        if len(candidates) == 1:
            return candidates[0]["code"]
        if len(candidates) > 1 and tt:
            # Try partial type match
            for cand in candidates:
                if tt in cand.get("tax_type", "").upper() or cand.get("tax_type", "").upper() in tt:
                    return cand["code"]

        logger.debug("No tax code match: country=%r type=%r rate=%r", country, tax_type, rate)
        return ""

    # ------------------------------------------------------------------
    # Payment terms
    # ------------------------------------------------------------------

    def _load_payment_terms(self, path: Path) -> None:
        data = _load_json(path)
        # alias_lower → payment_term_id
        self._pt_by_alias: Dict[str, str] = {}
        # days → payment_term_id (only when days is unique)
        self._pt_by_days: Dict[int, str] = {}

        for pt in data.get("payment_terms", []):
            pid = pt["payment_term_id"]
            days = int(pt.get("days", -1))
            if days >= 0:
                # If duplicate days, store None to indicate ambiguity
                if days in self._pt_by_days and self._pt_by_days[days] != pid:
                    self._pt_by_days[days] = ""  # ambiguous
                else:
                    self._pt_by_days[days] = pid
            for alias in pt.get("text_aliases", []):
                self._pt_by_alias[alias.lower().strip()] = pid

        logger.debug("Loaded %d payment term aliases", len(self._pt_by_alias))

    def match_payment_term(
        self,
        *,
        text: str = "",
        invoice_date: str = "",
        due_date: str = "",
    ) -> str:
        """Return payment_term_id, or empty string if no reliable match.

        Strategy:
          1. Case-insensitive alias substring match on printed text.
          2. Date-delta: (due_date - invoice_date).days.
        """
        if text:
            text_lower = text.lower().strip()
            # Exact alias match
            if text_lower in self._pt_by_alias:
                return self._pt_by_alias[text_lower]
            # Substring containment
            for alias, pid in self._pt_by_alias.items():
                if alias in text_lower:
                    return pid

        # Fall back to date delta
        if invoice_date and due_date:
            try:
                d1 = _parse_date(invoice_date)
                d2 = _parse_date(due_date)
                delta = (d2 - d1).days
                pid = self._pt_by_days.get(delta, "")
                if pid:
                    return pid
            except Exception as exc:
                logger.debug("Date delta payment term failed: %s", exc)

        return ""

    # ------------------------------------------------------------------
    # PO master
    # ------------------------------------------------------------------

    def _load_po_master(self, path: Path) -> None:
        data = _load_json(path)
        self._po_by_number: Dict[str, str] = {}  # po_number → po_id
        for po in data.get("purchase_orders", []):
            po_num = (po.get("po_number") or "").strip()
            po_id = po.get("po_id", "")
            if po_num:
                self._po_by_number[po_num] = po_id
                # Also normalise
                self._po_by_number[po_num.upper()] = po_id
        logger.debug("Loaded %d PO records", len(self._po_by_number))

    def match_po(self, po_number: str) -> str:
        """Return po_id if the printed PO number is in the master, else empty string."""
        if not po_number:
            return ""
        stripped = po_number.strip()
        # Exact match first
        hit = self._po_by_number.get(stripped)
        if hit:
            return hit
        # Upper case
        hit = self._po_by_number.get(stripped.upper())
        if hit:
            return hit
        return ""

    # ------------------------------------------------------------------
    # Chart of books (buyer resolution)
    # ------------------------------------------------------------------

    def _load_chart_of_books(self, path: Path) -> None:
        data = _load_json(path)
        # Flat list of (company_code, bu_code, location_code, address, bu_name, location_name, company_name)
        self._cob_entries: List[dict] = []
        # country_code_2 → list of entries
        self._cob_by_country: Dict[str, List[dict]] = {}

        for company in data.get("companies", []):
            cc = company["company_code"]
            cn = company.get("company_name", "")
            for bu in company.get("business_units", []):
                buc = bu["business_unit_code"]
                bun = bu.get("business_unit_name", "")
                for loc in bu.get("locations", []):
                    lc = loc["location_code"]
                    ln = loc.get("location_name", "")
                    addr = loc.get("invoice_to_address", "")
                    entry = {
                        "company_code": cc,
                        "company_name": cn,
                        "business_unit_code": buc,
                        "business_unit_name": bun,
                        "location_code": lc,
                        "location_name": ln,
                        "invoice_to_address": addr,
                    }
                    self._cob_entries.append(entry)
                    # Derive country from BU code prefix (e.g. EE001 → EE, GH001 → GH)
                    country_prefix = re.match(r"([A-Za-z]{2})", buc)
                    if country_prefix:
                        cc_country = country_prefix.group(1).upper()
                        self._cob_by_country.setdefault(cc_country, []).append(entry)

        logger.debug("Loaded %d chart-of-books entries", len(self._cob_entries))

    def resolve_buyer(
        self,
        *,
        country_code: str = "",
        address_text: str = "",
        company_name: str = "",
        business_unit_name: str = "",
    ) -> Buyer:
        """Resolve buyer codes from country and address evidence.

        Returns a Buyer model. Fields are empty strings when not matched.
        """
        candidates: List[dict] = []

        if country_code:
            cc = country_code.upper().strip()
            candidates = self._cob_by_country.get(cc, [])

        # If only one BU for this country, use it directly
        if len(candidates) == 1:
            e = candidates[0]
            return Buyer(
                company_code=e["company_code"],
                business_unit_code=e["business_unit_code"],
                location_code=e["location_code"],
            )

        # Multiple candidates — try address / BU-name matching
        if candidates and address_text:
            addr_norm = _normalise(address_text)
            for e in candidates:
                e_addr = _normalise(e.get("invoice_to_address", ""))
                e_bun = _normalise(e.get("business_unit_name", ""))
                if e_addr and (e_addr in addr_norm or addr_norm in e_addr):
                    return Buyer(
                        company_code=e["company_code"],
                        business_unit_code=e["business_unit_code"],
                        location_code=e["location_code"],
                    )
                if business_unit_name and e_bun:
                    if _normalise(business_unit_name) in e_bun or e_bun in _normalise(business_unit_name):
                        return Buyer(
                            company_code=e["company_code"],
                            business_unit_code=e["business_unit_code"],
                            location_code=e["location_code"],
                        )

        # If no country match but there's only one company, return company code at least
        if not candidates and self._cob_entries:
            # Try keyword search across all entries
            if address_text:
                addr_norm = _normalise(address_text)
                for e in self._cob_entries:
                    e_addr = _normalise(e.get("invoice_to_address", ""))
                    if e_addr and (e_addr in addr_norm or addr_norm in e_addr):
                        return Buyer(
                            company_code=e["company_code"],
                            business_unit_code=e["business_unit_code"],
                            location_code=e["location_code"],
                        )
            # All entries share the same company_code in this dataset
            unique_companies = {e["company_code"] for e in self._cob_entries}
            if len(unique_companies) == 1:
                return Buyer(
                    company_code=list(unique_companies)[0],
                    business_unit_code="",
                    location_code="",
                )

        return Buyer()


# ---------------------------------------------------------------------------
# Module-level singleton (lazy init)
# ---------------------------------------------------------------------------

_INDEX: Optional[MasterDataIndex] = None


def get_index(master_data_dir: Optional[Path] = None) -> MasterDataIndex:
    """Return the module-level MasterDataIndex, initialised on first call."""
    global _INDEX
    if _INDEX is None:
        _INDEX = MasterDataIndex(master_data_dir)
    return _INDEX


def reset_index() -> None:
    """Reset the module-level index (useful in tests)."""
    global _INDEX
    _INDEX = None


# ---------------------------------------------------------------------------
# Convenience wrappers (thin façade over MasterDataIndex)
# ---------------------------------------------------------------------------

def match_supplier(vat_id: str = "", iban: str = "", name: str = "", email: str = "") -> str:
    return get_index().match_supplier(vat_id=vat_id, iban=iban, name=name, email=email)


def match_tax_code(country: str, tax_type: str = "", rate: float = 0.0) -> str:
    return get_index().match_tax_code(country=country, tax_type=tax_type, rate=rate)


def match_payment_term(text: str = "", invoice_date: str = "", due_date: str = "") -> str:
    return get_index().match_payment_term(text=text, invoice_date=invoice_date, due_date=due_date)


def match_po(po_number: str) -> str:
    return get_index().match_po(po_number)


def resolve_buyer(
    country_code: str = "",
    address_text: str = "",
    company_name: str = "",
    business_unit_name: str = "",
) -> Buyer:
    return get_index().resolve_buyer(
        country_code=country_code,
        address_text=address_text,
        company_name=company_name,
        business_unit_name=business_unit_name,
    )


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _parse_date(s: str) -> date:
    """Parse ISO or common date formats."""
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date: {s!r}")
