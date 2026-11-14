"""Privacy-preserving runtime telemetry and portable log export."""
from __future__ import annotations

import csv
import io
import json
import os
import platform
import shutil
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import psutil

SOFTWARE_VERSION = "2026.10.28.4"
METRIC_SCHEMA_VERSION = 2
RUN_LOG = "run-history.jsonl"
_LOCK = threading.Lock()
_METRIC_KEYS = (
    "generated_tokens", "prompt_tokens", "first_token_seconds", "model_ready_seconds",
    "generation_seconds", "tokens_per_second", "whole_request_tokens_per_second",
    "prompt_tokens_per_second", "decode_tokens_per_second",
    "observed_decode_tokens", "observed_decode_seconds", "observed_decode_tokens_per_second",
)
_RATE_SOURCES = {"native_backend", "worker_generated_ids"}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def _safe_metrics(metrics):
    result = {key: _number(metrics.get(key)) for key in _METRIC_KEYS
              if _number(metrics.get(key)) is not None}
    if (isinstance(metrics.get("tokens_per_second_source"), str)
            and metrics["tokens_per_second_source"] in _RATE_SOURCES):
        result["tokens_per_second_source"] = metrics["tokens_per_second_source"]
    return result


def _cpu_model():
    """Read the Windows CPU brand without spawning a process or identifying a device."""
    if platform.system() == "Windows":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
                0,
                winreg.KEY_READ,
            ) as key:
                name, value_type = winreg.QueryValueEx(key, "ProcessorNameString")
            if value_type == winreg.REG_SZ and isinstance(name, str) and name.strip():
                return " ".join(name.split())
        except (ImportError, OSError):
            pass
    return platform.processor() or platform.machine()


def _hardware_snapshot(include_cuda=False):
    memory = psutil.virtual_memory()
    try:
        physical = psutil.cpu_count(logical=False)
    except (OSError, RuntimeError):
        physical = None
    result = {
        "os": platform.system(),
        "os_release": platform.release(),
        "architecture": platform.machine(),
        "cpu_model": _cpu_model(),
        "logical_cpus": psutil.cpu_count(logical=True),
        "physical_cpus": physical,
        "ram_total_bytes": memory.total,
        "system_available_bytes": memory.available,
    }
    if include_cuda:
        cuda = {"available": None, "runtime": None, "devices": []}
        try:
            import torch
            cuda["runtime"] = torch.version.cuda
            cuda["available"] = bool(torch.cuda.is_available())
            if cuda["available"]:
                for index in range(torch.cuda.device_count()):
                    card = torch.cuda.get_device_properties(index)
                    cuda["devices"].append({"name": card.name, "memory_bytes": int(card.total_memory)})
        except Exception as exc:
            cuda["error_type"] = type(exc).__name__
        result["cuda"] = cuda
    return result


def _declared_hash(model):
    for item in model.get("files", []) or []:
        if isinstance(item, dict) and item.get("path") == model.get("entry"):
            value = item.get("sha256")
            return value if isinstance(value, str) else None
    return None


def _runtime_memory(runtime):
    result = {"system_available_bytes": psutil.virtual_memory().available,
              "app_rss_bytes": None, "model_rss_bytes": None, "model_commit_bytes": None}
    try:
        result["app_rss_bytes"] = psutil.Process(os.getpid()).memory_info().rss
    except (psutil.Error, OSError):
        pass
    process = getattr(runtime, "process", None)
    if process is not None:
        try:
            info = psutil.Process(process.pid).memory_info()
            result["model_rss_bytes"] = info.rss
            result["model_commit_bytes"] = getattr(info, "private", info.vms)
        except (psutil.Error, OSError, AttributeError, TypeError):
            pass
    job = getattr(runtime, "job", None)
    status = getattr(job, "memory_status", None) if job is not None else None
    if isinstance(status, dict):
        for source, target in (("rss", "model_rss_bytes"), ("available", "system_available_bytes"),
                               ("commit", "model_commit_bytes")):
            if isinstance(status.get(source), int):
                result[target] = status[source]
    return result


def _append(settings, record):
    path = Path(settings.data) / "logs" / RUN_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK, path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def record_generation(settings, catalog, runtime, model, *, status, started_at,
                      request_seconds, error_type=None, metrics_override=None):
    """Append one aggregate record. Prompt, response, files, and paths are omitted."""
    try:
        from .performance import effective_options

        metrics = metrics_override if isinstance(metrics_override, dict) else (getattr(runtime, "last_metrics", None) or {})
        safe_metrics = _safe_metrics(metrics)
        file_bytes = None
        try:
            file_bytes = (catalog.directory(model) / model["entry"]).stat().st_size
        except (OSError, KeyError, TypeError):
            pass
        try:
            options = effective_options(settings, model)
        except (AttributeError, KeyError, TypeError, ValueError):
            options = {}
        profile = model.get("runtime_profile") or {}
        runtime_mode = profile.get("cpu_compatibility") or model.get("backend") or "unknown"
        record = {
            "timestamp_utc": started_at,
            "kind": "generation",
            "status": status,
            "software_version": SOFTWARE_VERSION,
            "metric_schema_version": METRIC_SCHEMA_VERSION,
            "model_id": str(model.get("id", "")),
            "model_name": str(model.get("name", "")),
            "backend": str(model.get("backend", "")),
            "quantization": str(model.get("quantization", "")),
            "runtime_mode": str(runtime_mode),
            "runtime_variant": str(model.get("_runtime_variant", "text")),
            "model_file_bytes": file_bytes,
            "model_sha256": _declared_hash(model),
            "runtime_options": {key: value for key, value in options.items()
                                if key in {"context_length", "max_new_tokens", "threads", "threads_batch",
                                           "batch_size", "ubatch_size"}},
            "metrics": safe_metrics,
            "request_seconds": _number(request_seconds),
            "hardware": _hardware_snapshot(),
            "memory": _runtime_memory(runtime),
            "error_type": str(error_type)[:80] if error_type else None,
        }
        _append(settings, record)
        return True
    except Exception:
        # Telemetry must never interrupt a chat request.
        return False


def _read_run_records(path):
    rows = []
    if not path.is_file():
        return rows
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if len(line) > 128 * 1024:
                    continue
                try:
                    value = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(value, dict):
                    continue
                # Rebuild from an explicit allowlist so edited or legacy logs
                # cannot smuggle prompts, answers, credentials, or file paths.
                kind = value.get("kind")
                if kind == "generation":
                    safe = {key: value.get(key) for key in (
                        "timestamp_utc", "kind", "status", "software_version", "model_id", "model_name",
                        "backend", "quantization", "runtime_mode", "runtime_variant", "model_file_bytes", "model_sha256",
                        "runtime_options", "request_seconds", "hardware", "memory", "error_type")}
                    metrics = value.get("metrics") if isinstance(value.get("metrics"), dict) else {}
                    safe["metrics"] = _safe_metrics(metrics)
                    safe["metric_schema_version"] = (
                        METRIC_SCHEMA_VERSION if type(value.get("metric_schema_version")) is int
                        and value["metric_schema_version"] == METRIC_SCHEMA_VERSION else None)
                    for field, allowed in {
                        "runtime_options": ("context_length", "max_new_tokens", "threads", "threads_batch", "batch_size", "ubatch_size"),
                        "memory": ("system_available_bytes", "app_rss_bytes", "model_rss_bytes", "model_commit_bytes"),
                    }.items():
                        nested = value.get(field) if isinstance(value.get(field), dict) else {}
                        safe[field] = {key: _number(nested[key]) for key in allowed if _number(nested.get(key)) is not None}
                    hardware = value.get("hardware") if isinstance(value.get("hardware"), dict) else {}
                    safe["hardware"] = {key: hardware[key] for key in (
                        "os", "os_release", "architecture", "cpu_model", "logical_cpus", "physical_cpus",
                        "ram_total_bytes", "system_available_bytes") if key in hardware and isinstance(hardware[key], (str, int, float))}
                    safe["runtime_variant"] = value.get("runtime_variant") if value.get("runtime_variant") in ("text", "vision") else None
                    rows.append(safe)
                elif kind == "log_export":
                    rows.append({key: value.get(key) for key in (
                        "timestamp_utc", "kind", "status", "software_version", "archive_bytes", "error_type")})
    except OSError:
        return []
    return rows


def _safe_json_file(path, kind):
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None
    if kind == "selfcheck" and isinstance(value, dict):
        checks = value.get("checks")
        if not isinstance(checks, list):
            return None
        return {"ok": bool(value.get("ok")), "checks": [
            {"name": str(row.get("name", ""))[:120], "ok": bool(row.get("ok")),
             "required": bool(row.get("required", True))}
            for row in checks if isinstance(row, dict)]}
    if kind == "benchmarks" and isinstance(value, dict):
        result = {}
        for model_id, row in value.items():
            if not isinstance(row, dict):
                continue
            metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
            hardware = row.get("hardware") if isinstance(row.get("hardware"), dict) else {}
            result[str(model_id)[:120]] = {
                "time": _number(row.get("time")),
                "seconds": _number(row.get("seconds")),
                "metrics": _safe_metrics(metrics),
                "hardware": {key: hardware.get(key) for key in (
                    "cpu", "threads", "ram_gb", "available_gb", "gpus") if key in hardware},
                "inference": {key: int(item) for key, item in (row.get("inference") or {}).items()
                              if key in {"context_length", "max_new_tokens", "threads", "threads_batch",
                                         "batch_size", "ubatch_size"} and isinstance(item, int) and not isinstance(item, bool)},
            }
        return result
    return None


def _csv_bytes(rows):
    fields = ("timestamp_utc", "status", "software_version", "metric_schema_version",
              "model_id", "model_sha256", "backend", "quantization", "runtime_mode", "runtime_variant",
              "generated_tokens", "tokens_per_second", "first_token_seconds", "model_ready_seconds",
              "tokens_per_second_source", "observed_decode_tokens_per_second",
              "observed_decode_tokens", "observed_decode_seconds",
              "whole_request_tokens_per_second", "generation_seconds", "request_seconds",
              "context_length", "max_new_tokens", "threads", "threads_batch", "batch_size", "ubatch_size",
              "cpu_model", "logical_cpus", "physical_cpus",
              "ram_total_bytes", "system_available_bytes", "model_rss_bytes", "model_commit_bytes", "error_type")
    target = io.StringIO(newline="")
    writer = csv.DictWriter(target, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        hardware = row.get("hardware") if isinstance(row.get("hardware"), dict) else {}
        memory = row.get("memory") if isinstance(row.get("memory"), dict) else {}
        metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
        options = row.get("runtime_options") if isinstance(row.get("runtime_options"), dict) else {}
        writer.writerow({
            "timestamp_utc": row.get("timestamp_utc"), "status": row.get("status"),
            "software_version": row.get("software_version"),
            "metric_schema_version": row.get("metric_schema_version"),
            "model_id": row.get("model_id"), "backend": row.get("backend"),
            "model_sha256": row.get("model_sha256"),
            "quantization": row.get("quantization"), "runtime_mode": row.get("runtime_mode"),
            "runtime_variant": row.get("runtime_variant"),
            "generated_tokens": metrics.get("generated_tokens"),
            "tokens_per_second": metrics.get("tokens_per_second"),
            "tokens_per_second_source": metrics.get("tokens_per_second_source"),
            "observed_decode_tokens_per_second": metrics.get("observed_decode_tokens_per_second"),
            "observed_decode_tokens": metrics.get("observed_decode_tokens"),
            "observed_decode_seconds": metrics.get("observed_decode_seconds"),
            "whole_request_tokens_per_second": metrics.get("whole_request_tokens_per_second"),
            "first_token_seconds": metrics.get("first_token_seconds"),
            "model_ready_seconds": metrics.get("model_ready_seconds"),
            "generation_seconds": metrics.get("generation_seconds"),
            "request_seconds": row.get("request_seconds"),
            **{key: options.get(key) for key in (
                "context_length", "max_new_tokens", "threads", "threads_batch", "batch_size", "ubatch_size")},
            "cpu_model": hardware.get("cpu_model"), "logical_cpus": hardware.get("logical_cpus"),
            "physical_cpus": hardware.get("physical_cpus"), "ram_total_bytes": hardware.get("ram_total_bytes"),
            "system_available_bytes": memory.get("system_available_bytes"),
            "model_rss_bytes": memory.get("model_rss_bytes"),
            "model_commit_bytes": memory.get("model_commit_bytes"), "error_type": row.get("error_type"),
        })
    return target.getvalue().encode("utf-8-sig")


def export_logs(settings, destination):
    """Create a portable ZIP containing only approved, non-conversational telemetry."""
    target = Path(destination).expanduser().resolve()
    if not target.parent.is_dir():
        raise ValueError("请先选择已存在的保存文件夹")
    rows = _read_run_records(Path(settings.data) / "logs" / RUN_LOG)
    environment = {
        "software_version": SOFTWARE_VERSION,
        "exported_at_utc": datetime.now(timezone.utc).isoformat(),
        "hardware": _hardware_snapshot(include_cuda=True),
        "application_root_is_included": False,
    }
    manifest_path = Path(settings.root) / "config" / "models.json"
    try:
        manifest = json.loads(manifest_path.read_text("utf-8"))
        environment["available_models"] = [
            {"id": str(model.get("id", "")), "name": str(model.get("name", "")),
             "quantization": str(model.get("quantization", "")),
             "bundled": bool(model.get("bundled"))}
            for model in manifest.get("models", []) if isinstance(model, dict)]
    except (OSError, UnicodeError, ValueError, TypeError):
        environment["available_models"] = []

    content = {
        "runtime-environment.json": json.dumps(environment, ensure_ascii=False, indent=2).encode("utf-8"),
        "runs.jsonl": ("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                                for row in rows)).encode("utf-8"),
        "runs.csv": _csv_bytes(rows),
        "README.txt": ("Local AI Workspace 运行统计\n\n"
                       "runs.csv / runs.jsonl 每条记录是一轮本地推理或日志导出。包含模型身份、运行参数、首字时间、生成速度、token 数、设备概况和内存数据。\n"
                       "此 ZIP 不含聊天正文、问题提示、回答、附件、会话数据库、个人配置、访问密钥或本机绝对路径。\n"
                       "tokens_per_second 保留原始数值精度，显示时才缩短小数；tokens_per_second_source 标明 native_backend（后端报告）或 worker_generated_ids（原生生成 ID 计数）。不同后端的原生计时口径可能不同。\n"
                       "observed_decode_tokens_per_second 是首个正原生计数事件到最后一次计数增长之间的 token 增量/经过秒数；排除首批全部 tokens，重复最终事件不延长窗口。它包括流式交付开销，不是纯内核速度。\n"
                       "少于两个不同的有效正原生计数、计数回退或数据不足时，观测速率留空；不按 SSE 分包数或最终 usage 猜测。旧记录的口径版本留空，不推断或回填精度。\n"
                       "metric_schema_version=2 表示新增统计口径；首字时间从提交本轮推理到首段可见文本，受上下文整理、模型准备和提示词处理共同影响。\n"
                       "model_ready_seconds 是后端启动并报告就绪的时间；部分分层加载后端会在首轮推理继续载入权重，因此应与首字时间一起比较。\n"
                       "runs.csv 提供软件版本、模型 SHA256、CPU、内存、运行模式、上下文、线程和 batch/ubatch 列，便于分组。模型 SHA256 来自模型清单，并非每轮重新校验。\n").encode("utf-8"),
    }
    selfcheck = _safe_json_file(Path(settings.data) / "logs" / "selfcheck.json", "selfcheck")
    if selfcheck is not None:
        content["selfcheck-summary.json"] = json.dumps(selfcheck, ensure_ascii=False, indent=2).encode("utf-8")
    benchmarks = _safe_json_file(Path(settings.data) / "model-benchmarks.json", "benchmarks")
    if benchmarks is not None:
        content["benchmark-summary.json"] = json.dumps(benchmarks, ensure_ascii=False, indent=2).encode("utf-8")

    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(prefix=target.name + ".", suffix=".tmp", dir=target.parent,
                                         delete=False) as temporary:
            temp_name = temporary.name
        with zipfile.ZipFile(temp_name, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name, data in content.items():
                archive.writestr(name, data)
        os.replace(temp_name, target)
        temp_name = None
        _append(settings, {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "kind": "log_export",
                           "status": "success", "software_version": SOFTWARE_VERSION,
                           "archive_bytes": target.stat().st_size, "error_type": None})
        return target
    except Exception as exc:
        _append(settings, {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "kind": "log_export",
                           "status": "failed", "software_version": SOFTWARE_VERSION,
                           "archive_bytes": None, "error_type": type(exc).__name__})
        raise
    finally:
        if temp_name:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass
