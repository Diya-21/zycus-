# Design Report

## Overview

This project is a document-understanding and accounting-validation pipeline for the Zycus assignment. It begins with a PDF document, determines whether it is a payable record or not, extracts the relevant accounting data, matches values against the supplied master data, and validates the result against the fixed ERP logic in `candidate_kit/erp.py`. The system is intentionally conservative: it prefers explicit evidence over inference, leaves unsupported fields empty, and reports uncertain or failed cases instead of forcing a match.

The design is not a one-size-fits-all rule engine. It recognizes that accounting documents vary significantly in structure and intent. Some are legitimate invoices, some are credit memos, some are reminders or delivery documents, and some are multi-page invoices with taxes, discounts, and charges spread across header and lines. The pipeline therefore keeps a clear separation between ingestion, multimodal extraction, schema validation, normalization, master-data matching, and ERP reconciliation.

## A. What was understood about the assignment that was not obvious at the start

The first major change in understanding was that these documents are not simply “PDF forms” with a single total. They are accounting records with a specific internal contract. A document can be payable or not, a single PDF may contain multiple payable records, and the ERP expects the raw accounting components rather than only the printed gross figure.

### Document types and payable vs non-payable distinction

The assignment is built around a distinction between bookable payable records and non-bookable documents. A standard invoice is payable when the document represents an amount owed to a supplier. A credit memo, by contrast, is still a payable/receivable-type accounting object but expressed as a credit, and it is handled with the same schema, only with its own values. A reminder, a delivery note, a purchase order, or any non-invoice document may be valid document content but should not be treated as a payable record. That decision is not based on a single field; it depends on the document type, context, and whether the content is an actual accounting obligation.

### Multiple payable records and document-level composition

A single file can contain more than one payable item, and the pipeline is designed to support this by processing each extracted payable independently before assembling the final per-file output. This matters for invoice batches, multi-document PDFs, and edge cases where one file includes several bookable records rather than a single invoice. A strict, record-by-record interpretation is therefore more correct than collapsing the file into a single guessed payable.

### Master-data references

The project data includes master files for suppliers, tax types, payment terms, PO references, and buyer/GL structures. These are not decorative metadata. They establish the canonical IDs that the ERP expects. A document may print a supplier name and a VAT ID, but the ERP contract also wants the matching supplier code from master data. The same principle applies to PO references, payment term codes, buyer codes, and tax type codes. The pipeline therefore distinguishes between raw printed values and resolved master-data identifiers, keeping the raw value in place while populating the matched code only when there is a real master-data match.

### Line vs header taxes, discounts, and charges

One of the most important lessons from the assignment is that the ERP recomputes gross from the raw structure, not from the printed total alone. Taxes and charges can appear at the header or at the line level. Line-level taxes are tied to a specific line’s base; header taxes apply to the overall payable structure. Similarly, invoice discounts can exist at the line or header level, and line charges or freight items may appear as standalone amounts. The raw structure matters because the ERP validates against the arithmetic of quantities, unit prices, discounts, and taxes, not against a single total printed in the document.

### Why printed gross cannot replace ERP components

The schema explicitly requires raw components: quantity, unit price, discounts, taxes, charges, and the actual accounting description. A document may print a gross amount that looks reasonable, but that number alone does not define how the ERP should recompute the payable. Three different structures can produce the same printed gross, yet the ERP will only accept the correct underlying arithmetic. If a value is missing or misplaced, the system must not replace it with a guessed total. This is the central accounting principle in the assignment: a printed gross is evidence of what the document says, not a substitute for the underlying ERP inputs.

### Why document evidence matters

The assignment explicitly requires that every accounting value be grounded in evidence in the PDF. This is particularly important when the document contains a total but not the line decomposition, or when a charge is printed but not clearly attributable to a specific component. The correct behavior is not to “complete the numbers” from assumptions. The system must leave unsupported fields blank, treat them as unknown, and surface the uncertainty clearly.

## B. What the system does when it sees an unfamiliar document

The system is designed to behave conservatively when it encounters a document it does not understand well.

### Document ingestion

The first stage is document ingestion. In `src/pipeline.py`, the pipeline loads the PDF using the project’s ingestion layer, creating an object that preserves the file path and page-level content. This stage checks whether the file is usable before any extraction work begins. If the PDF is unreadable or blank, it is declined explicitly rather than guessed into a payable.

### Multimodal extraction

The extraction stage is intentionally multimodal. The code uses the document text and rendered page images, and the project is designed to call Gemini for multimodal invoice understanding. The extractor is not a naive regex-only parser; it is a structure-aware extraction step that looks for invoice-like content, tax references, headers, line items, totals, and other accounting fields. The model is expected to return JSON-like structured output that respects the assignment schema.

### Structured schema validation

Once the model returns structured content, the next stage validates the output against the assignment contract in `candidate_kit/AUTODRAFT_SCHEMA.md`. The schema defines the expected fields and hierarchy for a payable: invoice number, dates, supplier, buyer, payment terms, PO, totals, taxes, and line items. This is essential because it prevents malformed or half-structured output from being treated as valid accounting data. It also preserves a clear distinction between raw printed values and resolved master-data identifiers.

### Normalization

Normalization happens after extraction and before ERP validation. The design’s goal is to convert model-produced numbers into the assignment’s required string-based numeric format, while preserving the document-supported values precisely. Numeric values are converted to dot-decimal strings, while unsupported values remain empty rather than fabricated. The pipeline is careful around cases such as taxes, standalone amounts, and negative adjustments: a standalone amount may only be treated as a quantity/unit_price pair when the document clearly supports that interpretation, and tax-only values must remain tax values rather than being silently transformed into ordinary line prices.

### Master-data matching

The later stages resolve raw document values against the assignment master data. Supplier names and VAT IDs are matched to the supplier master; PO numbers match the PO master; payment terms match the payment term master; and tax references are matched to the tax master when possible. This is a deliberate design choice: preserve the human-readable document value, then resolve the authoritative code only when a meaningful match exists. If there is no match, the code remains empty rather than guessed.

### Uncertainty and missing fields

This is a core design principle. The pipeline does not silently invent data. If a document does not show a quantity, unit price, tax rate, or charge, the field stays empty. If a document is not clearly payable, it becomes a declined record. If a document is incomplete or inconsistent, the failure is surfaced instead of converted into a misleading accounting result. This is important because once a bad accounting value enters the ERP flow, the error becomes harder to audit.

### Controlled failure behavior

The implementation explicitly handles provider-side failures in a controlled way. When the upstream Gemini provider reports quota exhaustion (HTTP 429 / RESOURCE_EXHAUSTED), the extractor treats that as a hard failure and does not loop endlessly. For temporary unavailability (HTTP 503), the system uses bounded retry behavior. The project does not silently retry forever or hide the issue. This is critical in a real assignment because external AI providers can fail independently of the document quality.

### ERP validation

The final accounting gate is ERP validation, implemented in `src/reconciler.py` and the fixed Oracle in `candidate_kit/erp.py`. The reconciler computes the document’s printed gross and compares it with the ERP recomputation from the raw components. This final check is not a “force the total to match” step: it reports the delta and preserves the accounting structure. A mismatch means the structure is not yet supported by the document or not properly represented. This keeps the system honest.

## C. Was there a document that could not be solved the same way as the others?

Not all documents in this assignment follow the same pattern.

### Ordinary invoices

Ordinary invoices are the easiest class because they usually have a clear supplier, dates, invoice total, tax treatment, and line items. They can usually be processed with the same accounting logic: extract raw components, validate the structure, match the master data, and then compare against the ERP recomputation.

### Credit memos

Credit memos are conceptually similar to invoices but they represent credits rather than ordinary debits. They follow the same schema structure and should not be treated as a different model type, but the accounting values themselves are different. The important point is that the raw values still need to be supported by the document and expressed in the same ERP contract; there is no freedom to invent a different field layout just because the document is a credit memo.

### Delivery or non-payable documents

A delivery note or reminder may contain dates, supplier information, or even totals, but it is not necessarily a payable obligation. These should be declined as non-payable, not forced through the invoice path. The distinction matters because a document can be legitimate and structured without being a payable record.

### Complex multi-page invoices

Complex multi-page invoices present the hardest case. They may have a line item table on one page, tax details on another, charges or freight on the header, and subtotals spread across pages. A single extraction pass may succeed on some of these but fail on others because the structure is harder to assemble faithfully. The system therefore handles such documents conservatively: if the components are not confidently supported by the document, they remain incomplete or the document is flagged as failed rather than guessed.

### Honesty about the current implementation

The repository does not claim that every document in a large batch was successfully processed. The current system is designed around explicit failures, especially when the external Gemini provider hits quota or temporary service interruption. In practical terms, provider quota exhaustion is treated as a controlled failure, not as a silent success or a fabricated accounting value. The latest code changes were not re-verified through a fresh end-to-end Gemini run because the provider quota was exhausted, and the design remains truthful about that limitation.

## Conclusion

The real lesson of this assignment is that accounting documents are not just text blobs; they are structured legal and financial artifacts. The system must respect raw accounting components, preserve document evidence, use the ERP as the final authority, and fail clearly when evidence or provider access is insufficient. The project’s design is therefore conservative, auditable, and grounded in document support: it extracts what is visible, validates what is structurally correct, resolves the master-data references when possible, and refuses to fabricate accounting values when the evidence is missing.
