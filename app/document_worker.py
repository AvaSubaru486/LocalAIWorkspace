import json
import sys
from pathlib import Path

from app.attachments import extract_document

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        print(json.dumps(extract_document(Path(sys.argv[1]), int(sys.argv[2])), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
