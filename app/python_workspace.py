from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from pathlib import Path

from .attachments import safe_name
from .config import child_environment, contained
from .processes import bounded_run


class PythonWorkspace:
    def __init__(self, settings, store, attachments):
        self.settings, self.store, self.attachments = settings, store, attachments
        self.lock = threading.Lock()
        self._guard = threading.RLock()
        self._active: set[threading.Event] = set()
        self._closed = False

    def run(self, sid, code, timeout, cancel=None, stdin_queue=None, on_output=None):
        self.store.session(sid)
        if not code.strip() or len(code.encode("utf-8")) > 200000:
            raise ValueError("代码为空或超过 200KB")
        cancel = cancel if cancel is not None else threading.Event()
        if not hasattr(cancel, "is_set") or not hasattr(cancel, "set"):
            raise TypeError("cancel 必须是可设置的事件对象")
        if cancel.is_set():
            raise ValueError("Python 任务已取消")
        if not self.lock.acquire(blocking=False):
            raise ValueError("已有 Python 任务正在运行，请稍后再试")
        with self._guard:
            if self._closed:
                self.lock.release()
                raise RuntimeError("Python 工作区正在关闭")
            self._active.add(cancel)
        run_id = uuid.uuid4().hex
        folder = None
        try:
            folder = contained(self.settings.data / "sessions", self.settings.data / "sessions" / sid / "runs" / run_id)
            folder.mkdir(parents=True)
            inputs = folder / "attachments"
            outputs = folder / "outputs"
            inputs.mkdir()
            outputs.mkdir()
            inventory = []
            input_size = 0
            for row in self.store.attachments(sid):
                source = contained(self.attachments.directory(sid), Path(row["path"]))
                if not source.is_file():
                    continue
                # IDs prevent duplicate filenames from overwriting one another.
                target = inputs / (row["id"][:8] + "_" + safe_name(row["name"]))
                shutil.copy2(source, target)
                input_size += target.stat().st_size
                inventory.append({"id": row["id"], "name": row["name"], "path": str(target)})
            (folder / "attachments.json").write_text(json.dumps(inventory, ensure_ascii=False, indent=2), "utf-8")
            script = folder / "main.py"
            script.write_text(code, "utf-8")
            env = child_environment(self.settings, folder)
            env.update(WORKSPACE_ATTACHMENTS=str(inputs), WORKSPACE_OUTPUTS=str(outputs),
                       WORKSPACE_ATTACHMENT_MANIFEST=str(folder / "attachments.json"))
            result = bounded_run([str(self.settings.python), "-I", "-X", "utf8", "-u", str(script)], cwd=folder, env=env,
                                 timeout=min(max(1, timeout), self.settings["python_timeout_seconds"]),
                                 limit=self.settings["python_output_bytes"],
                                 directory_limit=input_size + self.settings["generated_total_bytes"],
                                 memory_limit=self.settings["python_memory_gb"] * 1024**3,
                                 cancelled=cancel, stdin_queue=stdin_queue, on_output=on_output)
            if result["reason"] == "cancelled":
                result.update(run_id=run_id, files=[], workdir=str(folder), message_id=None)
                return result
            generated, total = [], 0
            try:
                contained(folder, outputs)
                output_paths = outputs.rglob("*")
            except ValueError:
                output_paths = []
                result["stderr"] += "\n结果目录不能替换为符号链接或目录联接。"
            for path in output_paths:
                if not path.is_file():
                    continue
                try:
                    path = contained(folder, path)
                    path = contained(outputs, path)
                    size = path.stat().st_size
                    if size > self.settings["generated_file_bytes"] or total + size > self.settings["generated_total_bytes"] or len(generated) >= 30:
                        raise ValueError("生成文件超过大小或数量限制")
                    total += size
                    aid = uuid.uuid4().hex
                    target = self.attachments.directory(sid) / aid
                    target.mkdir()
                    copy = target / "result.bin"
                    shutil.copy2(path, copy)
                    row = {"id": aid, "session": sid, "name": safe_name(path.name), "kind": "generated", "size": size,
                           "path": str(copy), "text": "", "meta": {"run_id": run_id}, "created": time.time()}
                    self.store.save_attachment(row)
                    generated.append(self.attachments.public(row))
                except ValueError as exc:
                    result["stderr"] += f"\n{path.name}: {exc}"
            result.update(run_id=run_id, files=generated, workdir=str(folder))
            summary = f"Python 运行完成（{result['seconds']} 秒，退出码 {result['returncode']}）"
            if result["reason"]:
                summary += "：" + {"timeout": "已超时终止", "output_limit": "输出达到上限", "disk_limit": "工作目录达到大小上限", "cancelled": "已取消"}.get(result["reason"], result["reason"])
            result["message_id"] = self.store.add_message(sid, "tool", summary, [f["id"] for f in generated])
            return result
        finally:
            # Published outputs were copied into the session attachment store.
            try:
                if folder is not None and folder.exists():
                    shutil.rmtree(contained(self.settings.data / "sessions", folder), ignore_errors=True)
            except (OSError, ValueError):
                # A user script can alter files in its working directory.
                # If containment fails, leave that path alone but always
                # release the execution lock so the workspace remains usable.
                pass
            finally:
                with self._guard:
                    self._active.discard(cancel)
                self.lock.release()

    def close(self):
        """Cancel the active user process and wait for its execution lock."""
        with self._guard:
            self._closed = True
            active = list(self._active)
        for cancel in active:
            cancel.set()
        # bounded_run observes the event and owns process-tree termination.
        # Waiting on the lock gives callers a deterministic close boundary.
        if active:
            deadline = time.monotonic() + 10
            while self.lock.locked() and time.monotonic() < deadline:
                time.sleep(0.02)
