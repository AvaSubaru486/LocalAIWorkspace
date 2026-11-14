"""Exercise native desktop controls, real parsers and Python in isolated data."""
import json
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path


class SmokeRuntime:
    """UI streaming fixture. Actual model inference is a separate release check."""
    def __init__(self):
        self.lock = threading.Lock()
        self.last_used = time.monotonic()
        self.model_id = None
        self.seen = []

    def stream(self, model, messages, **kwargs):
        self.model_id = model["id"]
        self.seen.append(messages)
        for word in ("桌面", "流式", "测试完成"):
            yield word

    def unload(self):
        self.model_id = None


def run(source_settings, report_path=None, screenshot=None):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QLabel, QPushButton
    from PIL import Image
    from .config import Settings
    from .desktop import STYLE, Window
    from .workspace import Workspace

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    checks, exceptions = [], []
    old_hook = sys.excepthook
    sys.excepthook = lambda *args: exceptions.append("".join(traceback.format_exception(*args)))
    report = {"ok": False, "checks": checks, "chat": "UI fixture; not a real-model test"}
    window = workspace = None

    def wait(predicate, timeout=45):
        deadline = time.monotonic() + timeout
        while not predicate():
            if exceptions:
                raise AssertionError(exceptions[0])
            if time.monotonic() > deadline:
                import faulthandler
                faulthandler.dump_traceback()
                raise AssertionError("desktop operation timed out: " + (window.status.text() if window else "initializing"))
            app.processEvents()
            QTest.qWait(20)
        app.processEvents()
        if exceptions:
            raise AssertionError(exceptions[0])

    try:
        with tempfile.TemporaryDirectory(prefix="localai-desktop-") as tmp:
            settings = Settings(Path(tmp))
            settings.root = source_settings.root
            settings.values.update(auxiliary_enabled=False, python_timeout_seconds=3)
            runtime = SmokeRuntime()
            workspace = Workspace(settings, runtime)
            workspace.catalog.ready = lambda model: model["role"] != "vision"
            window = Window(workspace, startup_check=False)
            window.show()
            QTest.qWait(200)
            assert window.isVisible()
            assert not window.python_panel.isVisible(), "Python workspace should be collapsed on startup"
            checks.append("beginner-friendly first launch keeps Python workspace collapsed")
            texts = [x.text() for cls in (QLabel, QPushButton) for x in window.findChildren(cls)]
            assert not any("计划模式" in text or "目标模式" in text for text in texts)
            assert not workspace.store.messages(window.sid), "Python must not execute on startup"
            checks.append("native window; ordinary chat only; no automatic Python")
            document = Path(tmp) / "notes.txt"
            document.write_text("离线附件测试：项目编号 desktop-42", "utf-8")
            window.add_files([str(document)])
            wait(lambda: not window.upload_busy)
            assert len(window.pending) == 1
            assert "desktop-42" in window.pending[0]["preview"]
            window.composer.setPlainText("读取附件中的项目编号")
            QTest.mouseClick(window.send_button, Qt.MouseButton.LeftButton)
            wait(lambda: not window.chat_busy)
            messages = workspace.store.messages(window.sid)
            assert messages[-1]["content"] == "桌面流式测试完成"
            assert "desktop-42" in json.dumps(runtime.seen[-1], ensure_ascii=False)
            assert not window.pending and not window.composer.toPlainText()
            checks.append("attachment parser, chat streaming fixture, saved history, cleared composer")
            window.composer.setPlainText("接着上一条继续")
            QTest.mouseClick(window.send_button, Qt.MouseButton.LeftButton)
            wait(lambda: not window.chat_busy)
            assert len(runtime.seen[-1]) >= 4
            checks.append("conversation history reaches inference")
            if screenshot:
                Path(screenshot).parent.mkdir(parents=True, exist_ok=True)
                assert window.grab().save(str(screenshot))
            window.toggle_python()
            QTest.mouseClick(window.run_button, Qt.MouseButton.LeftButton)
            wait(lambda: not window.python_busy)
            assert "运行完成" in window.output.toPlainText(), window.output.toPlainText()
            files = [x for x in workspace.store.attachments(window.sid) if x["kind"] == "generated"]
            assert files and "你好，离线 Python" in Path(files[0]["path"]).read_text("utf-8")
            checks.append("manual bundled Python, Chinese stdout, file returned to conversation")
            image = Path(tmp) / "picture.png"
            Image.new("RGB", (120, 80), "#245d46").save(image)
            window.add_files([str(image)])
            wait(lambda: not window.upload_busy)
            assert window.pending and window.pending[0]["kind"] == "image"
            window.composer.setPlainText("描述图片")
            QTest.mouseClick(window.send_button, Qt.MouseButton.LeftButton)
            wait(lambda: not window.chat_busy)
            assert "视觉模型" in window.status.text(), window.status.text()
            assert window.composer.toPlainText() == "描述图片" and len(window.pending) == 1
            checks.append("missing vision model is explicit; input retained")
            window.remove_pending(window.pending[0])
            assert not window.pending
            window.composer.clear()
            checks.append("attachment removal")
            window.code.setPlainText("raise RuntimeError('desktop exception')")
            QTest.mouseClick(window.run_button, Qt.MouseButton.LeftButton)
            wait(lambda: not window.python_busy)
            assert "RuntimeError: desktop exception" in window.output.toPlainText()
            checks.append("exception traceback in Python panel")
            window.code.setPlainText("import time\ntime.sleep(30)")
            window.timeout.setValue(1)
            QTest.mouseClick(window.run_button, Qt.MouseButton.LeftButton)
            wait(lambda: not window.python_busy)
            assert "已超时终止" in window.output.toPlainText()
            checks.append("Python timeout from desktop")
            assistant = window.add_message("assistant", "```python\nprint('from assistant')\n```")
            load_button = assistant.findChild(QPushButton, "loadPython")
            assert load_button is not None
            QTest.mouseClick(load_button, Qt.MouseButton.LeftButton)
            assert window.code.toPlainText() == "print('from assistant')"
            assert not window.python_busy
            checks.append("assistant Python can be copied into workspace without auto-run")
            window.open_models()
            assert window.model_dialog.isVisible()
            assert window.settings_hub.isVisible()
            assert {"runtime", "models", "model_tools", "project", "project_tasks", "skills", "knowledge"}.issubset(window.settings_hub.pages)
            window.model_dialog.close()
            checks.append("settings hub exposes model and workspace tools")
            window.close()
            wait(lambda: not window.isVisible() and not window.jobs)
            workspace.close()
            checks.append("window closes and background work finishes")
            report["ok"] = True
    except Exception:
        report["ok"] = False
        report["error"] = traceback.format_exc()
    finally:
        if workspace:
            workspace.close()
        if window:
            window.close()
            deadline = time.monotonic() + 20
            while window.jobs and time.monotonic() < deadline:
                app.processEvents()
                QTest.qWait(20)
            app.processEvents()
        sys.excepthook = old_hook
    if report_path:
        path = Path(report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if report["ok"] else 1
