"""Bounded CPU paths for the three supplied models; never substitutes weights."""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

from .memory_limits import MemoryBudget

GIB = 1024 ** 3
SUPPORTED = {"qwen3-8b": "mapped", "gemma3-27b-fast": "mapped", "llama3-70b-q4km-cpu": "staged"}
SERVER_SHA256 = "d9d4edc3899780555a90868c91f26c932ae636b030eddd8ddf799c76e24623d3"
FILES = ("llama-server.exe", "libgcc_s_seh-1.dll", "libstdc++-6.dll", "libwinpthread-1.dll")


def mode(model):
    selected = (model.get("runtime_profile") or {}).get("cpu_compatibility")
    if selected is None:
        return None
    if SUPPORTED.get(model.get("id")) != selected:
        raise ValueError("此模型没有经过验证的低内存 CPU 加载配置")
    return selected


@lru_cache(maxsize=8)
def _verify(root, identities):
    directory = Path(root)
    manifest = json.loads((directory / "runtime-manifest.json").read_text("utf-8"))
    if set(manifest.get("files", {})) != set(FILES):
        raise ValueError("低内存运行库清单不完整，请重新完整解压修复包")
    if manifest["files"]["llama-server.exe"]["sha256"] != SERVER_SHA256:
        raise ValueError("低内存运行库版本不匹配")
    for name in FILES:
        path = directory / name
        expected = manifest["files"][name]
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if path.stat().st_size != expected["bytes"] or actual != expected["sha256"]:
            raise ValueError("低内存运行库校验失败：" + name)
    return str(directory / "llama-server.exe")


def binary(settings):
    directory = settings.root / "runtime/llama-low-memory"
    try:
        identities = tuple((name, (directory / name).stat().st_size, (directory / name).stat().st_mtime_ns)
                           for name in FILES + ("runtime-manifest.json",))
        return Path(_verify(str(directory), identities))
    except OSError as exc:
        raise ValueError("缺少低内存运行库，请完整解压包含 runtime/llama-low-memory 的修复包") from exc


def environment(original, model, folder):
    selected = mode(model)
    result = {k: v for k, v in original.items()
              if not k.upper().startswith("LLAMA_WIN_") and k.upper() != "LLAMA_HOTSWAP_ENABLED"}
    if selected:
        result.update(LLAMA_WIN_MMAP_NO_PREFETCH="1", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        if selected == "staged":
            result.update(LLAMA_WIN_LAYER_STREAM_FILE=str((folder / model["entry"]).resolve()),
                          LLAMA_WIN_LAYER_STREAM_UNBUFFERED="1", LLAMA_WIN_LAYER_STREAM_DIRECT="1")
    return result


def bounded_budget(original, model):
    selected = mode(model)
    if selected is None:
        return original
    # Mapped weights can stay resident when the launch budget permits. A
    # fixed 4 GiB cap otherwise forces even the supplied 8B to reread pages.
    # Keep the separately validated staged 70B process boundary unchanged.
    resident = (min(4 * GIB, original.resident_bytes)
                if selected == "staged" else original.resident_bytes)
    if resident < 2 * GIB:
        raise ValueError("内存保护：低内存模式需要至少 2 GiB 可用内存预算。"
                         "请释放内存或检查模型内存预算后重试。")
    return MemoryBudget(original.limit_bytes, resident)


def read_timeout(model):
    # A full 2048-token CPU prompt can take well over ten minutes with the
    # disk-backed 70B path. Cancellation still closes the owned process.
    return 3600 if mode(model) == "staged" else 600


def verify_activation(log_path, offset, selected):
    with Path(log_path).open("rb") as stream:
        stream.seek(offset)
        content = stream.read(2 * 1024 * 1024).decode("utf-8", "replace")
    # Staged loading reserves address space and reads each layer directly;
    # it never creates a whole-file mmap, so its log has no mmap marker.
    if selected == "mapped" and "win_mmap: loading prefetch disabled by LLAMA_WIN_MMAP_NO_PREFETCH=1" not in content:
        raise ValueError("低内存加载未生效，已停止模型以避免整文件预加载")
    if selected == "staged" and not any("layer_stream: enabled" in line and "mode=unbuffered" in line
                                        and "direct_requested=1" in line for line in content.splitlines()):
        raise ValueError("70B 分层读取未生效，已停止模型")
