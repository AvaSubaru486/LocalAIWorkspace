"""Compatibility entry point for the three-model package verifier."""
from __future__ import annotations

import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).with_name("verify_three_model_package.py")), run_name="__main__")
