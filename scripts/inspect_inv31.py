from pathlib import Path
import fitz

PDF = Path('candidate_kit/documents/INV-31.pdf')
KEYS = [
    'Customer Charge',
    'Distribution System Improvement Charge',
    'Hudson Energy Charges - ADJUSTMENT',
    'Distribution Charges',
    'Energy Eff & Nonbypassable Trans',
    'Hudson Energy Charges - ENERGY CHARGE',
]

doc = fitz.open(PDF)
print('pages', doc.page_count)
for i, page in enumerate(doc, start=1):
    text = page.get_text('text')
    for k in KEYS:
        if k in text:
            print('\n---- PAGE', i, '----')
            print('KEY:', k)
            idx = text.find(k)
            start = max(0, idx - 200)
            end = min(len(text), idx + 400)
            snippet = text[start:end]
            # replace newlines for single-line context
            print(snippet)
            # attempt to find amounts in the snippet
            import re
            amounts = re.findall(r"[$€£]?\s*-?\d{1,3}(?:,\d{3})*(?:\.\d+)?", snippet)
            print('AMOUNTS_NEAR_KEY:', amounts)

# Also print full page 1 and 2 text for manual inspection
for i in range(1, min(6, doc.page_count)+1):
    print('\n==== FULL PAGE', i, '====')
    print(doc[i-1].get_text('text')[:2000])

print('\nDone')
