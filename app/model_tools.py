"""Explicit model discovery and hardware inspection, separate from inference."""
import json
import threading
import time
import platform
import re
import shutil
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import httpx
import psutil

from .config import contained
from .attachments import safe_name
from .models import sha256
from .performance import effective_options, resource_estimate, tune_cpu


def hardware(settings):
    memory = psutil.virtual_memory()
    result = {"cpu": platform.processor() or platform.machine(), "threads": psutil.cpu_count(),
              "ram_gb": round(memory.total / 1024**3, 1), "available_gb": round(memory.available / 1024**3, 1),
              "disk_free_gb": round(shutil.disk_usage(settings.root).free / 1024**3, 1), "gpus": []}
    import torch
    for index in range(torch.cuda.device_count()):
        card = torch.cuda.get_device_properties(index)
        result["gpus"].append({"name": card.name, "vram_gb": round(card.total_memory / 1024**3, 1)})
    return result


def resource_advice(report, models):
    notes = []
    for model in models:
        size = sum(spec.get("size", 0) for spec in model.get("files", [])) / 1024**3
        parts = []
        if model["backend"] == "airllm":
            parts.append("需要 NVIDIA CUDA；首次分层转换同时保留原权重和缓存")
        elif model.get("role") == "auxiliary":
            parts.append(f"权重约 {size:.2f} GiB，使用独立 CPU 进程提供辅助分类")
        elif size:
            state = "当前内存可能不足" if report["available_gb"] < size + 2 else "可尝试 CPU / GPU 分层推理"
            parts.append(f"权重约 {size:.2f} GiB，{state}；上下文缓存另占内存")
        if model.get("resource_profile"):
            estimate = resource_estimate(model, report)
            parts.append(estimate["message"])
            if model["resource_profile"].get("requires_cuda") is False:
                parts.append("不需要 CUDA，可使用 CPU 推理；实际速度取决于处理器、内存和磁盘")
            target = estimate.get("speed_target_tokens_per_second")
            if target:
                parts.append(f"优化目标 {target:g} token/s；运行速度校准可获取本机数据")
            if model.get("note"):
                parts.append(model["note"])
        if parts:
            notes.append(model["name"] + "：" + "；".join(parts).rstrip("。") + "。")
    return "\n".join(notes)


def calibrate(workspace, model_id, *, cancel=None, user_initiated=False):
    if not user_initiated:
        raise ValueError("速度校准必须由用户点击运行")
    model = workspace.catalog.get(model_id)
    if model["role"] != "chat" or not workspace.catalog.ready(model):
        raise ValueError("请先选择已下载的文字聊天模型")
    runtime = workspace.runtime
    if not runtime.lock.acquire(False):
        raise ValueError("模型正在使用，请稍后校准")
    maximum = workspace.settings["max_new_tokens"]
    started = time.monotonic()
    completed = threading.Event()
    def watch_cancel():
        while not completed.wait(0.1):
            if cancel is not None and cancel.is_set():
                runtime.unload()
                return
    monitor = threading.Thread(target=watch_cancel, daemon=True)
    monitor.start()
    try:
        workspace.settings.values["max_new_tokens"] = 32
        for _ in runtime.stream(model, [{"role": "user", "content": "用简短文字介绍太阳系的八颗行星。"}], cancel=cancel):
            if cancel is not None and cancel.is_set():
                raise ValueError("速度校准已取消")
        if not runtime.last_metrics or not runtime.last_metrics.get("generated_tokens"):
            raise ValueError("推理后端没有提供可用于校准的 token 统计")
        report = {"model": model_id, "time": time.time(), "seconds": round(time.monotonic() - started, 3),
                  "metrics": dict(runtime.last_metrics), "hardware": hardware(workspace.settings),
                  "inference": effective_options(workspace.settings, model)}
        path = workspace.settings.data / "model-benchmarks.json"
        rows = json.loads(path.read_text("utf-8")) if path.exists() else {}
        rows[model_id] = report
        staged = path.with_suffix(".tmp")
        staged.write_text(json.dumps(rows, ensure_ascii=False, indent=2), "utf-8")
        staged.replace(path)
        return report
    finally:
        workspace.settings.values["max_new_tokens"] = maximum
        completed.set()
        monitor.join(timeout=2)
        try:
            runtime.unload()
        finally:
            runtime.lock.release()


class ModelDiscovery:
    def __init__(self, settings, catalog):
        self.settings, self.catalog = settings, catalog
        self.path = settings.data / "model-catalog.json"

    def _json(self, endpoint, params=None):
        errors = []
        for host in self.settings["download_mirrors"]:
            if not host.startswith("https://"):
                continue
            try:
                with httpx.Client(timeout=30, follow_redirects=True) as client:
                    with client.stream("GET", host.rstrip("/") + endpoint, params=params) as response:
                        response.raise_for_status()
                        if response.url.scheme != "https":
                            raise ValueError("模型目录不能跳转到非 HTTPS 地址")
                        content = bytearray()
                        for chunk in response.iter_bytes(65536):
                            content.extend(chunk)
                            if len(content) > 10 * 1024**2:
                                raise ValueError("模型清单过大")
                        return json.loads(content), host.rstrip("/")
            except (ValueError, httpx.HTTPError) as exc:
                errors.append(str(exc))
        raise ValueError("无法访问模型目录，请检查网络或使用本地导入：" + "; ".join(errors)[-300:])

    def search(self, query):
        if not query.strip() or len(query) > 200:
            raise ValueError("请输入 1–200 字的模型名称")
        rows, _ = self._json("/api/models", {"search": query.strip(), "filter": "gguf", "limit": 30})
        return [row["id"] for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)]

    def files(self, repo):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repo):
            raise ValueError("模型仓库名称无效")
        data, host = self._json("/api/models/" + repo, {"blobs": "true"})
        revision = data.get("sha")
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40,64}", revision):
            raise ValueError("仓库未提供固定版本，不能校验下载来源")
        result = []
        for item in data.get("siblings", []):
            name, lfs = item.get("rfilename", ""), item.get("lfs") or {}
            path = PurePosixPath(name)
            if not name.lower().endswith(".gguf") or path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
                continue
            # Split weights and vision projectors need a complete manifest.
            if re.search(r"-\d{5}-of-\d{5}\.gguf$", name) or "mmproj" in name.lower():
                continue
            digest, size = lfs.get("sha256"), item.get("size", lfs.get("size"))
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or not isinstance(size, int) or size <= 0:
                continue
            result.append({"path": name, "size": size, "sha256": digest,
                           "urls": [f"{host}/{repo}/resolve/{revision}/{quote(name)}"],
                           "repo_id": repo, "license": (data.get("cardData") or {}).get("license", "请核对上游许可证")})
        return result

    def register(self, spec):
        name = safe_name(PurePosixPath(spec["path"]).name)
        if not re.fullmatch(r"[0-9a-f]{64}", spec["sha256"]):
            raise ValueError("模型必须提供 SHA256")
        mid = "custom-" + spec["sha256"][:16]
        quant = re.search(r"(?:IQ|Q|F|BF)\d[\w_]*", name, re.I)
        model = {"id": mid, "name": name.removesuffix(".gguf"), "role": "chat", "backend": "llama",
                 "repo_id": spec.get("repo_id", "local/import"), "quantization": quant.group(0) if quant else "GGUF",
                 "bundled": False, "license": spec.get("license", "本地文件，请核对来源许可证"),
                 "path": f"data/models/{mid}", "entry": name,
                 "note": "用户添加的 GGUF；模型结构与聊天模板兼容性取决于文件本身。",
                 "files": [{**{key: value for key, value in spec.items() if key in {"size", "sha256", "urls"}}, "path": name}]}
        with self.catalog.guard:
            rows = json.loads(self.path.read_text("utf-8")) if self.path.exists() else []
            existing = next((row for row in rows if row["id"] == mid), None)
            if existing:
                return existing
            else:
                rows.append(model)
                staged = self.path.with_suffix(".tmp")
                staged.write_text(json.dumps(rows, ensure_ascii=False, indent=2), "utf-8")
                staged.replace(self.path)
        return model

    def import_file(self, source):
        source = Path(source).resolve(strict=True)
        if source.suffix.lower() != ".gguf" or not source.is_file():
            raise ValueError("请选择 GGUF 模型文件")
        with source.open("rb") as stream:
            if stream.read(4) != b"GGUF":
                raise ValueError("文件没有 GGUF 标识")
        if re.search(r"-\d{5}-of-\d{5}\.gguf$", source.name) or "mmproj" in source.name.lower():
            raise ValueError("分片或视觉模型请通过完整 manifest 导入")
        spec = {"path": source.name, "size": source.stat().st_size, "sha256": sha256(source), "urls": []}
        model = self.register(spec)
        with self.catalog.guard:
            operation = self.catalog._claim(model, "import")
        try:
            target = self.catalog.directory(model)
            target.mkdir(parents=True, exist_ok=True)
            destination = contained(target, target / model["entry"])
            if source != destination:
                if shutil.disk_usage(target).free < spec["size"]:
                    raise ValueError("磁盘空间不足")
                staged = contained(target, destination.with_suffix(".gguf.importing"))
                if staged.exists() and not staged.is_file():
                    raise ValueError("模型导入临时路径必须是普通文件")
                try:
                    shutil.copy2(source, staged)
                    if sha256(staged) != spec["sha256"]:
                        raise ValueError("复制后 SHA256 不一致")
                    staged.replace(destination)
                finally:
                    staged.unlink(missing_ok=True)
        finally:
            self.catalog._release(model["id"], operation)
        return model


def ModelToolsDialog(owner):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit, QListWidget, QListWidgetItem, QFileDialog, QPlainTextEdit
    class Tools(QDialog):
        def __init__(self):
            super().__init__(owner)
            self.cancel = threading.Event()

        def closeEvent(self, event):
            self.cancel.set()
            event.accept()

        def reject(self):
            self.close()
    dialog = Tools()
    dialog.setWindowTitle("电脑配置与添加模型")
    dialog.resize(900, 650)
    layout = QVBoxLayout(dialog)
    info = QLabel("先使用包内的日常模型即可聊天。想了解电脑能运行哪些模型，点击“扫描硬件”；更大的模型需要更多空间，回答也可能更慢。")
    info.setWordWrap(True)
    info.setTextFormat(Qt.TextFormat.PlainText)
    layout.addWidget(info)
    row = QHBoxLayout()
    scan, local, benchmark = QPushButton("扫描硬件"), QPushButton("导入模型文件"), QPushButton("测试回答速度")
    local.setToolTip("从电脑中添加 .gguf 模型文件；图片模型请使用模型管理里的完整模型。")
    benchmark.setToolTip("用一个短问题测试当前模型，不写入聊天记录。大模型可能需要较长时间，关闭此窗口可取消。")
    tune = QPushButton("寻找更快的设置")
    tune.setToolTip("测试不同处理器线程数，为当前模型保存实测更快的设置。结果仍受电脑和问题长度影响。")
    tune.setObjectName("tuneCpuThreads")
    row.addWidget(scan)
    row.addWidget(local)
    row.addWidget(benchmark)
    row.addWidget(tune)
    layout.addLayout(row)
    advice = QPlainTextEdit()
    advice.setObjectName("modelResourceAdvice")
    advice.setReadOnly(True)
    advice.setMaximumHeight(130)
    advice.setPlaceholderText("点击“扫描硬件”查看电脑配置和各模型的运行建议。")
    layout.addWidget(advice)
    query = QLineEdit()
    search_help = QLabel("添加其他模型（需联网）：1. 搜索名称　2. 双击结果查看文件　3. 选择文件并下载")
    search_help.setWordWrap(True)
    layout.addWidget(search_help)
    query.setPlaceholderText("输入模型名称，例如 Qwen；从 Hugging Face 搜索")
    search = QPushButton("联网搜索")
    row = QHBoxLayout()
    row.addWidget(query)
    row.addWidget(search)
    layout.addLayout(row)
    repos, files = QListWidget(), QListWidget()
    layout.addWidget(repos)
    layout.addWidget(files)
    download = QPushButton("下载所选模型文件")
    download.setEnabled(False)
    layout.addWidget(download)
    notice = QLabel("这里只添加单文件文字模型。想让 AI 看图片或视频，请到“模型管理”添加完整的看图模型。下载前请查看模型的使用许可。")
    notice.setToolTip("GGUF 是模型文件格式；只列出带 SHA256 校验值的完整单文件。分片模型与视觉模型通过模型清单导入。")
    notice.setTextFormat(Qt.TextFormat.PlainText)
    notice.setWordWrap(True)
    layout.addWidget(notice)
    discovery = ModelDiscovery(owner.settings, owner.workspace.catalog)
    controls = [scan, local, query, search, repos, files, download, benchmark, tune]
    def task(fn, result):
        for widget in controls:
            widget.setEnabled(False)
        notice.setText("正在处理…")
        def done():
            for widget in controls:
                widget.setEnabled(True)
            download.setEnabled(files.currentItem() is not None)
        job = owner.task(fn, result, finished=done)
        job.error.connect(notice.setText)
    def scan_result(report):
        gpu = "、".join(f"{g['name']} ({g['vram_gb']} GiB)" for g in report["gpus"]) or "没有可用于加速的 NVIDIA 显卡；文字聊天仍可使用处理器"
        info.setText(f"处理器：{report['cpu']} · {report['threads']} 线程\n内存：共 {report['ram_gb']} GiB，目前可用 {report['available_gb']} GiB\n显卡：{gpu}\n模型所在磁盘剩余：{report['disk_free_gb']} GiB")
        guidance = resource_advice(report, owner.workspace.catalog.entries())
        advice.setPlainText(guidance)
        info.setToolTip(guidance)
        notice.setText("内置日常模型可以使用处理器运行。AirLLM 模型需要 NVIDIA 显卡；70B 等大模型可能很慢，请先查看上方运行建议。")
    scan.clicked.connect(lambda: task(lambda _: hardware(owner.settings), scan_result))
    def do_benchmark(optimize=False):
        if owner.busy():
            notice.setText("请等待当前工作完成后校准")
            return
        model_id = owner.models.currentData()
        owner.maintenance_busy = True
        owner.update_controls()
        dialog.cancel = threading.Event()
        from .desktop import metrics_text
        for widget in controls:
            widget.setEnabled(False)
        notice.setText("正在实测不同 CPU 线程数，可能需要数分钟；关闭窗口可取消。" if optimize else
                       "正在用固定短问题校准当前模型，首次加载包含在总耗时内…关闭窗口可取消。")
        def done():
            owner.maintenance_busy = False
            owner.update_controls()
            for widget in controls:
                widget.setEnabled(True)
            download.setEnabled(files.currentItem() is not None)
        def result(report):
            if optimize:
                notice.setText(f"CPU 线程优化完成：{report['threads']} 线程，短基准生成速度 "
                               f"{report['tokens_per_second']:.2f} token/s。仅对当前模型生效；"
                               "实际对话速度受上下文和其他程序占用影响。")
            else:
                notice.setText(metrics_text(report["metrics"]) + f" · 含加载总耗时 {report['seconds']} 秒；结果已保存。")
        measure = tune_cpu if optimize else calibrate
        job = owner.task(lambda _: measure(owner.workspace, model_id, cancel=dialog.cancel, user_initiated=True),
                         result, finished=done)
        job.error.connect(notice.setText)
    benchmark.clicked.connect(lambda: do_benchmark(False))
    tune.clicked.connect(lambda: do_benchmark(True))
    def add_local():
        path, _ = QFileDialog.getOpenFileName(dialog, "选择电脑中的模型文件", "", "模型文件 (*.gguf)")
        if path:
            task(lambda _: discovery.import_file(path), lambda m: (owner.refresh_models(), notice.setText("已添加：" + m["name"])))
    local.clicked.connect(add_local)
    def searched(rows):
        repos.clear()
        files.clear()
        repos.addItems(rows)
        notice.setText(f"找到 {len(rows)} 个结果，双击其中一项查看可下载的文件。" if rows else "没有找到模型。请检查拼写，或试试更短的名称。")
    def do_search():
        text = query.text()
        task(lambda _: discovery.search(text), searched)
    search.clicked.connect(do_search)
    query.returnPressed.connect(do_search)
    def loaded(rows):
        files.clear()
        for spec in rows:
            item = QListWidgetItem(f"{spec['path']} · {spec['size'] / 1024**3:.2f} GiB · {spec['license']}")
            item.setData(Qt.ItemDataRole.UserRole, spec)
            files.addItem(item)
        notice.setText(f"有 {len(rows)} 个文件可下载。点击所需文件，再点击下方下载按钮。" if rows else "此结果没有可直接添加的完整文字模型文件，请选择其他搜索结果。")
    def select_repo(item):
        text = item.text()
        task(lambda _: discovery.files(text), loaded)
    repos.itemDoubleClicked.connect(select_repo)
    files.currentItemChanged.connect(lambda item, previous: download.setEnabled(item is not None))
    def start_download():
        if files.currentItem():
            spec = files.currentItem().data(Qt.ItemDataRole.UserRole)
            def start(_):
                model = discovery.register(spec)
                owner.workspace.catalog.download(model["id"])
                return model
            task(start, lambda m: (owner.refresh_models(), notice.setText("下载已开始，可在模型中心查看进度或取消：" + m["name"])))
    download.clicked.connect(start_download)
    return dialog
