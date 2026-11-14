"""Real short Qwen calibration in temporary data using only bundled assets."""
import json
import os
import tempfile
from pathlib import Path

from app.config import ROOT, Settings
from app.model_tools import calibrate
from app.workspace import Workspace


def main():
    os.environ.update(PATH="", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    with tempfile.TemporaryDirectory(prefix="localai-calibration-") as folder:
        settings = Settings(Path(folder))
        settings.root = ROOT
        settings.values.update(gpu_layers=0, context_length=2048, auxiliary_enabled=False)
        workspace = Workspace(settings)
        try:
            report = calibrate(workspace, "qwen3-8b", user_initiated=True)
            assert report["metrics"]["generated_tokens"] > 1 and report["metrics"]["tokens_per_second"] > 0
            assert (settings.data / "model-benchmarks.json").is_file()
            assert not workspace.store.sessions() and not workspace.runtime.lock.locked()
            assert workspace.runtime.process is None
            print(json.dumps(report, ensure_ascii=False, indent=2))
        finally:
            workspace.close()


if __name__ == "__main__":
    main()
