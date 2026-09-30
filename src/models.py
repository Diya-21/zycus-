"""src/models.py — Strict Pydantic models for the Autodraft Schema.

Conforms exactly to candidate_kit/AUTODRAFT_SCHEMA.md.
No invented fields, no removed required fields.
"""
from __future__ import annotations

from typing import List, Literal, Optional
from pydantic import BaseModel, Field, ConfigDict, field_validator


class TaxItem(BaseModel):
    """A quantifiable tax item at header or line level."""
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    tax_type: str = Field(default="", description="Tax category, e.g. VAT, IVA, SST, WHT")
    tax_name: str = Field(default="", description="Printed tax description / name")
    tax_rate: str = Field(default="", description="Tax rate percentage without %, dot-decimal")
    tax_amount: str = Field(default="", description="Explicit tax amount, may be negative for withholding")
    tax_type_code: str = Field(default="", description="Matched master-data tax code, or empty string")


class LineItem(BaseModel):
    """Raw components of an invoice line item."""
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    description: str = Field(default="", description="Description of product or service")
    item_type: str = Field(default="SERVICE", description="GOODS | SERVICE | FREIGHT | TAX")
    uom: str = Field(default="", description="Unit of measure, e.g. Hr, PC, KG")
    quantity: str = Field(default="", description="Quantity as dot-decimal string")
    unit_price: str = Field(default="", description="NET unit price (excludes tax) as dot-decimal string")
    total: str = Field(default="", description="Line extension as printed on the document")
    discount: str = Field(default="", description="Amount discount magnitude")
    discount_percentage: str = Field(default="", description="Percentage discount")
    tax_rate: str = Field(default="", description="Per-line tax rate (optional)")
    tax_amount: str = Field(default="", description="Per-line tax amount (optional)")
    taxes: List[TaxItem] = Field(default_factory=list, description="Explicit per-line tax breakdown")


class Supplier(BaseModel):
    """Supplier information and master-data resolution."""
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name: str = Field(default="", description="Supplier name as printed on document")
    supplier_id: str = Field(default="", description="Matched master-data supplier code or empty string")
    address: str = Field(default="", description="Supplier address as printed on document")
    vat_id: str = Field(default="", description="Supplier VAT / Tax ID as printed on document")


class Buyer(BaseModel):
    """Buyer tenant organisation resolved from master data."""
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    company_code: str = Field(default="", description="Tenant company code from chart_of_books.json")
    business_unit_code: str = Field(default="", description="Tenant business unit code from chart_of_books.json")
    location_code: str = Field(default="", description="Tenant location code from chart_of_books.json")


class Payable(BaseModel):
    """One bookable payable record consumed by the ERP."""
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    invoice_number: str = Field(default="", description="Document / Invoice number")
    invoice_date: str = Field(default="", description="ISO date YYYY-MM-DD")
    due_date: str = Field(default="", description="ISO date YYYY-MM-DD")
    invoice_type: str = Field(default="INVOICE", description="INVOICE | CREDIT_MEMO")
    currency: str = Field(default="", description="3-letter ISO currency code")

    supplier: Supplier = Field(default_factory=Supplier, description="Supplier details")
    buyer: Buyer = Field(default_factory=Buyer, description="Buyer tenant organisation details")
    payment_term_id: str = Field(default="", description="Matched master-data payment term code")
    po_number: str = Field(default="", description="PO number as printed on document (raw)")
    po_id: str = Field(default="", description="Matched master-data PO code or empty string")

    # Totals exactly as printed on the document
    gross_total: str = Field(default="", description="What the document says is owed")
    subtotal: str = Field(default="", description="Printed subtotal before taxes and charges")
    total_tax_amount: str = Field(default="", description="Declared total tax as printed")

    # Header-level charges & discount
    discount_amount: str = Field(default="", description="Header-level discount amount")
    freight_charges: str = Field(default="", description="Freight / shipping charges")
    insurance_charges: str = Field(default="", description="Insurance charges")
    extra_charges: str = Field(default="", description="Extra charges / fees")
    excise_duties: str = Field(default="", description="Excise duties")

    # Header-level taxes
    taxes: List[TaxItem] = Field(default_factory=list, description="Header-level taxes")

    # Line items: raw components
    line_items: List[LineItem] = Field(default_factory=list, description="Line item decomposition")


class DeclinedRecord(BaseModel):
    """Record for a document that is judged not to be a bookable payable."""
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    doc_type: str = Field(description="Document type classification, e.g. REMINDER, DELIVERY_NOTE, INTERNAL_FORM, ESTIMATE")
    reason: str = Field(description="Clear explanation of why this document is not a payable")


class AutodraftFile(BaseModel):
    """Per-file output container written to output/X.json."""
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    file: str = Field(description="Filename of the source PDF, e.g. X.pdf")
    payables: List[Payable] = Field(default_factory=list, description="0..N bookable payables")
    declined: List[DeclinedRecord] = Field(default_factory=list, description="Any documents determined not to be payables")
