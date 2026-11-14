from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = {
    "port": 39200, "default_model": "qwen3-8b", "vision_model": "",
    "context_length": 8192, "max_new_tokens": 1024, "gpu_memory_gb": 3.0,
    "gpu_layers": 12, "cpu_memory_gb": 12, "idle_unload_minutes": 20,
    "python_timeout_seconds": 30, "python_output_bytes": 1048576, "python_memory_gb": 4,
    "generated_file_bytes": 52428800, "generated_total_bytes": 104857600,
    "upload_bytes": 52428800, "video_bytes": 209715200, "session_bytes": 524288000,
    "max_attachments": 8, "max_document_chars": 60000, "max_video_frames": 6,
    "auxiliary_enabled": False,
    "auto_compress_context": True, "compression_threshold_percent": 75,
    "summary_timeout_seconds": 120,
    "attachment_ttl_hours": 168, "unfinished_upload_ttl_hours": 1,
    "download_mirrors": ["https://huggingface.co", "https://hf-mirror.com"],
}


class Settings:
    def __init__(self, root: Path = ROOT):
        self.root = root.resolve()
        self.data = self.root / "data"
        self.data.mkdir(parents=True, exist_ok=True)
        self.path = self.data / "workspace.json"
        for name in ("sessions", "models", "offload", "logs", "downloads"):
            (self.data / name).mkdir(exist_ok=True)
        existing, repaired, preserved = self._load_current()
        migrated = self._legacy_settings(existing)
        migrated_vision = False
        if existing.get("vision_model") == "gemma3-27b-vision":
            try:
                models = json.loads((self.root / "config/models.json").read_text("utf-8"))["models"]
                if (not any(m.get("id") == "gemma3-27b-vision" for m in models)
                        and any(m.get("id") == "gemma3-27b-fast" and m.get("vision_entry") for m in models)):
                    existing["vision_model"] = "gemma3-27b-fast"
                    migrated_vision = True
            except (OSError, ValueError, KeyError, TypeError):
                pass
        removed_memory_reserve = "system_memory_reserve_gb" in existing
        existing.pop("system_memory_reserve_gb", None)
        if repaired:
            self.migration_report["errors"].append(repaired)
            self._write_migration_report()
        self.values = DEFAULTS | existing
        should_write = (not self.path.exists() or (repaired and preserved)
                        or (migrated and not repaired) or removed_memory_reserve or migrated_vision)
        if should_write:
            # A failed write must leave the user's previous configuration intact.
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.data,
                                             prefix="workspace-", suffix=".json.tmp", delete=False) as file:
                temporary = Path(file.name)
                json.dump(self.values, file, ensure_ascii=False, indent=2)
            try:
                temporary.replace(self.path)
            finally:
                temporary.unlink(missing_ok=True)

    def _load_current(self):
        """Load the current config, preserving a damaged file before repair."""
        if not self.path.exists():
            return {}, None, False
        try:
            if self.path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError("workspace.json exceeds 4 MiB")
            value = json.loads(self.path.read_text("utf-8-sig"))
            if not isinstance(value, dict):
                raise ValueError("workspace.json must contain a JSON object")
            return value, None, False
        except (OSError, UnicodeError, ValueError, RecursionError) as exc:
            backup = self.path.with_name("workspace.json.corrupt")
            reserved = None
            try:
                # Reserve the destination exclusively before replacing the
                # damaged file. This avoids overwriting an older recovery copy
                # when several failed starts happen in the same second.
                for index in range(1000):
                    candidate = backup if index == 0 else self.path.with_name(
                        f"workspace.json.corrupt-{time.time_ns()}-{index}"
                    )
                    try:
                        handle = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    except FileExistsError:
                        continue
                    else:
                        os.close(handle)
                        backup = candidate
                        reserved = candidate
                        break
                else:
                    raise OSError("no unique workspace backup name available")
                os.replace(self.path, backup)
                detail = f"workspace.json was damaged and moved to {backup.name}: {exc}"
                preserved = True
            except OSError as move_error:
                if reserved is not None:
                    Path(reserved).unlink(missing_ok=True)
                detail = f"workspace.json is damaged and could not be preserved: {exc}; {move_error}"
                preserved = False
            return {}, detail, preserved

    def _write_migration_report(self):
        try:
            (self.data / "logs/settings-migration.json").write_text(
                json.dumps(self.migration_report, ensure_ascii=False, indent=2), "utf-8"
            )
        except OSError:
            pass

    def _legacy_settings(self, existing):
        source = self.data / "settings.json"
        report = {"source": source.name, "migrated": [], "skipped": [], "errors": []}
        self.migration_report = report
        if not source.is_file():
            return False
        try:
            if source.stat().st_size > 4 * 1024 * 1024:
                raise ValueError("旧配置文件超过 4 MiB")
            legacy = json.loads(source.read_text("utf-8-sig"))
            if not isinstance(legacy, dict):
                raise ValueError("旧配置必须是 JSON 对象")
            fields = {
                "ContextLength": ("context_length", lambda x: type(x) is int and 256 <= x <= 262144),
                "IdleUnloadMinutes": ("idle_unload_minutes", lambda x: type(x) is int and 0 <= x <= 10080),
                "AuxiliaryModelEnabled": ("auxiliary_enabled", lambda x: type(x) is bool),
                "AutoCompressContext": ("auto_compress_context", lambda x: type(x) is bool),
                "CompressionThresholdPercent": ("compression_threshold_percent", lambda x: type(x) is int and 25 <= x <= 95),
            }
            for old, (new, valid) in fields.items():
                if old not in legacy:
                    continue
                if new in existing or not valid(legacy[old]):
                    report["skipped"].append(old)
                    continue
                existing[new] = legacy[old]
                report["migrated"].append(new)
            # Paths, URLs, model selections, credentials and mode fields are deliberately
            # outside this allowlist. Their values never enter the report or new config.
        except (OSError, ValueError, RecursionError) as exc:
            report["errors"].append(str(exc))
        self._write_migration_report()
        return bool(report["migrated"])

    def __getitem__(self, key):
        return self.values[key]

    @property
    def python(self):
        path = self.root / "runtime/python/python.exe"
        if not path.is_file():
            raise RuntimeError("缺少内置 Python：请使用完整便携包，或重新运行安装检查。")
        return path

    def binary(self, name):
        path = self.root / "runtime/ffmpeg" / (name + ".exe")
        if not path.is_file():
            raise RuntimeError(f"缺少内置 {name}，请重新运行安装检查。")
        return path


def contained(root: Path, path: Path) -> Path:
    base, resolved = root.resolve(), path.resolve()
    if resolved == base or not resolved.is_relative_to(base):
        raise ValueError("路径超出允许目录")
    # Reject junctions/symlinks even when their target happens to be within the root.
    current = path.absolute()
    while current != base and current != current.parent:
        reparse = current.exists() and bool(getattr(current.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        if current.is_symlink() or reparse:
            raise ValueError("不允许符号链接或目录联接")
        current = current.parent
    return resolved


def child_environment(settings: Settings, workdir: Path):
    env = os.environ.copy()
    for key in ("PYTHONHOME", "PYTHONPATH", "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
        env.pop(key, None)
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONNOUSERSITE="1",
               HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
               HF_HOME=str(settings.data / "hf-cache"), TEMP=str(workdir), TMP=str(workdir))
    return env
