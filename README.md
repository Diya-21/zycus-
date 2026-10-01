# Zycus AI/ML Graduate Assignment

## Overview

This project implements an AI-assisted document understanding and accounting pipeline for the Zycus assignment. It processes PDF documents, identifies whether each document is payable or non-payable, extracts structured accounting data, resolves values against master data, and validates the result against the provided ERP oracle before producing final schema-compliant JSON. The system is designed to handle real invoice-like documents while keeping the logic conservative and auditable.

The implementation is not a generic no-code workflow: it is a clearly structured Python pipeline that moves through document ingestion, multimodal extraction, normalization, validation, master-data matching, accounting reconciliation, and ERP check. The goal is to produce usable accounting payloads without inventing values or forcing mismatched records to pass. This approach keeps the pipeline robust for valid documents while making failure modes explicit when the provider or document data is insufficient.

## Problem Statement

The assignment requires a full accounting-document understanding flow:

PDF documents → document understanding → payable/non-payable classification → accounting extraction → master-data matching → reconciliation → ERP validation → schema-compliant JSON

In practical terms, the system receives invoice-style and related PDF documents, decides whether they are payable, extracts relevant accounting fields such as invoice numbers, dates, totals, taxes, supplier information, and line items, matches those values to the assignment master data, and then validates the final payload using the fixed ERP logic provided by the project. All outputs are expected to conform to the assignment schema, and any extraction failure or provider-side error must be surfaced clearly rather than silently replaced with a guessed value.

## Architecture

```text
PDF Documents
    ↓
Document Ingestion
    ↓
Gemini Multimodal Extraction
    ↓
Normalization & Pydantic Validation
    ↓
Master Data Matching
    ↓
Accounting Reconciliation
    ↓
ERP Oracle Validation
    ↓
Final JSON Output
```

The pipeline is intentionally staged so that each responsibility remains separate: extraction is kept distinct from normalization, normalization is kept distinct from master-data matching, and ERP validation remains the final accounting gate. This separation makes the system easier to debug and ensures that errors such as quota exhaustion or temporary provider unavailability are treated explicitly instead of being hidden behind incorrect output.

## Key Features

The current implementation includes the following features:

- PDF ingestion
- Multimodal Gemini extraction
- Payable/non-payable classification
- Multiple payable records per document
- Structured accounting extraction
- Master-data matching
- Pydantic validation and normalization
- ERP reconciliation
- Fixed ERP oracle validation
- Gemini 429/503 error handling

## Technology Stack

The project uses the following technologies:

- Python 3.x
- PyMuPDF
- Pillow
- Google GenAI SDK
- Gemini
- Pydantic
- python-dotenv
- pytest
- JSON

## Repository Structure

The repository contains the following important structure:

```text
candidate_kit/
├── AUTODRAFT_SCHEMA.md
├── erp.py
├── example_check.py
├── sample_autodraft.json
├── master_data/
└── documents/

src/
├── models.py
├── ingestion.py
├── matcher.py
├── extractor.py
├── reconciler.py
└── pipeline.py

tests/
run.py
DESIGN.md
README.md
requirements.txt
.gitignore
.env.example
```

The assignment data and ERP oracle live under `candidate_kit/`, while the processing logic lives under `src/`. Test coverage is stored in `tests/`, and the CLI entry point for executing the pipeline is `run.py`.

## Setup

Create a virtual environment and install the project dependencies:

```bash
python -m venv .venv
```

On Windows:

```powershell
.venv\Scripts\activate
```

Then install the required packages:

```bash
pip install -r requirements.txt
```

The pipeline uses a local `.env` file to provide the Gemini API key. Create a `.env` file in the project root with the following content:

```dotenv
GEMINI_API_KEY=your_api_key_here
```

Do not commit a real key. The repository provides `.env.example` as a safe template, and `.gitignore` excludes local secret files from version control.

## Running the Pipeline

To run the assignment pipeline on the provided assignment documents, use:

```bash
python run.py --input candidate_kit/documents --output output
```

This command reads each PDF in the supplied document directory, runs the extraction and validation flow, and writes the resulting JSON outputs into the `output/` directory. Each output file contains the result for one document and follows the assignment output contract for payable records, declined documents, or explicit extraction failures.

## Output

The pipeline produces schema-compliant JSON output based on the assignment specification. For each processed document, the output can contain:

- payable records
- declined records for non-payable or non-bookable documents
- explicit extraction-failure records when the document could not be processed reliably

The output is designed to work with the fixed ERP validation logic and the assignment schema, rather than forcing values that should remain empty or invalid.

## Master Data and ERP Validation

The assignment includes a master-data layer under `candidate_kit/master_data/`, which is used to resolve controlled values such as suppliers, tax references, payment terms, PO information, and buyer data. This matching step is important because it connects the extracted document data to the canonical master records that the ERP process expects.

The file `candidate_kit/erp.py` is treated as the fixed ERP oracle for this assignment. It was not modified and remains the authoritative validation mechanism for reconciliation. The pipeline validates the extracted payable information against this oracle before treating it as a successful accounting result.

## Testing

The repository includes project-level validation using pytest:

```bash
pytest -q
```

It also includes a compile-time validation step for the Python modules:

```bash
python -m compileall src tests
```

These checks validate the model and pipeline behavior and confirm that the source files are syntactically valid. They do not replace end-to-end business validation, but they provide a straightforward confidence check for the Python implementation.

## Error Handling

The system includes explicit handling for the external provider failures that arise during Gemini calls:

- HTTP 429 / quota exhaustion is treated as a controlled extraction failure without repeated retries.
- HTTP 503 / temporary unavailability is retried with bounded exponential backoff.
- The pipeline does not fabricate accounting values in order to force ERP reconciliation.
- Extraction failures are surfaced explicitly, preserving the reason for the failure within the output data.

This keeps the application honest: if the extraction or provider layer fails, the file is marked as failed instead of being silently converted into a misleading accounting payload.

## Design Decisions

Multimodal Gemini extraction was selected because invoice and accounting documents often contain complex layouts, tables, multi-page structures, and textual/visual evidence that cannot be reliably recovered from simple regex extraction alone. A multimodal model helps read the PDF and associated document visuals more accurately than a purely text-only heuristic parser.

At the same time, the implementation remains conservative. The LLM is used only for extraction, while deterministic validation, normalization, master-data matching, and ERP reconciliation remain separate stages. This ensures the system still benefits from LLM flexibility without losing auditability or allowing bad accounting data to pass through.

## Validation and Limitations

INV-31 was used as a reconciliation regression case during development. Specifically, a simulated Gemini-style extraction payload constructed from observed document evidence could be normalized and reconciled by the ERP oracle in a controlled test. Because the final verification steps were performed using a simulated/extracted payload (not a fresh live Gemini call), the repository does not claim a recent live Gemini verification of INV-31 after the final extractor changes — the Gemini quota was exhausted during development and a live re-run was not possible.

Broader bulk testing also showed that Gemini free-tier usage limits can trigger provider-side quota exhaustion during large runs. Those cases are treated as controlled extraction failures rather than as silent record fabrication. The repository does not claim that all documents in a large dataset were successfully processed in every run, and it does not invent success rates, accuracy numbers, or performance metrics that were not measured.

## Submission Notes

For submission cleanliness, the repository keeps local runtime secrets out of version control. The `.env` file is local-only and contains the actual API key, while `.env.example` provides a safe placeholder that can be shared publicly. The project also uses `.gitignore` to exclude secrets and generated output files from Git.

The ERP oracle and schema file were not modified, and the actual assignment implementation remains in the repository. This README reflects the implemented system accurately and avoids overclaiming behavior that has not been verified.

