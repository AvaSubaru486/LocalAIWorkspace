"""Run one short, serialized CPU chat through each bundled release model."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
import sys
import time
import zipfile

from app.config import Settings
from app.models import ModelCatalog, ModelRuntime
from app.runlog import METRIC_SCHEMA_VERSION, export_logs, record_generation


def main():
    os.environ["PATH"] = ""
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    settings = Settings()
    settings.values["context_length"] = 512
    settings.values["max_new_tokens"] = 4
    catalog = ModelCatalog(settings)
    expected = ("qwen3-8b", "gemma3-27b-fast", "llama3-70b-q4km-cpu")
    actual = tuple(model["id"] for model in catalog.entries() if model.get("bundled") and model.get("role") == "chat")
    if actual != expected:
        raise ValueError("Release model set mismatch: " + repr(actual))

    runtime = ModelRuntime(settings, catalog)
    rows = []
    try:
        for model_id in expected:
            model = catalog.get(model_id)
            started = time.monotonic()
            started_at = datetime.now(timezone.utc).isoformat()
            text = "".join(runtime.stream(model, [{"role": "user", "content": "Reply with one short greeting."}]))
            metrics = dict(runtime.last_metrics or {})
            assert metrics.get("tokens_per_second_source") == "native_backend", metrics
            assert metrics.get("tokens_per_second", 0) > 0, metrics
            if metrics.get("generated_tokens", 0) > 1:
                assert metrics.get("observed_decode_tokens", 0) > 0, metrics
                assert metrics["observed_decode_tokens_per_second"] == (
                    metrics["observed_decode_tokens"] / metrics["observed_decode_seconds"]), metrics
            assert record_generation(settings, catalog, runtime, model, status="complete",
                                     started_at=started_at, request_seconds=metrics["request_seconds"],
                                     metrics_override=metrics)
            process = getattr(runtime, "process", None)
            pid = process.pid if process is not None else None
            runtime.unload()
            reaped = process is None or process.poll() is not None
            row = {"model_id": model_id, "response_nonempty": bool(text.strip()), "metrics": metrics,
                   "elapsed_seconds": round(time.monotonic() - started, 3), "process_reaped": reaped}
            rows.append(row)
            if not row["response_nonempty"] or not reaped:
                raise RuntimeError("Release model smoke failed: " + json.dumps(row, ensure_ascii=False))
        exported = export_logs(settings, settings.data / "logs/three-model-metrics.zip")
        with zipfile.ZipFile(exported) as archive:
            logged = [json.loads(line) for line in archive.read("runs.jsonl").decode("utf-8").splitlines()]
            for row in rows:
                entry = next(item for item in reversed(logged) if item.get("model_id") == row["model_id"])
                assert entry["metric_schema_version"] == METRIC_SCHEMA_VERSION
                for key in ("tokens_per_second", "tokens_per_second_source",
                            "observed_decode_tokens", "observed_decode_seconds",
                            "observed_decode_tokens_per_second"):
                    assert entry["metrics"].get(key) == row["metrics"].get(key), key
        result = {"success": True, "models": rows, "offline": True, "serialized": True,
                  "precise_metrics_log_zip_verified": True, "metric_schema_version": METRIC_SCHEMA_VERSION}
        report = settings.data / "logs" / "three-model-smoke.json"
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", "utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    finally:
        runtime.unload()


if __name__ == "__main__":
    main()
