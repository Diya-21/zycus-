"""Entry point CLI to process candidate documents into autodraft JSON files.

Usage:
    python run.py --input candidate_kit/documents --output output
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path
from src.pipeline import process_file
from src.extractor import GEMINI_STATS

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def main():
    ap = argparse.ArgumentParser(description="Process PDFs into Autodraft JSON files")
    ap.add_argument("--input", required=True, help="Input directory containing PDFs")
    ap.add_argument("--output", required=True, help="Output directory for JSON files")
    args = ap.parse_args()

    input_dir = Path(args.input)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    pdfs = sorted([p for p in input_dir.iterdir() if p.suffix.lower() == ".pdf"])
    total = len(pdfs)
    logger.info("Found %d PDFs in %s", total, input_dir)

    stats = {"processed": 0, "declined": 0, "failed": 0, "payables": 0, "payable_records": 0, "erp_failures": 0, "gemini_calls": 0, "extraction_failures": 0}
    sample_outputs = []
    for idx, p in enumerate(pdfs, start=1):
        logger.info("[%d/%d] Processing %s", idx, total, p.name)
        summary = process_file(p, output_dir)
        status = summary.get("status")
        if status == "ok":
            logger.info("    Status: ok")
            stats["processed"] += 1
            stats["payable_records"] += summary.get("payable_records", 0)
            stats["erp_failures"] += summary.get("erp_failures", 0)
            # collect sample outputs
            if len(sample_outputs) < 3:
                sample_outputs.append(p.stem + ".json")
        elif status == "declined":
            logger.info("    Status: declined")
            stats["declined"] += 1
        else:
            logger.info("    Status: failed: %s", summary.get("errors"))
            stats["failed"] += 1
        if summary.get("gemini_called"):
            stats["gemini_calls"] += 1
            if status != "ok":
                stats["extraction_failures"] += 1

    logger.info("")
    logger.info("Summary:")
    logger.info("  Total documents: %d", total)
    logger.info("  Payable documents (processed): %d", stats["processed"])
    logger.info("  Payable records: %d", stats["payable_records"])
    logger.info("  Declined documents: %d", stats["declined"])
    logger.info("  Failed documents: %d", stats["failed"])
    logger.info("  ERP reconciliation failures: %d", stats["erp_failures"])
    logger.info("  Gemini calls: %d (module reports %d)", stats["gemini_calls"], GEMINI_STATS.get("calls", 0))
    logger.info("  Extraction failures (Gemini called but not ok): %d", stats["extraction_failures"])

    if sample_outputs:
        logger.info("")
        logger.info("Representative outputs:")
        for fn in sample_outputs:
            p = output_dir / fn
            try:
                with open(p, 'r', encoding='utf-8') as fh:
                    data = fh.read()
                logger.info("--- %s ---", fn)
                logger.info(data[:1000])
            except Exception:
                logger.info("Could not read %s", fn)


if __name__ == "__main__":
    main()
