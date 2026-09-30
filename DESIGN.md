# Design Report

This document summarises the design decisions for the assignment implementation.

Overview
- Conservative extractor: deterministic PyMuPDF text extraction + regex heuristics.
- Deterministic master-data matching via `src/matcher.py`.
- ERP validation using the supplied `candidate_kit/erp.py`.

LLM vs Deterministic
- No LLM calls are used in this implementation. The extraction is conservative
  and intentionally avoids inventing values. In a production setting the
  `src/extractor.py` can be replaced with a Gemini multimodal prompt to
  improve layout understanding.

Handling Uncertainty
- If evidence for a field is not present in the document the pipeline leaves
  the field empty rather than guessing, following the assignment rules.
- The reconciler reports mismatches between printed gross and ERP-booked gross
  but does not alter source values to force a match.

Limitations
- The extractor uses simple regexes and will miss complex tabular layouts.
- No OCR post-processing is applied beyond PyMuPDF's text extraction.
