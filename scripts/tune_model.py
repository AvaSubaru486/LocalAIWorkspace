"""Run an explicit, local CPU llama-bench tune for one GGUF model."""

from __future__ import annotations

import argparse
import json

from app.config import Settings
from app.performance import tune_cpu
from app.workspace import Workspace


def main(argv=None):
    parser = argparse.ArgumentParser(description="Tune bundled llama.cpp CPU threads for a ready GGUF model")
    parser.add_argument("--model", help="manifest model ID; defaults to workspace default_model")
    parser.add_argument("--threads", help="optional comma-separated candidates, e.g. 2,4,6")
    parser.add_argument("--timeout", type=int, default=900, help="benchmark timeout in seconds")
    args = parser.parse_args(argv)
    settings = Settings()
    workspace = Workspace(settings)
    try:
        report = tune_cpu(workspace, args.model, user_initiated=True,
                          threads=args.threads, timeout=args.timeout)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    finally:
        workspace.close()


if __name__ == "__main__":
    raise SystemExit(main())
