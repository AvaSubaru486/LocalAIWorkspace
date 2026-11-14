"""Verify a three-model portable ZIP, its weights, offline startup and log export."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PACKAGE = ROOT / "build/LocalAIWorkspace-win-x64-8B-27B-70B-20261027.zip"
EXPECTED_IDS = ("qwen3-8b", "gemma3-27b-fast", "llama3-70b-q4km-cpu")
BUFFER = 4 * 1024 * 1024


def digest_stream(stream):
    hasher = hashlib.sha256()
    while block := stream.read(BUFFER):
        hasher.update(block)
    return hasher.hexdigest()


def safe_name(name):
    path = PurePosixPath(name)
    return not path.is_absolute() and ".." not in path.parts and ":" not in name and "\\" not in name


def run(command, cwd, env, timeout, label):
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError(f"{label} failed ({result.returncode}):\n{result.stdout[-6000:]}\n{result.stderr[-6000:]}")
    return result.stdout + result.stderr


def materialize_hardlink_tree(archive, target, model_paths):
    target.mkdir(parents=True)
    generated = {"data/MODEL-MANIFEST.json", "data/INCLUDED-MODELS-70B-27B-8B.json",
                 "START-HERE-70B-27B-8B.zh-CN.txt"}
    for info in archive.infolist():
        name = info.filename
        out = (target / Path(*PurePosixPath(name).parts)).resolve()
        if not out.is_relative_to(target.resolve()):
            raise ValueError("Unsafe package path: " + name)
        out.parent.mkdir(parents=True, exist_ok=True)
        if name in generated:
            with archive.open(info) as source, out.open("xb") as sink:
                shutil.copyfileobj(source, sink, BUFFER)
            continue
        if name in model_paths:
            source = ROOT / Path(*PurePosixPath(name).parts)
            if not source.is_file():
                raise FileNotFoundError("Hard-link model source is missing: " + str(source))
            os.link(source, out)
        else:
            with archive.open(info) as reader, out.open("xb") as writer:
                shutil.copyfileobj(reader, writer, BUFFER)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, default=DEFAULT_PACKAGE)
    parser.add_argument("--exe", type=Path, help="Standalone EXE to compare with the archive")
    parser.add_argument("--smoke", action="store_true", help="Run clean-root self-check, offline model and EXE smoke tests")
    parser.add_argument("--smoke-root", type=Path)
    args = parser.parse_args()
    package = args.package.expanduser().resolve()
    if not package.is_file():
        raise FileNotFoundError(package)

    checksum_path = package.with_suffix(".zip.sha256")
    checksum_line = checksum_path.read_text("utf-8").strip().split()
    if not checksum_line or checksum_line[0].lower() != digest_stream(package.open("rb")):
        raise ValueError("ZIP SHA256 does not match its sidecar checksum")
    package_hash = checksum_line[0].lower()
    report = {"status": "RUNNING", "package": package.name, "zip_bytes": package.stat().st_size,
              "zip_sha256": package_hash, "checks": {}}

    model_hashes = {}
    model_paths = set()
    with zipfile.ZipFile(package, "r") as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)) or any(not safe_name(name) for name in names):
            raise ValueError("Unsafe or duplicate ZIP entries")
        if any(name.startswith(("data/sessions/", "data/logs/", "data/uploads/", "data/offload/")) for name in names):
            raise ValueError("Personal data appeared in the release archive")
        if any(name.startswith("data/auxiliary/") for name in names):
            raise ValueError("Unexpected auxiliary model files appeared in the release archive")

        config = json.loads(archive.read("config/models.json"))
        models = config.get("models")
        if not isinstance(models, list) or tuple(model.get("id") for model in models) != EXPECTED_IDS:
            raise ValueError("The archive manifest must contain exactly the ordered release model IDs")
        release = json.loads(archive.read("data/INCLUDED-MODELS-70B-27B-8B.json"))
        if tuple(model.get("id") for model in release.get("models", [])) != EXPECTED_IDS:
            raise ValueError("Included-model manifest does not match the release IDs")
        if release.get("auxiliary_model_bundled") is not False or release.get("vision_model_bundled") is not False:
            raise ValueError("The included-model manifest advertises an extra model")
        if release.get("vision_projector_bundled") is not True or release.get("vision_chat_model") != "gemma3-27b-fast":
            raise ValueError("The included-model manifest omits the 27B vision component")
        disk_manifest = json.loads(archive.read("data/MODEL-MANIFEST.json"))
        expected = {}
        for model in models:
            if not model.get("bundled") or model.get("role") != "chat":
                raise ValueError("Every release model must be a bundled chat model")
            allowed = [model["entry"]]
            if model["id"] == "gemma3-27b-fast":
                if model.get("vision_entry") != "mmproj-F16.gguf":
                    raise ValueError("The existing 27B must have the matching projector")
                allowed.append("mmproj-F16.gguf")
            elif model.get("vision_entry"):
                raise ValueError("Unexpected vision component")
            files = model.get("files", [])
            if len(files) != len(allowed) or {item.get("path") for item in files} != set(allowed):
                raise ValueError("Unexpected model file specifications")
            for filename in allowed:
                spec = next(item for item in files if item["path"] == filename)
                if filename != model["entry"] and (spec["size"] != 857739392 or spec["sha256"].lower() !=
                        "debe2545831a2566dcec81992b9aa8fa8381c5a0a3588c0b59a41cc9c610cfe7"):
                    raise ValueError("Projector is not the pinned matching 27B component")
                name = Path(model["path"], filename).as_posix()
                if not safe_name(name):
                    raise ValueError("Unsafe model file path")
                key = model["id"] if filename == model["entry"] else model["id"] + ":vision"
                expected[name] = (spec["size"], spec["sha256"].lower(), key)
                model_paths.add(name)
        if len(disk_manifest) != len(expected):
            raise ValueError("The installed model file manifest has extra or missing entries")
        for row in disk_manifest:
            name = row.get("File")
            if name not in expected or row.get("Bytes") != expected[name][0] or row.get("Sha256", "").lower() != expected[name][1]:
                raise ValueError("Installed model file manifest mismatch: " + str(name))
        actual_weights = {name for name in names if name.startswith("data/models/") and name.lower().endswith((".gguf", ".safetensors"))}
        if actual_weights != set(expected):
            raise ValueError("Archive contains model weights outside the three-model allowlist: " + str(actual_weights))

        exe_info = archive.getinfo("LocalAIWorkspace.exe")
        exe_hash = digest_stream(archive.open(exe_info))
        source_exe = args.exe.expanduser().resolve() if args.exe else ROOT / "LocalAIWorkspace.exe"
        if source_exe.is_file() and digest_stream(source_exe.open("rb")) != exe_hash:
            raise ValueError("Standalone EXE differs from the EXE embedded in the ZIP")

        for index, info in enumerate(infos, 1):
            hasher = hashlib.sha256() if info.filename in expected else None
            with archive.open(info) as stream:
                while block := stream.read(BUFFER):
                    if hasher:
                        hasher.update(block)
            if hasher:
                size, sha, model_id = expected[info.filename]
                if info.file_size != size or hasher.hexdigest() != sha:
                    raise ValueError("Model entry CRC/size/SHA256 check failed: " + model_id)
                model_hashes[model_id] = hasher.hexdigest()
            if index % 2000 == 0:
                report["phase"] = "crc_verification"
                report["crc_entries_read"] = index
                report_path = package.with_suffix(".verify-report.json")
                report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")

    checks = {"zip_sha256": True, "all_entry_crc": True, "exactly_three_chat_weights": True,
              "model_sha256": True, "no_auxiliary_or_extra_chat_weights": True, "matching_27b_projector": True,
              "no_duplicate_or_unsafe_paths": True, "exe_matches_package": True}
    if args.smoke:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        smoke_root = (args.smoke_root or ROOT / "build" / ("three-model-smoke-" + stamp)).expanduser().resolve()
        if smoke_root.exists():
            raise FileExistsError("Smoke root must be new: " + str(smoke_root))
        try:
            with zipfile.ZipFile(package, "r") as archive:
                materialize_hardlink_tree(archive, smoke_root, model_paths)
            python = smoke_root / "runtime/python/python.exe"
            env = os.environ.copy()
            for key in ("PATH", "PYTHONHOME", "PYTHONPATH", "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
                env.pop(key, None)
            env.update(PATH="", PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONNOUSERSITE="1",
                       HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
            run([str(python), "-I", "-X", "utf8", "-m", "app.selfcheck", "--full"],
                smoke_root, env, 240, "Portable package self-check")
            smoke_report = json.loads((smoke_root / "data/logs/selfcheck.json").read_text("utf-8"))
            if not smoke_report.get("ok"):
                raise RuntimeError("Portable package self-check report is not successful")
            run([str(python), "-I", "-X", "utf8", "-m", "scripts.offline_smoke"],
                smoke_root, env, 900, "Offline Qwen history smoke")
            run([str(python), "-I", "-X", "utf8", "-m", "scripts.model_release_smoke"],
                smoke_root, env, 1800, "Serialized three-model inference smoke")
            run([str(python), "-I", "-X", "utf8", "-m", "scripts.vision_release_smoke",
                 "--output", str(smoke_root / "data/vision-release-smoke"), "--cases", "images,video,switch,cancel"],
                smoke_root, env, 2400, "Actual 27B image/video application smoke")
            log_smoke = (
                "from pathlib import Path; import tempfile,zipfile; from app.config import Settings; "
                "from app.runlog import export_logs; s=Settings(); "
                "d=tempfile.TemporaryDirectory(); p=Path(d.name)/'logs.zip'; export_logs(s,p); "
                "z=zipfile.ZipFile(p); names=set(z.namelist()); "
                "assert {'runtime-environment.json','runs.jsonl','runs.csv','README.txt'} <= names; "
                "assert b'prompt' not in z.read('README.txt').lower(); print('privacy-safe-log-zip-ok')"
            )
            run([str(python), "-I", "-X", "utf8", "-c", log_smoke],
                smoke_root, env, 120, "Privacy-safe run-log ZIP export")
            run([str(python), "-I", "-X", "utf8", "-m", "scripts.exe_smoke", str(smoke_root)],
                smoke_root, env, 180, "Actual packaged EXE window smoke")
            checks.update({"hardlink_materialized_clean_root": True, "full_selfcheck": True,
                           "offline_qwen_history_inference": True, "all_three_models_inferred": True,
                           "actual_27b_image_and_sampled_video_inference": True,
                           "privacy_safe_log_zip": True,
                           "actual_exe_window_and_cleanup": True})
            report["smoke_root"] = smoke_root.name
        except BaseException as exc:
            report.update(status="FAILED", error=f"{type(exc).__name__}: {exc}", smoke_root=smoke_root.name)
            package.with_suffix(".verify-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
            raise

    report.update(status="PASSED", checks=checks, model_sha256=model_hashes, exe_sha256=exe_hash,
                  archive_entries=len(names), finished_at_local=time.strftime("%Y-%m-%d %H:%M:%S %z"))
    report_path = package.with_suffix(".verify-report.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
