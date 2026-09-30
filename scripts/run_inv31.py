import sys, os
sys.path.append('.')
from pathlib import Path
from src.pipeline import process_file
import logging
logging.basicConfig(level=logging.INFO, format='%(message)s')

os.makedirs('output_inv31_run3', exist_ok=True)
print(process_file(Path('candidate_kit/documents/INV-31.pdf'), Path('output_inv31_run3')))
