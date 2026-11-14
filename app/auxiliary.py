import hashlib
import json
import tempfile
from pathlib import Path

from .config import child_environment
from .processes import bounded_run
from .memory_limits import budget_for


class Auxiliary:
    def __init__(self, settings, catalog):
        self.settings, self.catalog = settings, catalog
        self.cache = {}

    def classify(self, text, cancelled=None):
        if not self.settings["auxiliary_enabled"]:
            return {"available": False, "reason": "辅助模型已关闭"}
        try:
            spec = self.catalog.get("laya-0.4b")
        except ValueError:
            return {"available": False, "reason": "当前安装包只预置三款主模型，未包含辅助分类模型"}
        if not self.catalog.ready(spec):
            return {"available": False, "reason": "辅助模型文件缺失"}
        key = hashlib.sha256(text.encode()).hexdigest()
        if key in self.cache:
            return self.cache[key]
        with tempfile.TemporaryDirectory(dir=self.settings.data / "offload", prefix="aux-") as work:
            folder = Path(work)
            request = folder / "request.json"
            request.write_text(json.dumps({"text": text}, ensure_ascii=False), "utf-8")
            result = bounded_run([str(self.settings.python), "-I", "-X", "utf8", "-m", "app.auxiliary_worker", str(request)],
                                 cwd=self.settings.root, env=child_environment(self.settings, folder), timeout=60, limit=262144, cancelled=cancelled,
                                 **budget_for(self.settings).launch_kwargs())
        if result["returncode"] or result["reason"]:
            return {"available": False, "reason": result.get("memory_error") or (result["stderr"] or result["stdout"])[-300:]}
        try:
            data = json.loads(result["stdout"])
            answer = {"available": True, "task": data["answers"]["task"]["choice"]}
        except (ValueError, KeyError):
            return {"available": False, "reason": "辅助模型返回格式错误"}
        if len(self.cache) > 100:
            self.cache.clear()
        self.cache[key] = answer
        return answer
