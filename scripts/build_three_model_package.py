"""Build an offline ZIP containing exactly the three configured chat models."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BUFFER = 4 * 1024 * 1024
EXPECTED_IDS = {"qwen3-8b", "gemma3-27b-fast", "llama3-70b-q4km-cpu"}
MODEL_FALLBACKS = {
    "gemma3-27b-fast": [ROOT / "build/device-compatibility-20261024/isolated-app/data/models/gemma3-27b-fast"],
    "llama3-70b-q4km-cpu": [ROOT / "build/quality-acceptance-20261026/weights-q4km"],
}
TOP_FILES = (
    "LocalAIWorkspace.exe", "Start-LocalAI.cmd", "Check-Installation.cmd",
    "README.md", "requirements.txt", "requirements.lock.txt",
)
TREES = ("app", "config", "scripts", "launcher", "resources", "docs", "runtime")
MODEL_MANIFEST_PATH = "data/MODEL-MANIFEST.json"
RELEASE_MANIFEST_PATH = "data/INCLUDED-MODELS-70B-27B-8B.json"
PROJECTOR_SHA256 = "debe2545831a2566dcec81992b9aa8fa8381c5a0a3588c0b59a41cc9c610cfe7"


def components(models):
    """Allow three text weights and one exact component of the 27B model."""
    result = []
    for mid, model in models.items():
        allowed = [model["entry"]]
        if mid == "gemma3-27b-fast":
            if model.get("vision_entry") != "mmproj-F16.gguf":
                raise ValueError("The 27B matching projector must be configured")
            allowed.append(model["vision_entry"])
        elif model.get("vision_entry"):
            raise ValueError("Unexpected vision component for " + mid)
        specs = model.get("files", [])
        if len(specs) != len(allowed) or {s.get("path") for s in specs} != set(allowed):
            raise ValueError("Unexpected or missing model components for " + mid)
        for name in allowed:
            spec = next(s for s in specs if s["path"] == name)
            if name != model["entry"] and (spec.get("sha256") != PROJECTOR_SHA256 or spec.get("size") != 857739392):
                raise ValueError("27B projector identity mismatch")
            relative = Path(model["path"], name).as_posix()
            if not safe_relative(relative):
                raise ValueError("Unsafe component path")
            key = mid if name == model["entry"] else mid + ":vision"
            result.append((mid, key, relative, spec))
    return result


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(BUFFER):
            hasher.update(block)
    return hasher.hexdigest()


def load_models():
    config = json.loads((ROOT / "config/models.json").read_text("utf-8"))
    models = config.get("models")
    if not isinstance(models, list) or {m.get("id") for m in models if isinstance(m, dict)} != EXPECTED_IDS:
        raise ValueError("config/models.json must define exactly the three release models")
    return {m["id"]: m for m in models}


def parse_sources(values):
    result = {}
    for value in values:
        if "=" not in value:
            raise ValueError("--model-source format is MODEL_ID=PATH")
        model_id, path = value.split("=", 1)
        if model_id not in EXPECTED_IDS or model_id in result:
            raise ValueError("Unknown or duplicated --model-source id: " + model_id)
        result[model_id] = Path(path).expanduser().resolve()
    return result


def source_for(model_id, model, overrides):
    filename = model["entry"]
    if model_id in overrides:
        path = overrides[model_id]
        if path.is_dir():
            path = path / filename
        return path
    default = ROOT / model["path"] / filename
    if default.is_file():
        return default
    for folder in MODEL_FALLBACKS.get(model_id, []):
        candidate = folder / filename
        if candidate.is_file():
            return candidate
    return default


def safe_relative(name):
    path = PurePosixPath(name)
    return (not path.is_absolute() and ".." not in path.parts and ":" not in name
            and "\\" not in name)


def make_release_manifests(models, weights, report_path):
    manifest = [{"File": relative, "Bytes": weights[key]["bytes"], "Sha256": weights[key]["sha256"]}
                for mid, key, relative, spec in components(models)]
    release_models = []
    for model_id in ("qwen3-8b", "gemma3-27b-fast", "llama3-70b-q4km-cpu"):
        model = models[model_id]
        weight = weights[model_id]
        relative = Path(model["path"], model["entry"]).as_posix()
        release_models.append({
            "id": model_id, "name": model["name"], "path": model["path"],
            "entry": model["entry"], "bytes": weight["bytes"], "sha256": weight["sha256"],
            "quantization": model.get("quantization"), "license": model.get("license"),
            "runtime_profile": model.get("runtime_profile"),
            "resource_profile": model.get("resource_profile"),
            "vision_entry": model.get("vision_entry"),
            "vision_runtime_profile": model.get("vision_runtime_profile"),
        })
    release = {
        "release": "LocalAIWorkspace three-model portable release",
        "built_at_local": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "default_model": "qwen3-8b",
        "models": release_models,
        "speed_targets_tokens_per_second": {"gemma3-27b-fast": 4.0, "llama3-70b-q4km-cpu": 0.5},
        "speed_acceptance": {
            "gemma3-27b-fast": "Compact IQ1_S weights: target 4 token/s; not accepted. Previous quantization results do not apply.",
            "llama3-70b-q4km-cpu": "Compact IQ1_S weights: target 0.5 token/s; not accepted. Size reduction does not establish speed or capability.",
            "hardware_independent_guarantee": False,
        },
        "capability_retention": "Not measured by speed tests; no percentage claim is made.",
        "auxiliary_model_bundled": False,
        "vision_model_bundled": False,
        "vision_projector_bundled": True,
        "vision_chat_model": "gemma3-27b-fast",
        "vision_scope": "The existing 27B uses its pinned matching projector for image and sampled video frames; this is not a fourth chat model or a sustained-speed acceptance claim.",
        "chat_content_in_runlog_zip": False,
        "source_report": Path(report_path).name if report_path else None,
    }
    return (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"), \
           (json.dumps(release, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "build/LocalAIWorkspace-win-x64-8B-27B-70B-20261027.zip")
    parser.add_argument("--exe", type=Path, help="Optional staged EXE to include in the archive")
    parser.add_argument("--model-source", action="append", default=[], metavar="MODEL_ID=PATH")
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--skip-selfcheck", action="store_true", help="Use only after an equivalent clean package preflight")
    args = parser.parse_args()

    output = args.output.expanduser().resolve()
    executable = args.exe.expanduser().resolve() if args.exe else ROOT / "LocalAIWorkspace.exe"
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_suffix(".zip.part")
    checksum_path = output.with_suffix(".zip.sha256")
    report_path = output.with_suffix(".build-report.json")
    if any(path.exists() for path in (output, partial, checksum_path, report_path)):
        raise FileExistsError("Release output already exists; choose a new filename")

    models = load_models()
    overrides = parse_sources(args.model_source)
    sources = {}
    weights = {}
    component_list = components(models)
    for model_id, key, relative, spec in component_list:
        model = models[model_id]
        if not isinstance(spec, dict) or not isinstance(spec.get("sha256"), str):
            raise ValueError("Missing pinned size/SHA256 for " + model_id)
        main_source = source_for(model_id, model, overrides)
        source = main_source if key == model_id else main_source.parent / spec["path"]
        if not source.is_file():
            raise FileNotFoundError("Missing model file for " + model_id + ": " + str(source))
        if source.stat().st_size != spec["size"]:
            raise ValueError("Model byte count mismatch for " + model_id)
        actual = digest(source)
        if actual.lower() != spec["sha256"].lower():
            raise ValueError("Model SHA256 mismatch for " + model_id)
        sources[key] = source
        weights[key] = {"bytes": spec["size"], "sha256": actual}

    for name in TOP_FILES:
        source = executable if name == "LocalAIWorkspace.exe" else ROOT / name
        if not source.is_file():
            raise FileNotFoundError("Missing release file: " + name)
    if not (ROOT / "data/knowledge/archives/wikipedia_en_100_mini_2026-01.zim").is_file():
        raise FileNotFoundError("Missing bundled offline knowledge archive")

    if not args.skip_selfcheck:
        sys.path.insert(0, str(ROOT))
        from app.config import Settings
        from app.selfcheck import check
        report = check(Settings(ROOT), full=True)
        if not report["ok"]:
            raise RuntimeError("Full self-check failed; refusing to build package")

    model_manifest, release_manifest = make_release_manifests(models, weights, args.evidence)
    replacement_blobs = {MODEL_MANIFEST_PATH: model_manifest, RELEASE_MANIFEST_PATH: release_manifest,
                         "START-HERE-70B-27B-8B.zh-CN.txt": (ROOT / "docs/LOW-MEMORY.zh-CN.md").read_text("utf-8").encode("utf-8-sig")}

    total_bytes = sum(path.stat().st_size for path in sources.values())
    for name in TOP_FILES:
        source = executable if name == "LocalAIWorkspace.exe" else ROOT / name
        total_bytes += source.stat().st_size
    for tree in TREES:
        for path in (ROOT / tree).rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts and not path.name.endswith((".pyc", ".part", ".tmp")):
                relative = path.relative_to(ROOT).as_posix()
                if relative.startswith("runtime/python/Scripts/"):
                    continue
                total_bytes += path.stat().st_size
    total_bytes += (ROOT / "data/knowledge/archives/wikipedia_en_100_mini_2026-01.zim").stat().st_size
    total_bytes += sum(len(value) for value in replacement_blobs.values())
    if shutil.disk_usage(output.parent).free < total_bytes + 2 * 1024**3:
        raise OSError("Not enough output disk space for archive and 2 GiB safety margin")

    report = {"status": "RUNNING", "output": output.name, "expected_models": weights,
            "model_sources": {key: value.name for key, value in sources.items()},
            "exe_source": executable.name,
              "expected_uncompressed_bytes": total_bytes, "checks": {}}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
    seen = set()
    written_model_hashes = {}
    last_progress = time.monotonic()

    def write_path(archive, relative, source):
        nonlocal last_progress
        if not safe_relative(relative) or relative in seen:
            raise ValueError("Unsafe or duplicate archive entry: " + relative)
        seen.add(relative)
        compress = zipfile.ZIP_STORED if Path(relative).suffix.lower() in {".gguf", ".safetensors", ".zim", ".zip"} else zipfile.ZIP_DEFLATED
        archive.write(source, relative, compress_type=compress, compresslevel=2 if compress == zipfile.ZIP_DEFLATED else None)
        if time.monotonic() - last_progress > 10:
            report["phase"] = "writing"
            report["archive_entries_written"] = len(seen)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
            last_progress = time.monotonic()

    try:
        with zipfile.ZipFile(partial, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=2, allowZip64=True) as archive:
            for name in TOP_FILES:
                source = executable if name == "LocalAIWorkspace.exe" else ROOT / name
                write_path(archive, name, source)
            for tree in TREES:
                for path in sorted((ROOT / tree).rglob("*")):
                    if not path.is_file() or "__pycache__" in path.parts or path.name.endswith((".pyc", ".part", ".tmp")):
                        continue
                    relative = path.relative_to(ROOT).as_posix()
                    if relative.startswith("runtime/python/Scripts/"):
                        continue
                    if relative in replacement_blobs:
                        continue
                    write_path(archive, relative, path)
            for model_id, key, relative, spec in component_list:
                write_path(archive, relative, sources[key])
                written_model_hashes[key] = digest(sources[key])
            zim_relative = "data/knowledge/archives/wikipedia_en_100_mini_2026-01.zim"
            write_path(archive, zim_relative, ROOT / zim_relative)
            for relative, blob in replacement_blobs.items():
                if not safe_relative(relative) or relative in seen:
                    raise ValueError("Unsafe or duplicate generated entry: " + relative)
                seen.add(relative)
                archive.writestr(relative, blob)

        if written_model_hashes != {key: item["sha256"] for key, item in weights.items()}:
            raise ValueError("Model source changed during archive creation")
        with zipfile.ZipFile(partial, "r") as archive:
            names = [item.filename for item in archive.infolist()]
            if len(names) != len(set(names)):
                raise ValueError("Archive contains duplicate entries")
            model_paths = {relative for mid, key, relative, spec in component_list}
            actual_model_paths = {name for name in names if name.startswith("data/models/") and name.lower().endswith(".gguf")}
            if actual_model_paths != model_paths:
                raise ValueError("Archive does not contain exactly three chat weights and their pinned projector: " + str(actual_model_paths))
            if any(name.startswith("data/auxiliary/") for name in names):
                raise ValueError("Auxiliary model files must not be bundled")
            for model_id, key, name, spec in component_list:
                info = archive.getinfo(name)
                if info.file_size != weights[key]["bytes"]:
                    raise ValueError("Archive model size mismatch: " + model_id)
                hasher = hashlib.sha256()
                with archive.open(info) as stream:
                    while block := stream.read(BUFFER):
                        hasher.update(block)
                if hasher.hexdigest() != weights[key]["sha256"]:
                    raise ValueError("Archive model hash mismatch: " + model_id)
            # Reading every member verifies each ZIP CRC before publication.
            for index, info in enumerate(archive.infolist(), 1):
                with archive.open(info) as stream:
                    while stream.read(BUFFER):
                        pass
                if index % 2000 == 0:
                    report["phase"] = "crc_verification"
                    report["verified_entries"] = index
                    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")

        package_hash = digest(partial)
        size = partial.stat().st_size
        partial.replace(output)
        checksum_path.write_text(package_hash + "  " + output.name + "\n", "utf-8")
        report.update(status="COMPLETE", phase="complete", archive_bytes=size, sha256=package_hash,
                      archive_entries=len(seen), verified_model_sha256=written_model_hashes,
                      checks={"exactly_three_models": True, "no_auxiliary_model": True,
                              "matching_27b_projector": True,
                              "model_sha256": True, "all_entries_crc": True, "no_duplicate_entries": True})
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
        print(json.dumps({"status": report["status"], "output": output.name, "archive_bytes": size,
                          "sha256": package_hash, "report": report_path.name}, ensure_ascii=False, indent=2), flush=True)
    except BaseException as exc:
        report.update(status="FAILED", error=f"{type(exc).__name__}: {exc}", archive_partial=partial.name)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
        raise


if __name__ == "__main__":
    main()
