"""CPU-only llama.cpp performance tuning.

The tuner is deliberately separate from model loading.  It only runs the
bundled ``llama-bench`` executable after an explicit user action and stores a
small, hardware- and file-specific report that can safely adjust thread
counts on later launches.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from .config import child_environment, contained
from .processes import bounded_run

try:  # psutil is bundled, but keeping this optional helps small unit tests.
    import psutil
except ImportError:  # pragma: no cover - exercised only outside the bundle
    psutil = None


TUNING_FILENAME = "cpu-tuning.json"
DEFAULT_THREADS = min(os.cpu_count() or 4, 8)
DEFAULT_BATCH_SIZE = 2048
DEFAULT_UBATCH_SIZE = 512
BENCH_TIMEOUT_SECONDS = 900
BENCH_OUTPUT_LIMIT = 8 * 1024 * 1024


def _float_value(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def resource_estimate(model, hardware=None):
    """Estimate whether a model can be attempted on the current machine.

    This is a preflight diagnostic, not a performance claim.  Manifest entries
    may provide ``resource_profile`` values in GiB.  Unknown values remain
    unknown so importing a user-owned model is never blocked by a guess.
    ``hardware`` can be the dictionary returned by ``app.model_tools.hardware``;
    when omitted, inexpensive local RAM/disk checks are collected here.
    """
    profile = model.get("resource_profile") if isinstance(model, dict) else None
    if not isinstance(profile, dict):
        profile = {}
    requires_cuda = profile.get("requires_cuda", model.get("backend") == "airllm" if isinstance(model, dict) else False)
    report = dict(hardware) if isinstance(hardware, dict) else {}
    if not report:
        try:
            if psutil is not None:
                memory = psutil.virtual_memory()
                report["ram_gb"] = memory.total / 1024**3
                report["available_gb"] = memory.available / 1024**3
        except (OSError, RuntimeError):
            pass
        try:
            root = Path(__file__).resolve().parents[1]
            report["disk_free_gb"] = shutil.disk_usage(root).free / 1024**3
        except OSError:
            pass
        if requires_cuda:
            try:
                import torch
                if torch.cuda.is_available():
                    report["gpus"] = [{"name": "CUDA", "vram_gb": torch.cuda.get_device_properties(0).total_memory / 1024**3}]
                else:
                    report["gpus"] = []
            except Exception:
                report["gpus"] = []

    checks = []

    def check(code, ok, message):
        checks.append({"code": code, "ok": ok, "message": message})

    required_ram = _float_value(profile.get("minimum_ram_gb"))
    total_ram = _float_value(report.get("ram_gb"))
    if required_ram is not None:
        if total_ram is None:
            check("ram_unknown", None, f"配置建议至少需要 {required_ram:.0f} GiB 系统内存（参考值），当前容量未知")
        else:
            check("ram", total_ram >= required_ram,
                  f"系统内存 {total_ram:.1f} / 配置建议至少需要 {required_ram:.1f} GiB（参考值）")

    required_disk = _float_value(profile.get("minimum_free_disk_gb"))
    free_disk = _float_value(report.get("disk_free_gb"))
    if required_disk is not None:
        if free_disk is None:
            check("disk_unknown", None, f"首次下载或转换至少需要 {required_disk:.0f} GiB 磁盘空间（参考值），当前空间未知")
        else:
            check("disk", free_disk >= required_disk,
                  f"可用磁盘 {free_disk:.1f} / 首次下载或转换至少需要 {required_disk:.1f} GiB（参考值）")

    if requires_cuda:
        gpus = report.get("gpus")
        if isinstance(gpus, list):
            check("cuda", bool(gpus), "需要 NVIDIA CUDA；当前未检测到可用 CUDA 设备" if not gpus else "已检测到 CUDA 设备")
        else:
            check("cuda_unknown", None, "需要 NVIDIA CUDA；CUDA 状态未知")

    failed = [item for item in checks if item["ok"] is False]
    hard_failed = [item for item in failed if item["code"] == "cuda"]
    unknown = [item for item in checks if item["ok"] is None]
    # Memory and disk thresholds are advisory: a user may already have a
    # converted/offloaded cache elsewhere, or choose to accept paging.  CUDA
    # is a hard requirement for AirLLM and is the only preflight blocker.
    status = "blocked" if hard_failed else ("warning" if failed or unknown else "ready")
    speed_target = _float_value(profile.get("speed_target_tokens_per_second"))
    return {
        "model": model.get("id") if isinstance(model, dict) else None,
        "backend": model.get("backend") if isinstance(model, dict) else None,
        "status": status,
        "checks": checks,
        "requirements": profile | {"requires_cuda": bool(requires_cuda)},
        "hardware": report,
        "speed_target_tokens_per_second": speed_target,
        "speed_verified": False,
        "message": "；".join(item["message"] for item in (failed or unknown)) or "满足已知资源条件，可尝试加载",
    }


def _setting(settings, key, default=None):
    try:
        return settings[key]
    except (KeyError, TypeError, AttributeError):
        values = getattr(settings, "values", None)
        if isinstance(values, dict):
            return values.get(key, default)
        return default


def _positive_int(value, default):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _default_threads():
    return min(os.cpu_count() or 4, 8)


def hardware_fingerprint() -> dict[str, Any]:
    """Return stable CPU identifiers used to validate tuning reports."""

    logical = None
    physical = None
    if psutil is not None:
        try:
            logical = psutil.cpu_count(logical=True)
            physical = psutil.cpu_count(logical=False)
        except (OSError, RuntimeError):
            pass
    logical = _positive_int(logical, os.cpu_count() or 1)
    physical = _positive_int(physical, logical)
    return {
        "machine": (platform.machine() or "").strip().lower(),
        "processor": (platform.processor() or "").strip().lower(),
        "logical_cpus": logical,
        "physical_cpus": min(physical, logical),
    }


def _hardware_values(value):
    if not isinstance(value, dict):
        return None
    machine = value.get("machine")
    processor = value.get("processor")
    if processor is None:
        processor = value.get("cpu")
    logical = value.get("logical_cpus")
    if logical is None:
        logical = value.get("threads")
    physical = value.get("physical_cpus")
    if physical is None:
        physical = value.get("physical_threads")
    result = {}
    if machine is not None:
        result["machine"] = str(machine).strip().lower()
    if processor is not None:
        result["processor"] = str(processor).strip().lower()
    for key, item in (("logical_cpus", logical), ("physical_cpus", physical)):
        if item is not None:
            try:
                result[key] = int(item)
            except (TypeError, ValueError):
                return None
    return result


def _hardware_matches(expected, actual):
    expected = _hardware_values(expected)
    actual = _hardware_values(actual)
    if not expected or not actual:
        return False
    # Reports created by this module contain all four fields.  Accepting
    # aliases keeps migration of the older model-tools hardware shape safe,
    # while still requiring every field present in a report to match.
    return all(actual.get(key) == value for key, value in expected.items())


def _model_file(settings, model):
    root = Path(getattr(settings, "root", ".")).resolve()
    relative = model.get("path")
    entry = model.get("entry")
    if not isinstance(relative, str) or not isinstance(entry, str):
        return None
    try:
        folder = contained(root, root / relative)
        return contained(folder, folder / entry)
    except (OSError, ValueError):
        return None


def _declared_sha256(model):
    direct = model.get("sha256")
    if isinstance(direct, str) and direct:
        return direct.lower()
    entry = model.get("entry")
    for spec in model.get("files", []) or []:
        if isinstance(spec, dict) and spec.get("path") == entry:
            digest = spec.get("sha256")
            if isinstance(digest, str) and digest:
                return digest.lower()
    return None


def model_identity(settings, model) -> dict[str, Any]:
    """Describe the local GGUF without hashing it on every chat startup."""

    path = _model_file(settings, model)
    stat = None
    is_file = False
    if path is not None:
        try:
            stat = path.stat()
            is_file = path.is_file()
        except OSError:
            stat = None
    root = Path(getattr(settings, "root", ".")).resolve()
    relative_path = None
    if path is not None:
        try:
            relative_path = path.relative_to(root).as_posix()
        except ValueError:
            relative_path = path.name
    return {
        "path": relative_path,
        "entry": model.get("entry"),
        "sha256": _declared_sha256(model),
        "size": stat.st_size if stat is not None and is_file else None,
        "mtime_ns": stat.st_mtime_ns if stat is not None and is_file else None,
    }


def _identity_matches(expected, actual):
    if not isinstance(expected, dict) or not isinstance(actual, dict):
        return False
    # A declared SHA is preferred; size + mtime protects imported files whose
    # manifest has no digest without forcing a multi-gigabyte rehash.  A path
    # or filename alone is not a file identity and must never enable tuning.
    markers = ("sha256", "size", "mtime_ns", "path", "entry")
    if expected.get("sha256") is None and not (expected.get("size") is not None and expected.get("mtime_ns") is not None):
        return False
    for key in markers:
        expected_value = expected.get(key)
        if expected_value is not None and actual.get(key) != expected_value:
            return False
    return True


def _tuning_path(settings):
    data = getattr(settings, "data", None)
    if data is None:
        return None
    return Path(data) / TUNING_FILENAME


def _load_tuning(settings):
    path = _tuning_path(settings)
    if path is None or not path.is_file():
        return None
    try:
        if path.stat().st_size > 4 * 1024 * 1024:
            return None
        value = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _tuned_threads(settings, model):
    if (model.get("backend") or "llama") != "llama" or not str(model.get("entry", "")).lower().endswith(".gguf"):
        return None
    data = _load_tuning(settings)
    if not data:
        return None
    rows = data.get("models")
    model_id = model.get("id")
    record = None
    if isinstance(rows, dict):
        record = rows.get(model_id)
    elif isinstance(rows, list):
        record = next((row for row in rows if isinstance(row, dict) and row.get("model") == model_id), None)
    elif model_id in data and isinstance(data[model_id], dict):
        # Accept the early prototype shape while it is still local-only.
        record = data[model_id]
    if not isinstance(record, dict):
        return None
    expected_hardware = record.get("hardware") or data.get("hardware")
    if not _hardware_matches(expected_hardware, hardware_fingerprint()):
        return None
    identity = model_identity(settings, model)
    if not _identity_matches(record.get("identity"), identity):
        return None
    fastest = record.get("fastest")
    value = record.get("threads")
    if isinstance(fastest, dict):
        value = fastest.get("threads", value)
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    physical = hardware_fingerprint().get("physical_cpus", 1)
    return value if 1 <= value <= max(physical, 1) else None


def effective_options(settings, model) -> dict[str, int]:
    """Return llama.cpp runtime options for a model.

    Models without a runtime profile retain the existing settings exactly,
    including the historical context/max-token validation behavior.  A
    profile is an explicit cap, so its max-new-token value is also kept below
    the effective context length.
    """

    context = _positive_int(_setting(settings, "context_length", 8192), 8192)
    maximum = _positive_int(_setting(settings, "max_new_tokens", 1024), 1024)
    threads = _default_threads()
    batch_size = DEFAULT_BATCH_SIZE
    ubatch_size = DEFAULT_UBATCH_SIZE
    profile = model.get("runtime_profile") if isinstance(model, dict) else None
    if isinstance(profile, dict):
        profile_context = profile.get("context_length", profile.get("max_context_length"))
        profile_maximum = profile.get("max_new_tokens")
        if profile_context is not None:
            context = min(context, _positive_int(profile_context, context))
        if profile_maximum is not None:
            maximum = min(maximum, _positive_int(profile_maximum, maximum))
        if profile.get("threads") is not None:
            threads = max(1, _positive_int(profile.get("threads"), threads))
        if profile.get("batch_size") is not None:
            batch_size = _positive_int(profile.get("batch_size"), batch_size)
        if profile.get("ubatch_size") is not None:
            ubatch_size = _positive_int(profile.get("ubatch_size"), ubatch_size)
        if context <= 1:
            raise ValueError("runtime_profile 的 context_length 必须大于 1")
        maximum = min(maximum, context - 1)
    # Text-only tuning does not measure the projector or image-attention path.
    tuned = (_tuned_threads(settings, model) if isinstance(model, dict)
             and model.get("_runtime_variant") != "vision" else None)
    if tuned is not None:
        threads = tuned
    if isinstance(profile, dict) and profile.get("cpu_compatibility"):
        threads = min(threads, 4, os.cpu_count() or 1)
    return {"context_length": context, "max_new_tokens": maximum,
            "threads": threads, "threads_batch": threads,
            "batch_size": batch_size, "ubatch_size": ubatch_size}


def _physical_cpus():
    if psutil is not None:
        try:
            value = psutil.cpu_count(logical=False)
            if value:
                return int(value)
        except (OSError, RuntimeError):
            pass
    return int(os.cpu_count() or 1)


def _thread_candidates(physical, requested=None):
    physical = max(1, int(physical or 1))
    if requested is None:
        requested = (2, 4, 6, 8)
    elif isinstance(requested, str):
        requested = [x.strip() for x in requested.split(",") if x.strip()]
    values = []
    for item in requested:
        try:
            value = min(physical, int(item))
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in values:
            values.append(value)
    return values or [physical]


def _bench_binary(settings):
    root = Path(getattr(settings, "root", "."))
    for name in ("llama-bench.exe", "llama-bench"):
        candidate = root / "runtime" / "llama" / name
        if candidate.is_file():
            return candidate
    raise ValueError("缺少内置 llama-bench，请重新运行安装检查。")


def _parse_bench_rows(output):
    try:
        value = json.loads(output)
    except (TypeError, ValueError):
        value = None
        # Some builds print a short diagnostic before the JSON payload.
        for marker in ("[", "{"):
            start = output.find(marker)
            while start >= 0:
                try:
                    value = json.loads(output[start:])
                    break
                except ValueError:
                    start = output.find(marker, start + 1)
            if value is not None:
                break
    if value is None:
        raise ValueError("llama-bench 未返回有效 JSON")

    def walk(item):
        if isinstance(item, list):
            for child in item:
                yield from walk(child)
        elif isinstance(item, dict):
            results = item.get("results")
            if isinstance(results, (list, dict)):
                yield from walk(results)
            else:
                yield item

    rows = []
    for raw in walk(value):
        label = str(raw.get("test") or raw.get("name") or raw.get("type") or "").lower()
        prompt = raw.get("n_prompt", raw.get("prompt_tokens"))
        generated = raw.get("n_gen", raw.get("generated_tokens"))
        try:
            prompt = int(prompt) if prompt is not None else None
            generated = int(generated) if generated is not None else None
        except (TypeError, ValueError):
            prompt = generated = None
        if "tg32" in label or generated == 32:
            phase, test = "decode", "tg32"
        elif "pp64" in label or prompt == 64:
            phase, test = "prefill", "pp64"
        else:
            continue
        rate = None
        for key in ("avg_ts", "tokens_per_second", "tokens_per_sec", "t/s", "tps"):
            candidate = raw.get(key)
            if isinstance(candidate, (int, float)):
                rate = float(candidate)
                break
        thread_value = raw.get("n_threads", raw.get("threads", raw.get("thread")))
        try:
            thread_value = int(thread_value)
        except (TypeError, ValueError):
            thread_value = None
        if rate is None or rate <= 0 or thread_value is None or thread_value <= 0:
            continue
        row = {"test": test, "phase": phase, "threads": thread_value,
               "tokens_per_second": rate}
        if isinstance(raw.get("stddev_ts"), (int, float)):
            row["stddev_tokens_per_second"] = float(raw["stddev_ts"])
        rows.append(row)
    return rows


def _save_tuning(settings, model_id, report):
    path = _tuning_path(settings)
    if path is None:
        raise ValueError("设置没有可写的数据目录")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _load_tuning(settings) or {"schema_version": 1, "models": {}}
    models = data.get("models")
    if not isinstance(models, dict):
        models = {}
        data["models"] = models
    models[model_id] = report
    data["schema_version"] = 1
    data["hardware"] = report["hardware"]
    staged = path.with_name(path.name + ".tmp")
    staged.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    staged.replace(path)


def tune_cpu(workspace, model_id=None, cancel=None, user_initiated=False, *, threads=None,
             timeout=BENCH_TIMEOUT_SECONDS):
    """Benchmark the ready GGUF model with the bundled CPU llama-bench."""

    if not user_initiated:
        raise ValueError("速度调优必须由用户点击运行")
    settings = workspace.settings
    if model_id is None:
        model_id = _setting(settings, "default_model")
    model = workspace.catalog.get(model_id)
    if model.get("backend") != "llama" or model.get("role") != "chat" or not str(model.get("entry", "")).lower().endswith(".gguf"):
        raise ValueError("请选择已下载的 GGUF 文字聊天模型")
    if not workspace.catalog.ready(model):
        raise ValueError("请先下载或导入完整的 GGUF 模型")
    model_path = _model_file(settings, model)
    if model_path is None or not model_path.is_file():
        raise ValueError("GGUF 模型文件不存在")
    try:
        with model_path.open("rb") as source:
            if source.read(4) != b"GGUF":
                raise ValueError("模型文件不是有效的 GGUF")
    except OSError as exc:
        raise ValueError("无法读取 GGUF 模型文件") from exc
    binary = _bench_binary(settings)
    runtime = workspace.runtime
    if not runtime.lock.acquire(False):
        raise ValueError("模型正在使用，请稍后调优")
    cancel = cancel if cancel is not None else threading.Event()
    try:
        if cancel.is_set():
            raise ValueError("速度调优已取消")
        runtime.unload()
        profile = model.get("runtime_profile") or {}
        requested = threads if threads is not None else profile.get("benchmark_threads")
        if not isinstance(requested, (str, list, tuple)):
            requested = None
        candidates = _thread_candidates(_physical_cpus(), requested)
        repetitions = min(5, _positive_int(profile.get("benchmark_repetitions"), 2))
        command = [str(binary), "-ngl", "0", "-m", str(model_path), "-p", "64", "-n", "32",
                   "-t", ",".join(str(value) for value in candidates), "-r", str(repetitions), "-o", "json"]
        profile = model.get("runtime_profile") if isinstance(model, dict) else None
        if isinstance(profile, dict):
            batch = _positive_int(profile.get("batch_size"), 0)
            ubatch = _positive_int(profile.get("ubatch_size"), 0)
            if batch:
                command += ["-b", str(batch)]
            if ubatch:
                command += ["-ub", str(ubatch)]
            load_mode = profile.get("load_mode")
            if load_mode in {"auto", "none", "mmap", "mlock", "mmap+mlock", "dio"}:
                command += ["-lm", load_mode]
        root = Path(getattr(settings, "root", ".")).resolve()
        try:
            env = child_environment(settings, root)
        except (AttributeError, OSError, ValueError):
            env = os.environ.copy()
        env["PATH"] = str(binary.parent) + os.pathsep + env.get("PATH", "")
        from .memory_limits import budget_for
        budget = budget_for(settings)
        result = bounded_run(command, cwd=root, env=env, timeout=max(1, int(timeout)),
                             limit=BENCH_OUTPUT_LIMIT, cancelled=cancel, **budget.launch_kwargs())
        if result.get("reason") == "cancelled" or cancel.is_set():
            raise ValueError("速度调优已取消")
        if result.get("reason"):
            if result.get("memory_error"):
                raise ValueError(result["memory_error"])
            labels = {"timeout": "速度调优超时", "output_limit": "调优输出超过上限",
                      "disk_limit": "调优工作目录超过上限"}
            raise ValueError(labels.get(result["reason"], "速度调优失败：" + str(result["reason"])))
        if result.get("returncode") not in (0, None):
            detail = (result.get("stderr") or result.get("stdout") or "").strip()
            raise ValueError("llama-bench 执行失败" + (f"：{detail[-500:]}" if detail else ""))
        rows = _parse_bench_rows(result.get("stdout", ""))
        decode = [row for row in rows if row["phase"] == "decode"]
        prefill = [row for row in rows if row["phase"] == "prefill"]
        if not decode:
            raise ValueError("llama-bench 未返回 tg32 解码结果")
        fastest = sorted(decode, key=lambda row: (-row["tokens_per_second"], row["threads"]))[0]
        report = {
            "schema_version": 1,
            "model": model_id,
            "time": time.time(),
            "seconds": result.get("seconds"),
            "benchmark_plan": {"threads": candidates, "repetitions": repetitions,
                               "prompt_tokens": 64, "generated_tokens": 32},
            "hardware": hardware_fingerprint(),
            "identity": model_identity(settings, model),
            "rows": rows,
            "prefill": prefill,
            "decode": decode,
            "fastest": dict(fastest),
            # The desktop tuner consumes these top-level values directly.
            "threads": fastest["threads"],
            "tokens_per_second": fastest["tokens_per_second"],
        }
        _save_tuning(settings, model_id, report)
        return report
    finally:
        try:
            runtime.unload()
        finally:
            runtime.lock.release()

