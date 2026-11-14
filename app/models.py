from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import os
import queue
import secrets
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

from .config import child_environment, contained
from .processes import HIDDEN, terminate_tree
from .windows_job import start_owned
from .performance import effective_options, resource_estimate
from .memory_limits import budget_for, memory_settings
from .stream_metrics import NativeCounterWindow
from . import cpu_compatibility


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


class DownloadCancelled(Exception):
    pass


def request_model(model, *, vision=False):
    """Choose an execution profile without adding a second catalog model."""
    override = model.get("vision_runtime_profile")
    if not isinstance(override, dict) or "_runtime_variant" in model:
        return model
    selected = dict(model)
    selected["_runtime_variant"] = "vision" if vision else "text"
    if vision:
        if not model.get("vision_entry"):
            raise ValueError("此模型缺少视觉投影文件配置")
        selected["runtime_profile"] = dict(model.get("runtime_profile") or {}) | override
    else:
        selected.pop("vision_entry", None)
    return selected


class ModelCatalog:
    def __init__(self, settings):
        self.settings = settings
        self.manifest = settings.root / "config/models.json"
        self.jobs = {}
        self.guard = threading.Lock()
        self._operations = {}

    def entries(self):
        models = json.loads(self.manifest.read_text("utf-8"))["models"]
        custom = self.settings.data / "model-catalog.json"
        if custom.is_file():
            additions = json.loads(custom.read_text("utf-8"))
            ids = {model["id"] for model in models}
            for model in additions:
                if model["id"] not in ids:
                    models.append(model)
                    ids.add(model["id"])
        return models

    def get(self, mid):
        for model in self.entries():
            if model["id"] == mid:
                return model
        raise ValueError("模型不在 manifest 中")

    def directory(self, model):
        return contained(self.settings.root, self.settings.root / model["path"])

    def ready(self, model):
        folder = self.directory(model)
        required = model.get("required", [model["entry"]])
        if model.get("vision_entry"):
            required = required + [model["vision_entry"]]
        if not all(contained(folder, folder / name).is_file() for name in required):
            return False
        if model["backend"] == "airllm":
            if not any((folder / name).is_file() for name in ("tokenizer.json", "tokenizer.model", "tokenizer.model.v3")):
                return False
            index = folder / "model.safetensors.index.json"
            if index.exists():
                try:
                    shards = set(json.loads(index.read_text("utf-8"))["weight_map"].values())
                    return bool(shards) and all(contained(folder, folder / x).is_file() for x in shards)
                except (ValueError, KeyError):
                    return False
            return (folder / "model.safetensors").is_file()
        for spec in model.get("files", []):
            path = contained(folder, folder / spec["path"])
            if not path.exists() or (spec.get("size") and path.stat().st_size != spec["size"]):
                return False
        return True

    def public(self):
        with self.guard:
            jobs = {mid: dict(job) for mid, job in self.jobs.items()}
        return [m | {"ready": self.ready(m), "download": jobs.get(m["id"])} for m in self.entries()]

    def _claim(self, model, kind, cancel=None):
        """Called under guard; reserve overlapping directories until work ends."""
        folder = self.directory(model)
        for operation in self._operations.values():
            other = operation["folder"]
            if folder == other or folder.is_relative_to(other) or other.is_relative_to(folder):
                raise ValueError("此模型目录正在下载、导入或删除，请等待操作结束")
        operation = {"folder": folder, "kind": kind, "cancel": cancel}
        self._operations[model["id"]] = operation
        return operation

    def _release(self, mid, operation):
        with self.guard:
            if self._operations.get(mid) is operation:
                self._operations.pop(mid)

    def download(self, mid, token=None):
        model = self.get(mid)
        with self.guard:
            active = self._operations.get(mid)
            if active and active["kind"] == "download":
                return self.jobs[mid]
            cancel = threading.Event()
            operation = self._claim(model, "download", cancel)
            job = {"status": "pending", "bytes": 0, "total": 0, "file": ""}
            self.jobs[mid] = job
        try:
            threading.Thread(target=self._download, args=(model, job, token, cancel, operation), daemon=True).start()
        except BaseException as exc:
            with self.guard:
                job.update(status="failed", error=f"无法启动模型下载：{exc}")
            self._release(mid, operation)
            raise
        return job

    def cancel_download(self, mid):
        self.get(mid)  # Only manifest IDs are accepted, even when no job exists.
        response = None
        with self.guard:
            operation = self._operations.get(mid)
            job = self.jobs.get(mid)
            if operation and operation["kind"] == "download" and job["status"] in {"pending", "downloading", "verifying", "cancelling"}:
                operation["cancel"].set()
                response = operation.get("response")
                job.update(status="cancelling", message="正在取消下载；保留断点文件供下次续传")
            snapshot = dict(job) if job else {"status": "idle"}
        if response is not None:
            try:
                response.close()
            except (httpx.HTTPError, OSError):
                pass  # The worker still observes cancellation or its read timeout.
        return snapshot

    def _download(self, model, job, token, cancel=None, operation=None):
        cancel = cancel or threading.Event()

        def check_cancelled():
            if cancel.is_set():
                raise DownloadCancelled()

        def update(**values):
            with self.guard:
                check_cancelled()
                job.update(values)

        def file_hash(path):
            digest = hashlib.sha256()
            with path.open("rb") as source:
                while block := source.read(8 * 1024 * 1024):
                    check_cancelled()
                    digest.update(block)
            return digest.hexdigest()

        try:
            check_cancelled()
            folder = self.directory(model)
            folder.mkdir(parents=True, exist_ok=True)
            specs = model.get("files")
            if not specs:
                metadata = None
                errors = []
                manifest = json.loads(self.manifest.read_text("utf-8"))
                sources = manifest.get("metadata_sources", [])
                for source in sources:
                    try:
                        check_cancelled()
                        api = source["api"].format(repo=model["repo_id"])
                        headers = {"Authorization": f"Bearer {token}"} if token and urlparse(api).hostname == "huggingface.co" else {}
                        with httpx.Client(timeout=40, follow_redirects=True) as client:
                            response = client.get(api, headers=headers)
                            response.raise_for_status()
                            metadata = response.json()
                        check_cancelled()
                        if source["kind"] == "modelscope":
                            files = metadata.get("Data", {}).get("Files", [])
                            if not files:
                                raise ValueError("镜像没有返回文件清单")
                            metadata = {"sha": "master", "siblings": [{"rfilename": f["Path"], "size": f.get("Size", 0), "lfs": {"sha256": f.get("Sha256")}}
                                                                       for f in files if f.get("Type") != "tree"]}
                        elif not metadata.get("siblings"):
                            raise ValueError("模型仓库文件清单为空")
                        break
                    except DownloadCancelled:
                        raise
                    except Exception as exc:
                        metadata = None
                        errors.append(str(exc))
                if metadata is None:
                    raise ValueError("无法访问模型仓库。请配置可用镜像或导入本地模型。" + "; ".join(errors)[-400:])
                revision = metadata["sha"]
                specs = []
                for file in metadata.get("siblings", []):
                    check_cancelled()
                    name = file["rfilename"]
                    if any(fnmatch.fnmatch(name, p) for p in model["allow_patterns"]):
                        contained(folder, folder / name)
                        lfs = file.get("lfs") or {}
                        specs.append({"path": name, "size": file.get("size", lfs.get("size", 0)),
                                      "sha256": lfs.get("sha256"), "urls": [source["resolve"].format(repo=model["repo_id"], revision=revision, file=quote(name))]})
                if not specs:
                    raise ValueError("仓库中没有匹配 manifest 的模型文件")
                contained(folder, folder / "download-lock.json").write_text(json.dumps({"repo_id": model["repo_id"], "revision": revision, "files": specs}, indent=2), "utf-8")
            for spec in specs:
                contained(folder, folder / spec["path"])
            update(total=sum(x.get("size") or 0 for x in specs))
            if shutil.disk_usage(folder).free < job["total"]:
                raise ValueError("磁盘可用空间不足")
            for spec in specs:
                check_cancelled()
                destination = contained(folder, folder / spec["path"])
                destination.parent.mkdir(parents=True, exist_ok=True)
                update(file=spec["path"])
                if destination.is_file() and (not spec.get("size") or destination.stat().st_size == spec["size"]):
                    if spec.get("sha256") and file_hash(destination) == spec["sha256"]:
                        update(bytes=job["bytes"] + destination.stat().st_size)
                        continue
                urls = spec.get("urls", []) + [f"{m}/{model['repo_id']}/resolve/main/{quote(spec['path'])}" for m in self.settings["download_mirrors"]]
                partial = contained(folder, destination.with_name(destination.name + ".part"))
                last_error = None
                for url in urls:
                    try:
                        check_cancelled()
                        # A cancellation after the final byte may leave a complete
                        # partial. Validate it locally instead of issuing bytes=N-
                        # and receiving HTTP 416 for a file of length N.
                        if partial.is_file() and spec.get("size") and partial.stat().st_size == spec["size"]:
                            if not spec.get("sha256") or file_hash(partial) == spec["sha256"]:
                                size = partial.stat().st_size
                                with self.guard:
                                    check_cancelled()
                                    partial.replace(destination)
                                    job["bytes"] += size
                                last_error = None
                                break
                        if urlparse(url).scheme != "https":
                            raise ValueError("模型下载地址必须使用 HTTPS")
                        headers = {"Authorization": f"Bearer {token}"} if token and urlparse(url).hostname == "huggingface.co" else {}
                        offset = partial.stat().st_size if partial.exists() else 0
                        if offset:
                            headers["Range"] = f"bytes={offset}-"
                        update(status="downloading")
                        with httpx.Client(timeout=httpx.Timeout(60, read=120), follow_redirects=True) as client:
                            with client.stream("GET", url, headers=headers) as response:
                                with self.guard:
                                    check_cancelled()
                                    if operation is not None:
                                        operation["response"] = response
                                try:
                                    response.raise_for_status()
                                    check_cancelled()
                                    resume = response.status_code == 206 and response.headers.get("content-range", "").startswith(f"bytes {offset}-")
                                    if not resume:
                                        offset = 0
                                    digest = hashlib.sha256()
                                    if offset:
                                        with partial.open("rb") as old:
                                            while block := old.read(8 * 1024 * 1024):
                                                check_cancelled()
                                                digest.update(block)
                                    base = job["bytes"]
                                    with partial.open("ab" if resume else "wb") as out:
                                        for block in response.iter_bytes(1024 * 1024):
                                            check_cancelled()
                                            out.write(block)
                                            digest.update(block)
                                            offset += len(block)
                                            update(bytes=base + offset)
                                finally:
                                    with self.guard:
                                        if operation is not None and operation.get("response") is response:
                                            operation["response"] = None
                        update(status="verifying")
                        if spec.get("size") and offset != spec["size"]:
                            raise ValueError("模型文件长度不匹配")
                        if spec.get("sha256") and digest.hexdigest() != spec["sha256"]:
                            partial.unlink(missing_ok=True)
                            raise ValueError("模型 SHA256 校验失败")
                        # Cancellation and publication are ordered under guard.
                        with self.guard:
                            check_cancelled()
                            partial.replace(destination)
                        last_error = None
                        break
                    except DownloadCancelled:
                        raise
                    except Exception as exc:
                        check_cancelled()
                        update(bytes=sum(contained(folder, folder / s["path"]).stat().st_size for s in specs if contained(folder, folder / s["path"]).is_file()))
                        last_error = exc
                if last_error:
                    raise last_error
            if not self.ready(model):
                raise ValueError("下载后仍缺少入口或必要文件，请检查 manifest")
            update(status="complete")
        except DownloadCancelled:
            with self.guard:
                job.update(status="cancelled", message="下载已取消；断点文件已保留，再次下载将尝试续传")
        except Exception as exc:
            with self.guard:
                if cancel.is_set():
                    job.update(status="cancelled", message="下载已取消；断点文件已保留，再次下载将尝试续传")
                else:
                    job.update(status="failed", error=str(exc))
        finally:
            if operation is not None:
                self._release(model["id"], operation)

    def import_model(self, mid, directory):
        model = self.get(mid)
        with self.guard:
            operation = self._claim(model, "import")
        try:
            return self._import_model(model, directory)
        finally:
            self._release(mid, operation)

    def _import_model(self, model, directory):
        source = Path(directory).expanduser().resolve()
        if not source.is_dir():
            raise ValueError("导入路径必须是包含模型文件的目录")
        target = self.directory(model)
        if source == target:
            for spec in model.get("files", []):
                path = contained(target, target / spec["path"])
                if spec.get("sha256") and path.is_file() and sha256(path) != spec["sha256"]:
                    raise ValueError("导入模型 SHA256 不匹配")
            if not self.ready(model):
                raise ValueError("导入模型文件不完整")
            return {"ready": True}
        patterns = model.get("allow_patterns") or [s["path"] for s in model.get("files", [])]
        target.mkdir(parents=True, exist_ok=True)
        for item in source.rglob("*"):
            if item.is_file() and any(fnmatch.fnmatch(item.relative_to(source).as_posix(), p) for p in patterns):
                checked = contained(source, item)
                out = contained(target, target / item.relative_to(source))
                out.parent.mkdir(parents=True, exist_ok=True)
                partial = contained(target, out.with_name(out.name + ".importing"))
                if partial.exists() and not partial.is_file():
                    raise ValueError("模型导入临时路径必须是普通文件")
                shutil.copy2(checked, partial)
                spec = next((s for s in model.get("files", []) if s["path"] == item.relative_to(source).as_posix()), None)
                if spec and spec.get("sha256") and sha256(partial) != spec["sha256"]:
                    partial.unlink(missing_ok=True)
                    raise ValueError("导入模型 SHA256 不匹配，保留已有文件")
                partial.replace(out)
        for spec in model.get("files", []):
            path = contained(target, target / spec["path"])
            if spec.get("sha256") and path.is_file() and sha256(path) != spec["sha256"]:
                raise ValueError("导入模型 SHA256 不匹配")
        if not self.ready(model):
            raise ValueError("导入的文件不完整")
        return {"ready": True}

    def delete_model(self, mid):
        """Delete an optional model; the Workspace must first unload its runtime."""
        model = self.get(mid)
        if model.get("bundled") is not False:
            raise ValueError("内置基础模型不能删除；只允许删除非 bundled 的 manifest 模型")
        models_root = contained(self.settings.root, self.settings.data / "models")
        folder = contained(models_root, self.settings.root / model["path"])
        entries = self.entries()
        if sum(other["id"] == mid for other in entries) != 1:
            raise ValueError("manifest 模型 ID 重复，拒绝删除")
        for other in entries:
            if other["id"] == mid:
                continue
            other_folder = self.directory(other)
            if folder == other_folder or folder.is_relative_to(other_folder) or other_folder.is_relative_to(folder):
                raise ValueError("模型目录与其他 manifest 模型重叠，拒绝删除")
        with self.guard:
            operation = self._claim(model, "delete")
        try:
            if folder.exists():
                if not folder.is_dir():
                    raise ValueError("模型路径不是受控目录")
                # Inspect before removal so junctions/symlinks are never traversed.
                pending = [folder]
                while pending:
                    for path in pending.pop().iterdir():
                        checked = contained(folder, path)
                        if checked.is_dir():
                            pending.append(checked)
                contained(models_root, self.settings.root / model["path"])
                shutil.rmtree(folder)
            with self.guard:
                self.jobs.pop(mid, None)
            return {"deleted": True, "model_id": mid}
        finally:
            self._release(mid, operation)


class ModelRuntime:
    def __init__(self, settings, catalog):
        self.settings, self.catalog = settings, catalog
        self.process = None
        self.model_id = None
        self.port = None
        self.api_key = None
        self.log = None
        self.job = None
        self.last_used = time.monotonic()
        self.last_metrics = None
        self.last_load_seconds = None
        self.lock = threading.Lock()
        self._state = threading.RLock()
        self._memory_settings = None
        self._runtime_variant = None

    def apply_memory_settings(self):
        """A saved cap must not leave an already loaded model on its old cap."""
        limits = memory_settings(self.settings)
        with self._state:
            if self.process is not None and limits != self._memory_settings:
                job = self.job
                if job is not None:
                    job._stop_for_memory(self.process, "内存设置已更新，已释放旧预算下的模型；请重新发送以应用新预算")
                self.unload()

    def unload(self):
        # Clear shared state before terminating the child.  A cancellation
        # callback can call unload from another thread while ensure() is still
        # polling startup; clearing first prevents a second unload from
        # touching a newer model process.
        with self._state:
            job, process, log = self.job, self.process, self.log
            self.job, self.process, self.log = None, None, None
            self.model_id, self.port, self.api_key = None, None, None
            self._runtime_variant = None
        if job:
            job.close()
        if process is not None:
            terminate_tree(process)
        if log is not None:
            log.close()

    def ensure(self, model, cancel=None):
        model = request_model(model)
        variant = model.get("_runtime_variant", "default")
        load_started = time.monotonic()
        limits = memory_settings(self.settings)
        with self._state:
            current_id, current_process = self.model_id, self.process
        releasing_loaded_model = current_process is not None and current_process.poll() is None
        if (current_id == model["id"] and current_process and current_process.poll() is None
                and limits == self._memory_settings and variant == self._runtime_variant):
            self.last_load_seconds = 0.0
            return
        self.last_load_seconds = None
        self.unload()
        if cancel is not None and cancel.is_set():
            raise ValueError("模型请求已取消")
        if not self.catalog.ready(model):
            if model["role"] == "vision":
                raise ValueError("未下载视觉模型。请在模型中心下载或导入 27B 视觉模型和视觉投影文件后重试。")
            raise ValueError("模型尚未下载或文件不完整，请前往模型中心。")
        folder = self.catalog.directory(model)
        log = (self.settings.data / "logs/model.log").open("a", encoding="utf-8")
        process = job = None
        try:
            if model["backend"] == "llama":
                options = effective_options(self.settings, model)
                compatibility = cpu_compatibility.mode(model)
                binary = cpu_compatibility.binary(self.settings) if compatibility else self.settings.root / "runtime/llama/llama-server.exe"
                if not binary.exists():
                    raise ValueError("缺少内置 llama.cpp 运行库")
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    port = sock.getsockname()[1]
                api_key = secrets.token_urlsafe(32)
                # Budget includes a conservative KV/workspace reserve; 0 layers is CPU offload.
                weight_gb = (folder / model["entry"]).stat().st_size / 1024**3
                layers = min(self.settings["gpu_layers"], max(0, int((self.settings["gpu_memory_gb"] - 1) / max(weight_gb, 1) * 32)))
                profile = model.get("runtime_profile") or {}
                if compatibility or profile.get("force_cpu"):
                    layers = 0
                command = [str(binary), "-m", str(folder / model["entry"]), "--host", "127.0.0.1", "--port", str(port),
                           "-c", str(options["context_length"]), "-ngl", str(layers), "--parallel", "1",
                           "--threads", str(options["threads"]), "--threads-batch", str(options["threads_batch"]),
                           "--batch-size", str(options["batch_size"]), "--ubatch-size", str(options["ubatch_size"]),
                           "--api-key", api_key]
                if compatibility:
                    command += ["--webui", "none", "--jinja", "--reasoning", "off", "--no-warmup"]
                else:
                    command += ["--no-webui", "--offline"]
                    if profile.get("force_cpu"):
                        command += ["--device", "none", "--no-op-offload", "--fit", "off"]
                    if profile.get("no_warmup"):
                        command.append("--no-warmup")
                    if profile.get("flash_attention"):
                        command += ["--flash-attn", "on"]
                load_mode = (model.get("runtime_profile") or {}).get("load_mode")
                if not compatibility and load_mode in {"auto", "none", "mmap", "mlock", "mmap+mlock", "dio"}:
                    command += ["--load-mode", load_mode]
                if model.get("vision_entry"):
                    command += ["--mmproj", str(folder / model["vision_entry"])]
                    if profile.get("force_cpu"):
                        command += ["--mmproj-device", "none"]
                if model.get("draft_entry"):
                    draft_path = contained(folder, folder / model["draft_entry"])
                    if not draft_path.is_file():
                        raise ValueError("模型 MTP draft 文件缺失，请重新下载或导入完整模型。")
                    command += ["--model-draft", str(draft_path), "--spec-type", "draft-mtp",
                                "--spec-draft-n-max", str(max(1, int(model.get("speculative_tokens", 2)))),
                                "--spec-draft-threads", str(max(1, int(model.get("runtime_profile", {}).get("draft_threads", 2))))]
                model_env = cpu_compatibility.environment(os.environ.copy(), model, folder)
                torch_lib = self.settings.root / "runtime/python/Lib/site-packages/torch/lib"
                model_env["PATH"] = str(binary.parent) + os.pathsep + str(torch_lib) + os.pathsep + model_env.get("PATH", "")
                with self._state:
                    if cancel is not None and cancel.is_set():
                        raise ValueError("模型请求已取消")
                    # Keep launch and state publication atomic relative to
                    # unload(); cancellation can only observe a published child.
                    # Windows can signal process exit before all resident pages
                    # appear in Available RAM. Retry this transient switch only;
                    # retain the same caps and minimum launch requirements.
                    reclaim_started = time.monotonic()
                    reclaim_deadline = reclaim_started + (5 if releasing_loaded_model else 0)
                    reclaim_retries = 0
                    previous_resident = stable_since = None
                    while True:
                        if cancel is not None and cancel.is_set():
                            raise ValueError("模型请求已取消")
                        try:
                            budget = budget_for(self.settings)
                            if compatibility:
                                budget = cpu_compatibility.bounded_budget(budget, model)
                        except ValueError:
                            if time.monotonic() >= reclaim_deadline:
                                raise
                            previous_resident = stable_since = None
                        else:
                            now = time.monotonic()
                            if not releasing_loaded_model or now >= reclaim_deadline:
                                break
                            if previous_resident is not None and abs(budget.resident_bytes - previous_resident) <= 64 * 1024**2:
                                stable_since = now if stable_since is None else stable_since
                            else:
                                stable_since = None
                            previous_resident = budget.resident_bytes
                            if stable_since is not None and now - stable_since >= 0.3 and now - reclaim_started >= 0.5:
                                break
                        reclaim_retries += 1
                        time.sleep(0.1)
                    if reclaim_retries:
                        log.write(f"Memory reclaim after previous model exit: retries={reclaim_retries}\n")
                    log.write(f"\nMemory budget: commit={budget.limit_bytes}, resident={budget.resident_bytes}\n")
                    if compatibility:
                        log.write(f"CPU compatibility: mode={compatibility}, model={model['id']}, context={options['context_length']}\n")
                    log.flush()
                    activation_offset = log.tell()
                    process, job = start_owned(command, cwd=self.settings.root, stdout=log, stderr=log, creationflags=HIDDEN, env=model_env,
                                               **budget.launch_kwargs())
                    self.process, self.job, self.log = process, job, log
                    self._memory_settings = limits
                    self.port, self.api_key = port, api_key
                deadline = time.monotonic() + 300
                while time.monotonic() < deadline:
                    if cancel is not None and cancel.is_set():
                        self.unload()
                        raise ValueError("模型请求已取消")
                    if process.poll() is not None:
                        raise ValueError(getattr(job, "memory_error", None) or "模型启动失败，详情见 data/logs/model.log（也请检查内存预算）")
                    try:
                        if httpx.get(f"http://127.0.0.1:{port}/health", headers={"Authorization": f"Bearer {api_key}"}, timeout=2).status_code == 200:
                            if compatibility:
                                cpu_compatibility.verify_activation(self.settings.data / "logs/model.log", activation_offset, compatibility)
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.3)
                else:
                    self.unload()
                    raise ValueError("模型加载超时")
            elif model["backend"] == "airllm":
                # AirLLM's layer offload path depends on a CUDA device.  Fail
                # before creating a child process so a missing driver is clear
                # and cannot leave a long-lived worker or partial cache.
                profile = model.get("resource_profile") or {}
                if profile.get("requires_cuda", True):
                    estimate = resource_estimate(model)
                    cuda = next((item for item in estimate["checks"] if item["code"] == "cuda"), None)
                    if cuda and cuda["ok"] is False:
                        raise ValueError("AirLLM 分层推理需要 NVIDIA CUDA；当前未检测到可用 CUDA 设备。可改用 GGUF CPU 模型。")
                command = [str(self.settings.python), "-I", "-X", "utf8", "-u", "-m", "app.inference_worker", model["id"]]
                with self._state:
                    if cancel is not None and cancel.is_set():
                        raise ValueError("模型请求已取消")
                    process, job = start_owned(command, cwd=self.settings.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                               stderr=log, text=True, encoding="utf-8", creationflags=HIDDEN,
                                               env=child_environment(self.settings, self.settings.data / "offload"),
                                               **budget_for(self.settings).launch_kwargs())
                    self.process, self.job, self.log = process, job, log
                    self._memory_settings = limits
            else:
                raise ValueError("此模型不是聊天模型")
        except BaseException as exc:
            try:
                if not log.closed:
                    log.write("\nModel startup stopped: " + str(getattr(job, "memory_error", None) or exc) + "\n")
                    log.flush()
            except (OSError, ValueError):
                pass  # Concurrent cancellation may already have closed this log.
            with self._state:
                published = self.log is log
                if published:
                    self.unload()
            if not published:
                log.close()
            if getattr(job, "memory_error", None):
                raise ValueError(job.memory_error) from exc
            raise
        with self._state:
            if self.process is process:
                self.model_id = model["id"]
                self._runtime_variant = variant
                self.last_load_seconds = round(time.monotonic() - load_started, 3)

    def stream(self, model, messages, cancel=None):
        has_images = any(isinstance(row.get("content"), list) and
                         any(part.get("type") == "image_url" for part in row["content"])
                         for row in messages)
        model = request_model(model, vision=has_images)
        requested_at = time.monotonic()
        self.last_metrics = None
        self.last_load_seconds = None
        self.ensure(model, cancel=cancel)
        with self._state:
            process, port, api_key, job = self.process, self.port, self.api_key, self.job
        if process is None or (cancel is not None and cancel.is_set()):
            raise ValueError("模型请求已取消")
        self.last_used = time.monotonic()
        first_token_at = None
        generated_tokens = None
        prompt_tokens = None
        backend_timings = None
        native_window = NativeCounterWindow()
        try:
            if model["backend"] == "llama":
                with httpx.Client(timeout=httpx.Timeout(20, read=cpu_compatibility.read_timeout(model))) as client:
                    with client.stream("POST", f"http://127.0.0.1:{port}/v1/chat/completions", headers={"Authorization": f"Bearer {api_key}"}, json={
                            "model": model["id"], "messages": messages, "stream": True,
                            "stream_options": {"include_usage": True},
                            "timings_per_token": True,
                            "max_tokens": effective_options(self.settings, model)["max_new_tokens"], "temperature": 0.7,
                            "chat_template_kwargs": {"enable_thinking": False}}) as response:
                        if response.is_error:
                            response.read()
                            raise ValueError("模型请求失败：" + response.text[-500:])
                        for line in response.iter_lines():
                            if cancel is not None and cancel.is_set():
                                raise ValueError("模型请求已取消")
                            if line.startswith("data: ") and line != "data: [DONE]":
                                event = json.loads(line[6:])
                                if "error" in event:
                                    raise ValueError(str(event["error"]))
                                usage = event.get("usage") or {}
                                if usage.get("completion_tokens") is not None:
                                    generated_tokens = int(usage["completion_tokens"])
                                if usage.get("prompt_tokens") is not None:
                                    prompt_tokens = int(usage["prompt_tokens"])
                                if event.get("timings"):
                                    backend_timings = event["timings"]
                                    tokens = backend_timings.get("predicted_n")
                                    milliseconds = backend_timings.get("predicted_ms")
                                    per_second = backend_timings.get("predicted_per_second")
                                    native_window.observe(tokens, time.monotonic())
                                    self.last_metrics = {
                                        "generated_tokens": int(tokens) if tokens is not None else generated_tokens,
                                        "first_token_seconds": round(first_token_at - requested_at, 3) if first_token_at else None,
                                        "generation_seconds": float(milliseconds) / 1000 if milliseconds is not None else None,
                                        "tokens_per_second": float(per_second) if per_second is not None and per_second > 0 else None,
                                        "tokens_per_second_source": "native_backend",
                                        **native_window.metrics(),
                                    }
                                delta = (event.get("choices") or [{}])[0].get("delta") or {}
                                if delta.get("content"):
                                    first_token_at = first_token_at or time.monotonic()
                                    yield delta["content"]
            else:
                if cancel is not None and cancel.is_set():
                    raise ValueError("模型请求已取消")
                process.stdin.write(json.dumps({"messages": messages, "max_new_tokens": self.settings["max_new_tokens"]}, ensure_ascii=False) + "\n")
                process.stdin.flush()
                events = queue.Queue(maxsize=32)
                def read():
                    try:
                        for line in process.stdout:
                            while cancel is None or not cancel.is_set():
                                try:
                                    events.put(line, timeout=0.1)
                                    break
                                except queue.Full:
                                    continue
                    finally:
                        # End-of-stream remains visible even when no further
                        # stdout arrives after a cancelled or failed child.
                        while cancel is None or not cancel.is_set():
                            try:
                                events.put(None, timeout=0.1)
                                break
                            except queue.Full:
                                continue
                reader = threading.Thread(target=read, daemon=True)
                reader.start()
                deadline = time.monotonic() + 600
                while True:
                    if cancel is not None and cancel.is_set():
                        self.unload()
                        raise ValueError("模型请求已取消")
                    try:
                        line = events.get(timeout=0.1)
                    except queue.Empty:
                        if time.monotonic() >= deadline:
                            self.unload()
                            raise ValueError("模型推理超时")
                        continue
                    deadline = time.monotonic() + 600
                    if line is None:
                        raise ValueError("模型进程已退出，请查看日志")
                    event = json.loads(line)
                    if "error" in event:
                        raise ValueError(event["error"])
                    if "delta" in event:
                        first_token_at = first_token_at or time.monotonic()
                        yield event["delta"]
                    if "metrics" in event:
                        self.last_metrics = dict(event["metrics"])
                        if self.last_metrics.get("tokens_per_second") is not None:
                            self.last_metrics["tokens_per_second_source"] = "worker_generated_ids"
                        # A tokenizer may buffer text until a full word is
                        # available. Wake the workspace for metrics-only events.
                        yield ""
                    if event.get("done"):
                        break
                # One worker per generation keeps cancellation and stdout ownership unambiguous.
                self.unload()
            if getattr(job, "memory_error", None):
                raise ValueError(job.memory_error)
        except Exception as exc:
            if getattr(job, "memory_error", None):
                with self._state:
                    if self.process is process:
                        self.unload()
                raise ValueError(job.memory_error) from exc
            raise
        finally:
            self.last_used = time.monotonic()
            if model["backend"] == "llama":
                elapsed = max(0, self.last_used - (first_token_at or self.last_used))
                metrics = {"generated_tokens": generated_tokens,
                           "first_token_seconds": round(first_token_at - requested_at, 3) if first_token_at else None,
                           "generation_seconds": round(elapsed, 3), "tokens_per_second": None}
                if backend_timings and backend_timings.get("predicted_n") is not None:
                    metrics["generated_tokens"] = int(backend_timings["predicted_n"])
                    if backend_timings.get("predicted_ms") is not None:
                        metrics["generation_seconds"] = float(backend_timings["predicted_ms"]) / 1000
                    if backend_timings.get("predicted_per_second") is not None:
                        metrics["tokens_per_second"] = float(backend_timings["predicted_per_second"])
                        metrics["tokens_per_second_source"] = "native_backend"
                metrics.update(native_window.metrics())
                # Usage totals do not reveal how many tokens were in the
                # first visible chunk. Keep decode rate unavailable when
                # native timings are absent; whole-request rate stays valid.
                self.last_metrics = metrics
            if self.last_metrics is None:
                self.last_metrics = {}
            if prompt_tokens is not None:
                self.last_metrics["prompt_tokens"] = prompt_tokens
            if backend_timings and backend_timings.get("prompt_per_second") is not None:
                self.last_metrics["prompt_tokens_per_second"] = round(float(backend_timings["prompt_per_second"]), 4)
            self.last_metrics["model_ready_seconds"] = self.last_load_seconds
            self.last_metrics["request_seconds"] = time.monotonic() - requested_at
            tokens = self.last_metrics.get("generated_tokens")
            elapsed = self.last_metrics.get("request_seconds")
            if isinstance(tokens, int) and tokens > 0 and elapsed:
                self.last_metrics["whole_request_tokens_per_second"] = tokens / elapsed


def image_part(path):
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode("ascii")}}
