import argparse
import importlib
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from .config import Settings
from .models import ModelCatalog, sha256
from . import cpu_compatibility
from .processes import HIDDEN
from .project_tools import git_binary
from .knowledge import BUNDLED_WIKIPEDIA, BUNDLED_WIKIPEDIA_ARTICLES, BUNDLED_WIKIPEDIA_BYTES, BUNDLED_WIKIPEDIA_SHA256

# Keep the bundled runtime independent of the host environment.  PySide6's
# package bootstrap indexes PATH directly, so an absent variable must behave
# like an empty one for offline startup and installation checks.
os.environ.setdefault("PATH", "")


def check(settings, full=False):
    checks = []
    def record(name, ok, detail, required=True):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail), "required": required})
    record("Windows x64", os.name == "nt" and platform.machine().lower() in {"amd64", "x86_64"}, platform.platform())
    record("内置 Python", Path(sys.executable).resolve() == settings.python.resolve(), sys.executable)
    for module in ("PySide6.QtWidgets", "httpx", "PIL", "pypdf", "docx", "openpyxl", "pptx", "psutil", "torch", "transformers", "airllm", "accelerate", "bitsandbytes", "laya", "libzim"):
        try:
            loaded = importlib.import_module(module)
            record(module, True, getattr(loaded, "__version__", "已安装"))
        except Exception as exc:
            record(module, False, exc)
    for binary in ("ffmpeg", "ffprobe"):
        try:
            result = subprocess.run([str(settings.binary(binary)), "-version"], capture_output=True, timeout=20, creationflags=HIDDEN)
            record(binary, result.returncode == 0, result.stdout.decode("utf-8", "replace").splitlines()[0])
        except Exception as exc:
            record(binary, False, exc)
    try:
        git = git_binary(settings)
        result = subprocess.run([str(git), "--version"], capture_output=True, timeout=20,
                                creationflags=HIDDEN)
        version = (result.stdout or result.stderr).decode("utf-8", "replace").strip()
        record("内置 Git", result.returncode == 0 and version.lower().startswith("git version "),
               version or "git --version 没有输出")
    except Exception as exc:
        record("内置 Git", False, exc)
    try:
        import torch
        record("PyTorch CUDA 运行库", torch.version.cuda is not None, f"CUDA {torch.version.cuda}")
        record("NVIDIA/CUDA", torch.cuda.is_available(), f"PyTorch CUDA {torch.version.cuda}; 驱动需预先安装", required=False)
    except Exception as exc:
        record("PyTorch CUDA 运行库", False, exc)
        record("NVIDIA/CUDA", False, exc, required=False)
    for name in ("llama-server.exe", "ggml-cuda.dll", "cublas64_12.dll", "cudart64_12.dll"):
        record(name, (settings.root / "runtime/llama" / name).is_file(), "runtime/llama/" + name)
    for name in ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
        record(name, (settings.root / "runtime/python" / name).is_file(), "runtime/python/" + name)
    catalog = ModelCatalog(settings)
    if any((m.get("runtime_profile") or {}).get("cpu_compatibility") for m in catalog.entries()):
        try:
            record("低内存 CPU 运行库", True, cpu_compatibility.binary(settings))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            record("低内存 CPU 运行库", False, exc)
    for model in catalog.entries():
        if not model["bundled"]:
            continue
        ready = catalog.ready(model)
        record(model["name"], ready, catalog.directory(model))
        if ready and full:
            for spec in model.get("files", []):
                if spec.get("sha256"):
                    path = catalog.directory(model) / spec["path"]
                    record(f"SHA256 {spec['path']}", sha256(path) == spec["sha256"], spec["sha256"])
    if full:
        auxiliary = next((model for model in catalog.entries() if model.get("role") == "auxiliary" and model.get("bundled")), None)
        if auxiliary:
            aux = settings.root / auxiliary["path"] / auxiliary["entry"]
            file_spec = next((item for item in auxiliary.get("files", []) if item.get("path") == auxiliary["entry"]), {})
            expected = file_spec.get("sha256")
            record("SHA256 辅助模型", aux.is_file() and expected is not None and sha256(aux) == expected,
                   "内置辅助模型文件")
        wikipedia = settings.data / "knowledge/archives" / BUNDLED_WIKIPEDIA
        try:
            from libzim.reader import Archive
            archive = Archive(wikipedia)
            valid = (wikipedia.stat().st_size == BUNDLED_WIKIPEDIA_BYTES
                     and sha256(wikipedia) == BUNDLED_WIKIPEDIA_SHA256
                     and archive.article_count == BUNDLED_WIKIPEDIA_ARTICLES
                     and archive.has_fulltext_index and not archive.is_multipart)
            detail = f"{archive.article_count} 条 ZIM 记录；SHA256 校验；libzim 全文索引"
        except Exception as exc:
            valid, detail = False, str(exc)
        record("Project NOMAD Wikipedia", valid, detail)
    record("可用磁盘空间", shutil.disk_usage(settings.root).free > 1024**3, f"{shutil.disk_usage(settings.root).free / 1024**3:.1f} GB", required=False)
    report = {"ok": all(c["ok"] for c in checks if c["required"]), "checks": checks}
    (settings.data / "logs/selfcheck.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    result = check(Settings(), args.full)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["ok"] else 1)
