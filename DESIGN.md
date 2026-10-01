# Design

## A. What I learned about the documents
- The dataset contains multiple document types: invoices, credit memos, reminders, delivery notes, and multi-page composites. Not every PDF is a bookable payable.
- A single PDF may contain multiple payable records; the pipeline treats each payable independently.
- Master-data fields (supplier codes, VAT IDs, payment terms, PO references) are authoritative for ERP reconciliation and must be matched deterministically when present.
- Line-level vs header-level accounting matters: taxes, discounts, and charges can appear either per-line or at the header, and ERP recomputation depends on the raw components (quantity, unit_price, line discounts, line taxes, header taxes/discounts, and supported charges).
- Printed gross totals are evidence but not a substitute for the ERP-required components; different internal structures can produce the same printed gross.

## B. What the system does for an unfamiliar document
Pipeline (concise):

PDF ingestion → payable classification → multimodal extraction → schema validation → normalization → master-data matching → ERP reconciliation → final JSON

Why this generalizes:
- The extractor uses document evidence (text + images) and a schema-driven JSON contract rather than filename heuristics.
- Normalization converts provider output into the ERP-safe string numeric form and preserves uncertainty (empty fields) rather than inventing values.
- Master-data matching is deterministic: human-readable values are preserved, canonical IDs are filled only on confident matches.
- Provider failures (quota, unavailability, malformed output) are surfaced explicitly as extraction failures rather than being silently classified as non-payable.

## C. A document/problem that could not be solved the same way
- INV-31 (verified case): the file contains standalone charges and tax rows where some accounting components were not visible to the original deterministic parser. During debugging the gap between the printed gross and the ERP recomputation was observed. The remediation was to (a) strengthen evidence-backed extraction/normalization so standalone amounts and tax rows are represented in the correct schema locations, and (b) ensure negative adjustments and header vs line taxes are preserved. This verification used a controlled/simulated extraction payload derived from the observed document evidence; a fresh live Gemini re-run was not available due to provider quota exhaustion.

Notes
- The design prioritizes auditability and conservatism: extract what is visible, normalize to the ERP contract, resolve master-data when confident, and surface failures instead of fabricating values.
- The `candidate_kit/erp.py` oracle is unchanged and remains the final authority for reconciliation checks.
