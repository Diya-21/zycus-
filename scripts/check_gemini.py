"""Simple debug helper to inspect Gemini environment and extractor stats."""
import os
import importlib.util
import json

print("GEMINI_API_KEY present:", bool(os.environ.get("GEMINI_API_KEY")))
print("google.generativeai importable:", bool(importlib.util.find_spec("google.generativeai")))
try:
    from src import extractor
    print("GEMINI_STATS:", json.dumps(extractor.GEMINI_STATS))
except Exception as e:
    print("Failed to import src.extractor:", e)

# Also check for a .env file
from pathlib import Path
env_path = Path.cwd() / ".env"
print(".env exists:", env_path.is_file())
if env_path.is_file():
    txt = env_path.read_text(encoding="utf-8")
    for ln in txt.splitlines():
        if ln.strip().startswith("GEMINI_API_KEY"):
            print(".env GEMINI_API_KEY line:", ln)
            break
