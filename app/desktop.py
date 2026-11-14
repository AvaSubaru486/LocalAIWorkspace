"""Native offline desktop; workers call Workspace directly without a web UI."""
from __future__ import annotations

import json
import queue
import re
import shutil
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QLockFile, Qt, QTimer, Signal, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QKeySequence, QPixmap, QShortcut, QTextCharFormat, QSyntaxHighlighter, QTextDocument, QTextCursor, QTextBlockFormat, QTextTableFormat
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QSpinBox, QSplitter, QTabWidget, QTextBrowser, QVBoxLayout, QWidget, QProgressBar, QGraphicsOpacityEffect, QSizePolicy,
)

from .config import Settings, contained
from .workspace import Workspace

TITLE = "Local AI Workspace · 离线桌面"
STYLE = """
QWidget { color:#262626; font-family:'Microsoft YaHei UI'; font-size:13px; }
QMainWindow, QDialog { background:#fafaf9; }
QFrame#sidebar { background:#f1f1ef; border-right:1px solid #e5e5e3; }
QLabel#brand { font-size:20px; font-weight:700; }
QLabel#eyebrow { color:#737373; font-size:11px; font-weight:600; }
QLabel#headline { font-size:25px; font-weight:700; }
QLabel#muted { color:#737373; }
QPushButton { background:#fff; border:1px solid #e1e1df; border-radius:8px; padding:8px 13px; }
QPushButton:hover { background:#f1f1ef; border-color:#c9c9c6; }
QPushButton:disabled { color:#a3a3a3; background:#f2f2f0; }
QPushButton#primary, QPushButton#runPython { background:#30302f; color:white; border-color:#30302f; font-weight:600; }
QPushButton#primary:hover { background:#484846; }
QPushButton#danger { color:#9d4234; }
QPlainTextEdit, QTextBrowser, QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
 background:#fff; border:1px solid #e1e1df; border-radius:7px; padding:7px; selection-background-color:#d8d8d5; }
QPlainTextEdit#code, QPlainTextEdit#output { font-family:Consolas,'Microsoft YaHei UI'; font-size:13px; }
QPlainTextEdit#code { background:#fafaf9; }
QPlainTextEdit#output { background:#f2f2f0; }
QListWidget { border:0; background:transparent; outline:0; }
QListWidget::item { padding:10px; border-radius:7px; margin:2px 0; }
QListWidget::item:selected { background:#e2e2df; color:#262626; }
QListWidget::item:hover { background:#e9e9e6; }
QFrame#message { background:white; border:1px solid #e8e8e6; border-radius:10px; }
QFrame#attachment { background:#f4f4f2; border:1px solid #e5e5e3; border-radius:8px; }
QScrollArea { background:transparent; border:0; }
QScrollArea > QWidget > QWidget { background:transparent; }
QScrollBar:vertical { background:#f0f0ee; width:14px; border:0; margin:0; border-radius:7px; }
QScrollBar::handle:vertical { background:#b8b8b5; min-height:36px; border-radius:6px; margin:2px; }
QScrollBar::handle:vertical:hover { background:#858582; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background:transparent; }
QProgressBar#activity { background:#e8e8e6; border:0; border-radius:2px; }
QProgressBar#activity::chunk { background:#737373; border-radius:2px; }
QSplitter::handle { background:#e5e5e3; width:1px; }
QToolTip { background:white; color:#262626; border:1px solid #d4d4d1; padding:6px; }
"""
SAMPLE = '''from pathlib import Path
import json
import os

# 当前会话附件的名称和路径
manifest = Path(os.environ["WORKSPACE_ATTACHMENT_MANIFEST"])
files = json.loads(manifest.read_text("utf-8"))
for item in files:
    print(item["name"], item["path"])

# 写入 outputs 的文件会回传到对话区
out = Path(os.environ["WORKSPACE_OUTPUTS"])
(out / "result.txt").write_text("你好，离线 Python！", encoding="utf-8")
print("运行完成")
'''


def label(text, name=None):
    item = QLabel(text)
    item.setTextFormat(Qt.TextFormat.PlainText)
    item.setWordWrap(True)
    if name:
        item.setObjectName(name)
    return item


def button(text, action, name=None):
    item = QPushButton(text)
    if name:
        item.setObjectName(name)
    item.clicked.connect(action)
    return item


def size_label(size):
    if size < 1024:
        return f"{size} B"
    return f"{size / 1024**2:.1f} MB" if size >= 1024**2 else f"{size / 1024:.1f} KB"


def metrics_text(metrics):
    if not metrics:
        return ""
    parts = []
    if metrics.get("tokens_per_second") is not None:
        speed = metrics["tokens_per_second"]
        shown = f"{speed:.3f}" if 0 < speed < 1 else f"{speed:.1f}"
        parts.append(f"生成速度 {shown} token/s")
    if metrics.get("generated_tokens") is not None:
        parts.append(f"已生成 {metrics['generated_tokens']} tokens")
    if metrics.get("first_token_seconds") is not None:
        parts.append(f"首字 {metrics['first_token_seconds']:.2f} 秒")
    return "  ·  ".join(parts)


def model_choice_text(model):
    """Explain the beginner choices while retaining model IDs and detail names."""
    name = model["name"]
    if model.get("vision_entry"):
        return ("聊天与看图（内置） · " if model.get("bundled") else "聊天与看图 · ") + name.split(" · ", 1)[0]
    if model.get("bundled") and model.get("role") == "chat":
        return "日常聊天（内置） · " + name.split(" · ", 1)[0]
    if model.get("role") == "vision":
        return "看图与聊天 · " + name
    return name


def extract_python_blocks(text):
    """Return Python fenced blocks without Markdown markers.

    Model output remains text until the user explicitly chooses an action.  A
    language tag is preferred; untagged fenced blocks are accepted only when
    their contents look like Python, which keeps prose code examples out of
    the workspace by default.
    """
    tagged = re.findall(r"```[ \t]*(?:python(?:3)?|py)[ \t]*\r?\n(.*?)```", text or "", flags=re.IGNORECASE | re.DOTALL)
    if tagged:
        return [block.strip("\r\n") for block in tagged if block.strip()]
    blocks = re.findall(r"```[ \t]*\r?\n(.*?)```", text or "", flags=re.DOTALL)
    candidates = []
    for block in blocks:
        stripped = block.strip()
        if stripped and re.search(r"(?:^|\n)[ \t]*(?:from |import |def |class |if __name__|print\(|for |while |with )", stripped):
            candidates.append(stripped)
    return candidates


def clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().deleteLater()


class Task(QObject):
    result = Signal(object)
    error = Signal(str)
    event = Signal(object)
    finished = Signal()

    def __init__(self, function, parent):
        super().__init__(parent)
        self.function = function

    def start(self):
        def work():
            try:
                self.result.emit(self.function(self.event.emit))
            except Exception as exc:
                self.error.emit(str(exc))
                traceback.print_exc()
            finally:
                self.finished.emit()
        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()


class CodeHighlighter(QSyntaxHighlighter):
    def highlightBlock(self, text):
        import re
        for pattern, color in [(r"\b(?:import|from|for|in|if|else|elif|while|def|class|return|with|as|try|except|raise|True|False|None|print)\b", "#8a4584"),
                               (r'"[^"\\]*(?:\\.[^"\\]*)*"|\x27[^\x27\\]*(?:\\.[^\x27\\]*)*\x27', "#47864f"), (r"#.*$", "#87968c")]:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            for match in re.finditer(pattern, text):
                self.setFormat(match.start(), match.end() - match.start(), fmt)


class Composer(QPlainTextEdit):
    send = Signal()
    files = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.send_shortcut = "ctrl_enter"

    def keyPressEvent(self, event):
        is_enter = event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        if is_enter:
            modifiers = event.modifiers()
            if self.send_shortcut == "enter":
                if not modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier):
                    if modifiers & Qt.KeyboardModifier.ShiftModifier:
                        super().keyPressEvent(event)
                    else:
                        self.send.emit()
                    return
            elif modifiers & Qt.KeyboardModifier.ControlModifier:
                self.send.emit()
                return
        super().keyPressEvent(event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and all(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            self.files.emit([url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()])
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


class CodeEditor(QPlainTextEdit):
    """Python editor that unwraps copied Markdown code fences on paste."""

    def insertFromMimeData(self, source):
        text = source.text()
        blocks = extract_python_blocks(text)
        if blocks:
            self.setPlainText(blocks[0])
            if len(blocks) > 1 and callable(getattr(self, "on_multiple_python_blocks", None)):
                self.on_multiple_python_blocks(len(blocks))
        else:
            super().insertFromMimeData(source)


class ConversationScroll(QScrollArea):
    user_scrolled = Signal()

    def wheelEvent(self, event):
        self.user_scrolled.emit()
        super().wheelEvent(event)


class MessageBody(QTextBrowser):
    """Rich text is selectable, but scrolling belongs to the conversation."""
    def __init__(self, conversation):
        super().__init__()
        self.conversation = conversation
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.document().setDefaultStyleSheet("p {margin:6px 0;} pre {background:#edf3ed; padding:10px; font-family:Consolas;} code {font-family:Consolas;} table {border-collapse:collapse;} td,th {border:1px solid #ceddce; padding:6px;}")

    def loadResource(self, resource_type, url):
        # Model output must not load remote images or arbitrary local files.
        return QPixmap()

    def wheelEvent(self, event):
        self.conversation.user_scrolled.emit()
        bar = self.conversation.verticalScrollBar()
        delta = event.pixelDelta().y() or round(event.angleDelta().y() / 120 * 72)
        bar.setValue(bar.value() - delta)
        event.accept()


class Message(QFrame):
    def __init__(self, role, text, files, owner, created=None, metrics=None):
        super().__init__()
        self.owner = owner
        self.role = role
        self.raw_text = text
        self.setObjectName("message")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        heading = QHBoxLayout()
        heading.addWidget(label({"user": "你", "assistant": "LOCAL AI", "tool": "PYTHON 运行结果"}.get(role, role), "eyebrow"))
        heading.addStretch()
        heading.addWidget(label(datetime.fromtimestamp(created or time.time()).strftime("%m-%d %H:%M"), "muted"))
        copy = button("复制", lambda: QApplication.clipboard().setText(self.raw_text))
        copy.setFixedHeight(28)
        heading.addWidget(copy)
        layout.addLayout(heading)
        self.body = MessageBody(owner.scroll)
        self.body.setOpenLinks(False)
        self.body.setOpenExternalLinks(False)
        self.body.setStyleSheet("background:transparent; border:0; padding:0;")
        self.body.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body.document().documentLayout().documentSizeChanged.connect(lambda _: self.fit())
        layout.addWidget(self.body)
        self.python_actions = QHBoxLayout()
        self.python_actions.setContentsMargins(0, 4, 0, 0)
        layout.addLayout(self.python_actions)
        for row in files:
            layout.addWidget(owner.attachment_card(row, removable=False))
        self.metrics_label = label(metrics_text(metrics), "muted")
        self.metrics_label.setToolTip("token 是 AI 处理文字的小片段，不一定等于一个汉字。token/s 表示每秒生成多少片段；等待首字的时间单独显示。")
        self.metrics_label.setVisible(bool(metrics_text(metrics)))
        layout.addWidget(self.metrics_label)
        self.set_text(text)

    def set_text(self, text):
        self.raw_text = text
        if self.role == "assistant":
            self.body.document().setMarkdown(text, QTextDocument.MarkdownFeature.MarkdownDialectGitHub | QTextDocument.MarkdownFeature.MarkdownNoHTML)
            document = self.body.document()
            block = document.begin()
            tables = set()
            while block.isValid():
                cursor = QTextCursor(block)
                fmt = block.blockFormat()
                fmt.setLineHeight(140, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
                if fmt.nonBreakableLines():
                    fmt.setBackground(QColor("#edf3ed"))
                cursor.setBlockFormat(fmt)
                table = cursor.currentTable()
                if table is not None and table not in tables:
                    tables.add(table)
                    table_format = table.format()
                    table_format.setBorder(1)
                    table_format.setBorderBrush(QColor("#c9d9cc"))
                    table_format.setCellPadding(6)
                    table_format.setCellSpacing(0)
                    table.setFormat(table_format)
                block = block.next()
        else:
            self.body.setPlainText(text)
        self.update_python_actions()
        self.fit()

    def update_python_actions(self):
        clear_layout(self.python_actions)
        if self.role != "assistant":
            return
        blocks = extract_python_blocks(self.raw_text)
        if not blocks:
            return
        self.python_actions.addWidget(label("检测到 Python 代码", "muted"))
        for index, code in enumerate(blocks, start=1):
            suffix = f" {index}" if len(blocks) > 1 else ""
            copy = button("复制 Python" + suffix, lambda checked=False, code=code: QApplication.clipboard().setText(code))
            copy.setObjectName("copyPython")
            self.python_actions.addWidget(copy)
            load = button("加入工作区" + suffix, lambda checked=False, code=code: self.owner.load_python_code(code))
            load.setObjectName("loadPython")
            self.python_actions.addWidget(load)
        self.python_actions.addStretch()

    def set_metrics(self, metrics):
        self.metrics_label.setText(metrics_text(metrics))
        self.metrics_label.setVisible(bool(metrics_text(metrics)))

    def fit(self):
        height = max(30, int(self.body.document().size().height()) + 10)
        if self.body.height() != height:
            self.body.setFixedHeight(height)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self, self.fit)


class ModelDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.setWindowTitle("模型中心")
        self.resize(690, 660)
        layout = QVBoxLayout(self)
        layout.addWidget(label("模型与资源", "headline"))
        layout.addWidget(label("基础模型已内置。其他模型可在联网时下载，或从本机目录导入。", "muted"))
        self.tools_button = button("查看电脑配置或添加其他模型", owner.open_model_tools)
        layout.addWidget(self.tools_button)
        current = QHBoxLayout()
        self.loaded_state = label("", "muted")
        current.addWidget(self.loaded_state, 1)
        self.unload_button = button("释放模型内存", owner.unload_model)
        self.unload_button.setToolTip("关闭当前模型进程并释放内存，已下载的模型文件仍会保留。")
        self.unload_button.setObjectName("unloadModel")
        current.addWidget(self.unload_button)
        layout.addLayout(current)
        self.token = QLineEdit()
        self.token.setPlaceholderText("仅部分模型需要：粘贴 Hugging Face 下载授权码，本次使用后不保存")
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.token)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        self.rows = QVBoxLayout(content)
        scroll.setWidget(content)
        layout.addWidget(scroll)
        self.states = {}
        self.frames = {}
        self.rows.addStretch()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.setInterval(800)
        self.refresh()

    def add_model(self, model):
        frame = QFrame()
        frame.setObjectName("message")
        box = QVBoxLayout(frame)
        box.addWidget(label(model["name"], "eyebrow"))
        box.addWidget(label(model.get("note", "") + "\n许可证：" + model.get("license", "请查看模型说明")))
        state = label("")
        box.addWidget(state)
        controls = QHBoxLayout()
        download = button("下载", lambda checked=False, mid=model["id"]: self.download(mid))
        importer = button("导入目录", lambda checked=False, mid=model["id"]: self.import_model(mid))
        cancel = button("取消下载", lambda checked=False, mid=model["id"]: self.cancel_download(mid))
        delete = button("删除模型", lambda checked=False, mid=model["id"]: self.delete_model(mid))
        for control in (download, importer, cancel, delete):
            controls.addWidget(control)
        controls.addStretch()
        box.addLayout(controls)
        self.rows.insertWidget(self.rows.count() - 1, frame)
        self.frames[model["id"]] = frame
        self.states[model["id"]] = (state, download, importer, cancel, delete)

    def showEvent(self, event):
        self.refresh()
        self.timer.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def refresh(self):
        mid = self.owner.workspace.runtime.model_id
        if mid:
            self.loaded_state.setText("当前已加载：" + self.owner.workspace.catalog.get(mid)["name"])
        else:
            self.loaded_state.setText("模型未运行；发送问题时会自动准备。")
        self.unload_button.setEnabled(bool(mid) and not self.owner.busy())
        models = self.owner.workspace.catalog.public()
        ids = {model["id"] for model in models}
        for removed in self.states.keys() - ids:
            frame = self.frames.pop(removed)
            self.rows.removeWidget(frame)
            frame.deleteLater()
            self.states.pop(removed)
        for model in models:
            if model["id"] not in self.states:
                self.add_model(model)
            state, download, importer, cancel, delete = self.states[model["id"]]
            job = model.get("download") or {}
            active = job.get("status") in {"pending", "downloading", "verifying", "cancelling"}
            if active:
                message = "正在取消下载…" if job.get("status") == "cancelling" else f"正在下载 / 校验：{size_label(job.get('bytes', 0))} / {size_label(job.get('total', 0))}\n{job.get('file', '')}"
            elif job.get("status") == "failed":
                message = "下载失败：" + job.get("error", "未知错误")
            elif job.get("status") == "cancelled":
                message = "下载已取消；再次下载可继续未完成文件"
            else:
                message = "已就绪 · 可离线使用" if model["ready"] else "未下载"
            state.setText(message)
            download.setEnabled(not active and not model["ready"])
            importer.setEnabled(not active and not self.owner.busy())
            cancel.setVisible(active)
            cancel.setEnabled(active and job.get("status") != "cancelling")
            delete.setVisible(not model.get("bundled", True))
            # Cancelled or failed downloads also leave resumable files on disk.
            delete.setEnabled(not active and not model.get("bundled", True)
                              and self.owner.workspace.catalog.directory(model).is_dir()
                              and not self.owner.busy())

    def download(self, mid):
        try:
            self.owner.workspace.catalog.download(mid, self.token.text().strip() or None)
            self.refresh()
        except Exception as exc:
            self.owner.show_error(str(exc))

    def cancel_download(self, mid):
        try:
            self.owner.workspace.catalog.cancel_download(mid)
            self.refresh()
        except Exception as exc:
            self.owner.show_error(str(exc))

    def delete_model(self, mid):
        if self.owner.busy():
            return
        model = self.owner.workspace.catalog.get(mid)
        if model.get("bundled", True):
            self.owner.show_error("内置基础模型不能从模型中心删除")
            return
        if QMessageBox.question(self, "删除模型", "删除已下载的“" + model["name"] + "”模型文件？再次使用时需要重新下载。") != QMessageBox.StandardButton.Yes:
            return
        self.owner.maintenance_busy = True
        self.owner.update_controls()
        self.owner.status.setText("正在卸载并删除模型…")
        self.refresh()
        def remove(_):
            with self.owner.workspace.runtime.lock:
                if self.owner.workspace.runtime.model_id == mid:
                    self.owner.workspace.runtime.unload()
                return self.owner.workspace.catalog.delete_model(mid)
        def finished():
            self.owner.maintenance_busy = False
            self.owner.update_controls()
            if not self.owner.closing:
                self.refresh()
                self.owner.refresh_models()
        self.owner.task(remove, result=lambda _: self.owner.status.setText("模型文件已删除"), finished=finished)

    def import_model(self, mid):
        folder = QFileDialog.getExistingDirectory(self, "选择包含模型文件的目录")
        if folder:
            self.owner.importing = True
            self.owner.update_controls()
            self.owner.status.setText("正在导入并校验模型…")
            self.owner.task(lambda _: self.owner.workspace.catalog.import_model(mid, folder),
                            lambda _: self.owner.status.setText("模型导入完成"), finished=self.owner.import_finished)


class SettingsHub(QDialog):
    """One settings surface for runtime preferences and workspace tools."""

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.setObjectName("settingsHub")
        self.setWindowTitle("设置与工具")
        self.resize(1320, 850)
        self.setMinimumSize(1020, 680)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("settingsTabs")
        layout.addWidget(self.tabs)
        self.pages = {}

        # Keep the existing dialog objects and their owner callbacks.  They
        # are placed in tabs so every tool remains reachable from one surface,
        # while the Window attributes continue to support older integrations.
        runtime = owner._build_runtime_settings_page(self)
        self.runtime_buttons = getattr(runtime, "buttons", None)
        self.pages["runtime"] = self.add_page("常用设置", runtime)
        for key, title, factory in (
            ("models", "模型管理", owner._ensure_model_dialog),
            ("model_tools", "电脑与模型", owner._ensure_model_tools_dialog),
            ("project", "文件与项目", owner._ensure_project_dialog),
            ("project_tasks", "项目任务", owner._ensure_project_tasks_dialog),
            ("skills", "回答偏好", owner._ensure_skills_dialog),
            ("knowledge", "离线资料", owner._ensure_knowledge_dialog),
        ):
            page = factory()
            self.pages[key] = self.add_page(title, page)

    def add_page(self, title, widget):
        widget.setWindowFlags(Qt.WindowType.Widget)
        self.tabs.addTab(widget, title)
        return widget

    def findChild(self, child_type, name="", *args):
        # Older callers locate the runtime Save/Cancel box without an object
        # name. Other embedded tools may also contain button boxes, so make
        # the runtime control deterministic while preserving Qt lookup rules.
        if child_type is QDialogButtonBox and not name and self.runtime_buttons is not None:
            return self.runtime_buttons
        return super().findChild(child_type, name, *args)

    def activate(self, widget):
        index = self.tabs.indexOf(widget)
        if index < 0:
            return False
        self.tabs.setCurrentIndex(index)
        widget.show()
        return True

    def done(self, result):
        # Closing the settings surface must invoke each tool's existing draft
        # and cancellation handling. A busy tool may refuse to close.
        for index in range(self.tabs.count() - 1, -1, -1):
            page = self.tabs.widget(index)
            if isinstance(page, QDialog) and not page.close():
                self.tabs.setCurrentIndex(index)
                return
        if result == QDialog.DialogCode.Rejected:
            self.pages["runtime"].reset_settings()
        super().done(result)


class Window(QMainWindow):
    def __init__(self, workspace, startup_check=True):
        super().__init__()
        self.workspace = workspace
        self.settings = workspace.settings
        self.sid = None
        self.pending = []
        self.session_drafts = {}
        self.session_notices = {}
        self.chat_sid = None
        self.chat_message = ""
        self.chat_files = []
        self.chat_notice = ""
        self.chat_error = ""
        self.chat_model_notice = ""
        self.jobs = set()
        self.chat_busy = self.python_busy = self.upload_busy = self.importing = self.maintenance_busy = False
        self.closing = False
        self.follow_tail = True
        self.scroll_revision = 0
        self.cancel = threading.Event()
        self.python_cancel = threading.Event()
        self.python_input_queue = None
        self.python_last_stream = None
        self.python_streamed = {"stdout": "", "stderr": ""}
        self.python_input_closed = False
        self.assistant = None
        self.answer = ""
        self.answer_dirty = False
        # Keep the exact editor contents sent with the active request.  The
        # composer remains editable while a response streams, so completion
        # must not erase a draft the user typed for the next request.
        self.sent_composer_text = None
        self.model_dialog = None
        self.model_tools_dialog = None
        self.project_dialog = None
        self.skills_dialog = None
        self.knowledge_dialog = None
        self.project_tasks_dialog = None
        self.settings_hub = None
        self.check_button = None
        self.installation_report = None
        self.ui_path = self.settings.data / "desktop-ui.json"
        self.setWindowTitle(TITLE)
        self.setWindowIcon(QIcon(str(self.settings.root / "resources/app.ico")))
        self.resize(1400, 900)
        self.setMinimumSize(1020, 700)
        self.setAcceptDrops(True)
        shell = QWidget()
        self.setCentralWidget(shell)
        base = QHBoxLayout(shell)
        base.setContentsMargins(0, 0, 0, 0)
        base.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(238)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(18, 24, 18, 20)
        side.addWidget(label("LOCAL AI", "eyebrow"))
        side.addWidget(label("你的离线工作区", "brand"))
        side.addWidget(label("普通对话 · 本机文件 · Python", "muted"))
        side.addSpacing(22)
        self.new_button = button("＋  新建对话", self.new_session, "primary")
        side.addWidget(self.new_button)
        side.addSpacing(15)
        side.addWidget(label("最近对话", "eyebrow"))
        self.session_search = QLineEdit()
        self.session_search.setObjectName("sessionSearch")
        self.session_search.setPlaceholderText("搜索标题与消息")
        self.session_search.setClearButtonEnabled(True)
        self.session_search_timer = QTimer(self)
        self.session_search_timer.setSingleShot(True)
        self.session_search_timer.setInterval(180)
        self.session_search_timer.timeout.connect(self.refresh_sessions)
        self.session_search.textChanged.connect(lambda _: self.session_search_timer.start())
        side.addWidget(self.session_search)
        self.sessions = QListWidget()
        self.sessions.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sessions.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.sessions.setObjectName("sessions")
        self.sessions.itemClicked.connect(lambda item: self.select_session(item.data(Qt.ItemDataRole.UserRole)))
        side.addWidget(self.sessions, 1)
        self.delete_button = button("删除当前对话", self.delete_session)
        side.addWidget(self.delete_button)
        side.addSpacing(10)
        self.settings_button = button("设置与工具", self.open_settings)
        self.help_button = button("使用说明", self.open_guide)
        self.help_button.setObjectName("helpGuide")
        side.addWidget(self.help_button)
        side.addWidget(self.settings_button)
        # Compatibility entry points retained for older integrations and UI
        # tests. The visible entry is the single settings/tools surface.
        self.model_button = QPushButton("模型中心", self)
        self.model_button.clicked.connect(self.open_models)
        self.project_button = QPushButton("项目工具", self)
        self.project_button.clicked.connect(self.open_project)
        self.skills_button = QPushButton("技能", self)
        self.skills_button.clicked.connect(self.open_skills)
        self.knowledge_button = QPushButton("知识库", self)
        self.knowledge_button.clicked.connect(self.open_knowledge)
        for compat in (self.model_button, self.project_button, self.skills_button, self.knowledge_button):
            compat.hide()
        side.addSpacing(14)
        side.addWidget(label("离线运行\n文件与对话保存在资源目录中", "muted"))
        base.addWidget(sidebar)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        base.addWidget(self.splitter, 1)
        chat = QWidget()
        column = QVBoxLayout(chat)
        column.setContentsMargins(36, 25, 36, 20)
        top = QHBoxLayout()
        top.addWidget(label("普通对话", "headline"))
        top.addStretch()
        self.python_toggle = button("打开 Python 工作区", self.toggle_python)
        top.addWidget(self.python_toggle)
        column.addLayout(top)
        model_row = QHBoxLayout()
        model_row.addWidget(label("模型", "muted"))
        self.models = QComboBox()
        self.models.setToolTip("不确定时，使用“日常聊天（内置）”即可。有图片或视频的对话会自动使用看图模型，实际使用的模型会显示在生成进度旁。")
        self.models.setMinimumWidth(210)
        model_row.addWidget(self.models, 1)
        self.model_state = label("", "muted")
        model_row.addWidget(self.model_state)
        self.models.currentIndexChanged.connect(self.model_changed)
        column.addLayout(model_row)
        self.scroll = ConversationScroll()
        self.scroll.setWidgetResizable(True)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setMinimumHeight(100)
        self.messages_widget = QWidget()
        self.messages = QVBoxLayout(self.messages_widget)
        self.messages.setContentsMargins(0, 12, 0, 12)
        self.messages.setSpacing(12)
        self.messages.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll.setWidget(self.messages_widget)
        column.addWidget(self.scroll, 1)
        self.scroll_animation = QPropertyAnimation(self.scroll.verticalScrollBar(), b"value", self)
        self.scroll_animation.setDuration(140)
        self.scroll_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.scroll.user_scrolled.connect(self.manual_scroll)
        self.scroll.verticalScrollBar().sliderPressed.connect(self.manual_scroll)
        self.jump_button = button("↓ 回到最新消息", lambda: self.scroll_bottom(force=True))
        self.jump_button.setVisible(False)
        column.addWidget(self.jump_button, 0, Qt.AlignmentFlag.AlignRight)
        self.scroll.verticalScrollBar().valueChanged.connect(self.scroll_position_changed)
        self.scroll.verticalScrollBar().rangeChanged.connect(self.scroll_position_changed)
        self.activity = QProgressBar()
        self.activity.setObjectName("activity")
        self.activity.setRange(0, 0)
        self.activity.setTextVisible(False)
        self.activity.setFixedHeight(3)
        self.activity.setVisible(False)
        column.addWidget(self.activity)
        self.pending_widget = QWidget()
        self.pending_layout = QVBoxLayout(self.pending_widget)
        self.pending_layout.setContentsMargins(0, 0, 0, 0)
        self.pending_scroll = QScrollArea()
        self.pending_scroll.setWidgetResizable(True)
        self.pending_scroll.setWidget(self.pending_widget)
        self.pending_scroll.setMaximumHeight(180)
        self.pending_scroll.hide()
        column.addWidget(self.pending_scroll)
        self.composer = Composer()
        self.composer.setObjectName("composer")
        self.composer.setPlaceholderText("输入问题，或将图片、视频和文件拖到这里…")
        self.composer.setFixedHeight(104)
        self.composer.send.connect(self.send)
        self.composer.files.connect(self.add_files)
        column.addWidget(self.composer)
        controls = QHBoxLayout()
        self.attach_button = button("＋ 添加附件", self.choose_files)
        controls.addWidget(self.attach_button)
        self.shortcut_hint = label("", "muted")
        controls.addWidget(self.shortcut_hint)
        self.apply_send_shortcut()
        controls.addStretch()
        self.cancel_button = button("停止生成", self.stop_chat, "danger")
        controls.addWidget(self.cancel_button)
        self.send_button = button("发送  ↑", self.send, "primary")
        controls.addWidget(self.send_button)
        column.addLayout(controls)
        self.status = label("准备就绪", "muted")
        self.status.setMinimumHeight(22)
        column.addWidget(self.status)
        self.generation_info = label("", "muted")
        self.generation_info.setToolTip("token/s 是每秒生成的文字片段数，越高越快；一个片段不一定等于一个汉字。这里显示后端提供的实际统计。")
        self.generation_info.setVisible(False)
        column.addWidget(self.generation_info)
        self.splitter.addWidget(chat)
        self.python_panel = QWidget()
        self.python_panel.setMinimumWidth(310)
        panel = QVBoxLayout(self.python_panel)
        panel.setContentsMargins(20, 25, 20, 20)
        panel.addWidget(label("PYTHON", "eyebrow"))
        panel.addWidget(label("编写、运行、查看结果", "brand"))
        panel.addWidget(label("使用内置 Python；仅在你点击运行后执行。", "muted"))
        row = QHBoxLayout()
        row.addWidget(button("打开 .py", self.open_code))
        row.addWidget(button("保存代码", self.save_code))
        row.addStretch()
        panel.addLayout(row)
        self.code = CodeEditor(SAMPLE)
        self.code.on_multiple_python_blocks = lambda count: self.status.setText(
            f"剪贴板有 {count} 段 Python 代码，已导入第 1 段；可在回答中逐段选择。")
        self.code.setObjectName("code")
        self.code.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.code.setTabStopDistance(32)
        self.highlighter = CodeHighlighter(self.code.document())
        panel.addWidget(self.code, 3)
        run_row = QHBoxLayout()
        run_row.addWidget(label("超时"))
        self.timeout = QSpinBox()
        self.timeout.setRange(1, min(300, self.settings["python_timeout_seconds"]))
        self.timeout.setValue(min(30, self.timeout.maximum()))
        self.timeout.setSuffix(" 秒")
        run_row.addWidget(self.timeout)
        run_row.addStretch()
        self.run_button = button("运行代码", self.run_python, "runPython")
        run_row.addWidget(self.run_button)
        self.python_stop_button = button("停止", self.stop_python, "danger")
        self.python_stop_button.setObjectName("stopPython")
        self.python_stop_button.setVisible(False)
        run_row.addWidget(self.python_stop_button)
        panel.addLayout(run_row)
        input_row = QHBoxLayout()
        self.python_input = QLineEdit()
        self.python_input.setObjectName("pythonInput")
        self.python_input.setMaxLength(8192)
        self.python_input.setPlaceholderText("程序需要你回答时，在这里输入…")
        self.python_input.setToolTip("运行含 input() 的代码后，在这里填写回答。按 Enter 或点击“发送输入”提交。")
        self.python_input.returnPressed.connect(self.send_python_input)
        panel.addWidget(self.python_input)
        input_row.addStretch()
        self.python_send_button = button("发送输入", self.send_python_input)
        self.python_send_button.setObjectName("sendPythonInput")
        input_row.addWidget(self.python_send_button)
        self.python_eof_button = button("结束输入", self.end_python_input)
        self.python_eof_button.setToolTip("告诉程序你已没有更多内容要输入。")
        self.python_eof_button.setObjectName("endPythonInput")
        input_row.addWidget(self.python_eof_button)
        panel.addLayout(input_row)
        panel.addWidget(label("运行结果", "eyebrow"))
        self.output = QPlainTextEdit()
        self.output.setObjectName("output")
        self.output.setReadOnly(True)
        self.output.setPlaceholderText("点击“运行代码”后，结果会显示在这里。出错时也会显示原因和位置。")
        panel.addWidget(self.output, 2)
        panel.addWidget(label("参考示例把结果存入输出文件夹，完成后可在对话中点击“保存”。", "muted"))
        self.splitter.addWidget(self.python_panel)
        self.splitter.setSizes([780, 410])
        self.python_panel.hide()
        self.splitter.setCollapsible(0, False)
        self.splitter.setCollapsible(1, False)
        self.stream_timer = QTimer(self)
        self.stream_timer.timeout.connect(self.flush_answer)
        self.stream_timer.start(70)
        self.activity_timer = QTimer(self)
        self.activity_timer.timeout.connect(self.update_generation_info)
        self.activity_timer.start(200)
        self.housekeeper = QTimer(self)
        self.housekeeper.timeout.connect(self.housekeeping)
        self.housekeeper.start(300000)
        self.refresh_models()
        existing = self.workspace.store.sessions()
        try:
            state = json.loads(self.ui_path.read_text("utf-8"))
        except (OSError, ValueError):
            state = {}
        sid = state.get("session")
        ids = {s["id"] for s in existing}
        self.select_session(sid if sid in ids else existing[0]["id"] if existing else self.workspace.store.new_session()["id"])
        self.update_controls()
        QShortcut(QKeySequence("Ctrl+N"), self, activated=self.new_session)
        if startup_check:
            QTimer.singleShot(150, self, self.check_installation)

    def busy(self):
        return self.chat_busy or self.python_busy or self.upload_busy or self.importing or self.maintenance_busy or self.closing

    def navigation_busy(self):
        return self.python_busy or self.upload_busy or self.importing or self.maintenance_busy or self.closing

    def task(self, function, result=None, event=None, finished=None, error=None):
        job = Task(function, self)
        self.jobs.add(job)
        if result:
            job.result.connect(result, Qt.ConnectionType.QueuedConnection)
        if event:
            job.event.connect(event, Qt.ConnectionType.QueuedConnection)
        job.error.connect(error or self.show_error, Qt.ConnectionType.QueuedConnection)
        def end():
            self.jobs.discard(job)
            if finished:
                finished()
            job.deleteLater()
        job.finished.connect(end, Qt.ConnectionType.QueuedConnection)
        job.start()
        return job

    def show_error(self, message):
        self.status.setText("错误：" + message)
        self.status.setToolTip(message)

    def update_controls(self):
        idle = not self.busy()
        for widget in (self.delete_button, self.models,
                       self.attach_button, self.send_button, self.run_button):
            widget.setEnabled(idle)
        for widget in (self.new_button, self.session_search, self.sessions):
            widget.setEnabled(not self.navigation_busy())
        # A running answer owns the current session and model, but the text
        # editor is independent: let the user prepare the next prompt while
        # keeping send()/attachment handlers guarded by their busy checks.
        self.composer.setEnabled(not (self.python_busy or self.upload_busy or self.importing
                                      or self.maintenance_busy or self.closing))
        # Streaming owns the inference runtime but leaves independent tools
        # available. Model maintenance and Python execution remain gated by
        # the existing busy checks to avoid sharing the session lock.
        tools_available = not (self.python_busy or self.upload_busy or self.importing
                               or self.maintenance_busy or self.closing)
        self.settings_button.setEnabled(tools_available)
        self.cancel_button.setVisible(self.chat_busy)
        self.cancel_button.setEnabled(self.chat_busy and not self.cancel.is_set())
        self.send_button.setText("正在回答…" if self.chat_busy and self.sid == self.chat_sid else
                                 "等待后台回答" if self.chat_busy else "发送  ↑")
        self.cancel_button.setText("停止回答" if self.sid == self.chat_sid else "停止后台回答")
        self.run_button.setText("正在运行…" if self.python_busy else "运行代码")
        self.python_stop_button.setVisible(self.python_busy)
        self.python_stop_button.setEnabled(self.python_busy and not self.python_cancel.is_set())
        accepting_input = self.python_busy and not self.python_input_closed and not self.python_cancel.is_set()
        self.python_input.setEnabled(accepting_input)
        self.python_send_button.setEnabled(accepting_input)
        self.python_eof_button.setEnabled(accepting_input)
        self.activity.setVisible(self.busy() and not self.closing)
        self.code.setReadOnly(self.python_busy or self.closing)
        self.pending_widget.setEnabled(idle)

    def refresh_sessions(self, *args):
        query = self.session_search.text().strip().casefold()
        matched = set()
        if query:
            with self.workspace.store.connect() as db:
                matched = {row["session"] for row in db.execute("SELECT session,content FROM messages")
                           if query in row["content"].casefold()}
        self.sessions.clear()
        for session in self.workspace.store.sessions():
            if query and query not in session["title"].casefold() and session["id"] not in matched:
                continue
            title = session["title"] + (" · 正在回答" if self.chat_busy and session["id"] == self.chat_sid else "")
            item = QListWidgetItem(title)
            item.setToolTip(session["title"])
            item.setData(Qt.ItemDataRole.UserRole, session["id"])
            self.sessions.addItem(item)
            if session["id"] == self.sid:
                self.sessions.setCurrentItem(item)

    def select_session(self, sid):
        if self.navigation_busy() or not sid or sid == self.sid:
            return
        try:
            if self.sid:
                self.session_drafts[self.sid] = {"text": self.composer.toPlainText(), "pending": list(self.pending)}
            self.sid = sid
            draft = self.session_drafts.get(sid, {})
            self.composer.setPlainText(draft.get("text", ""))
            self.pending = list(draft.get("pending", []))
            try:
                preferences = json.loads(self.ui_path.read_text("utf-8"))
                if not isinstance(preferences, dict):
                    preferences = {}
            except (OSError, ValueError):
                preferences = {}
            self.ui_path.write_text(json.dumps(preferences | {"session": sid}), "utf-8")
            self.render_history()
            self.render_pending()
            self.refresh_sessions()
            self.status.setText((self.chat_notice if sid == self.chat_sid else "另一段对话正在后台回答，你可以查看历史或准备下一条问题。") if self.chat_busy else self.session_notices.get(sid, "准备就绪"))
            self.generation_info.setVisible(self.chat_busy)
            self.update_controls()
        except Exception as exc:
            self.show_error(str(exc))

    def new_session(self):
        if not self.navigation_busy():
            self.session_search.clear()
            self.select_session(self.workspace.store.new_session()["id"])

    def delete_session(self):
        if self.busy():
            return
        if QMessageBox.question(self, "删除对话", "删除当前对话及其附件？此操作无法撤销。") != QMessageBox.StandardButton.Yes:
            return
        try:
            self.workspace.delete_session(self.sid)
            self.session_drafts.pop(self.sid, None)
            self.session_notices.pop(self.sid, None)
            self.pending.clear()
            self.sid = None
            self.composer.clear()
            rows = self.workspace.store.sessions()
            self.select_session(rows[0]["id"] if rows else self.workspace.store.new_session()["id"])
        except Exception as exc:
            self.show_error(str(exc))

    def render_history(self, scroll_to_end=True):
        history = self.workspace.history(self.sid)
        files = {row["id"]: row for row in history["attachments"]}
        clear_layout(self.messages)
        self.assistant = None
        active = self.chat_busy and self.sid == self.chat_sid
        if not history["messages"] and not active:
            welcome = QWidget()
            layout = QVBoxLayout(welcome)
            layout.setContentsMargins(20, 65, 20, 50)
            layout.addWidget(label("从一个问题开始", "headline"))
            layout.addWidget(label("例如：帮我写一封邮件、解释一个知识点，或读一份文件。\n图片与视频抽样帧会使用内置 27B 模型。", "muted"))
            self.messages.addWidget(welcome)
        for message in history["messages"]:
            self.add_message(message["role"], message["content"], [files[x] for x in message["attachments"] if x in files],
                             created=message["created"], metrics=message.get("metrics"))
        if active:
            added = [message for message in history["messages"] if message["id"] not in self.previous_message_ids]
            if not any(message["role"] == "user" for message in added):
                self.add_message("user", self.chat_message, self.chat_files)
            if not any(message["role"] == "assistant" for message in added):
                self.assistant = self.add_message("assistant", self.answer or "正在准备…", metrics=self.live_metrics)
                self.answer_dirty = False
        if scroll_to_end:
            self.scroll_bottom(force=True)

    def add_message(self, role, content, files=(), created=None, metrics=None, animate=False):
        message = Message(role, content, files, self, created, metrics)
        self.messages.addWidget(message)
        if animate and not self.closing:
            effect = QGraphicsOpacityEffect(message)
            message.setGraphicsEffect(effect)
            animation = QPropertyAnimation(effect, b"opacity", message)
            animation.setDuration(180)
            animation.setStartValue(.25)
            animation.setEndValue(1.0)
            animation.setEasingCurve(QEasingCurve.Type.OutCubic)
            animation.finished.connect(lambda: message.setGraphicsEffect(None))
            animation.start()
        return message

    def scroll_bottom(self, force=False):
        if force:
            self.scroll_revision += 1
            self.follow_tail = True
        revision = self.scroll_revision
        def scroll():
            if self.closing or revision != self.scroll_revision or (self.chat_busy and not self.follow_tail and not force):
                return
            bar = self.scroll.verticalScrollBar()
            self.scroll_animation.stop()
            if force or not self.chat_busy:
                bar.setValue(bar.maximum())
            else:
                self.scroll_animation.setStartValue(bar.value())
                self.scroll_animation.setEndValue(bar.maximum())
                self.scroll_animation.start()
        QTimer.singleShot(30, self, scroll)

    def manual_scroll(self):
        self.scroll_revision += 1
        self.scroll_animation.stop()
        self.follow_tail = False

    def scroll_position_changed(self, *args):
        bar = self.scroll.verticalScrollBar()
        self.jump_button.setVisible(bar.maximum() - bar.value() > 100)
        if bar.value() == bar.maximum():
            self.follow_tail = True

    def update_generation_info(self):
        if not self.chat_busy:
            return
        elapsed = time.monotonic() - self.generation_started
        dots = "." * (int(elapsed * 2) % 3 + 1)
        if self.first_delta_at is None:
            text = f"等待首字{dots}  {elapsed:.1f} 秒"
        else:
            text = f"正在生成{dots}  首字 {self.first_delta_at - self.generation_started:.2f} 秒 · 已生成 {time.monotonic() - self.first_delta_at:.1f} 秒"
        if self.live_metrics:
            text = metrics_text(self.live_metrics)
        if self.chat_model_notice:
            text = self.chat_model_notice + "\n" + text
        if self.sid != self.chat_sid:
            text = "后台回答 · " + text
        self.generation_info.setText(text)

    def attachment_path(self, row):
        stored = self.workspace.store.attachment(row["session"], row["id"])
        return contained(self.workspace.attachments.directory(row["session"]), Path(stored["path"]))

    def attachment_card(self, row, removable=True):
        card = QFrame()
        card.setObjectName("attachment")
        layout = QHBoxLayout(card)
        if row["kind"] in {"image", "video"}:
            try:
                preview = self.attachment_path(row).parent / "preview.jpg"
                image = QLabel()
                image.setPixmap(QPixmap(str(preview)).scaled(72, 54, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
                layout.addWidget(image)
            except ValueError:
                pass
        info = QVBoxLayout()
        info.addWidget(label(row["name"]))
        text = size_label(row["size"])
        if row["kind"] == "video":
            text += f" · {row['meta'].get('duration', 0):.1f} 秒 · 抽样帧分析"
        if row.get("preview"):
            text += " · " + row["preview"][:80].replace("\n", " ")
        info.addWidget(label(text, "muted"))
        layout.addLayout(info, 1)
        if row["kind"] != "generated":
            layout.addWidget(button("预览", lambda: self.preview_file(row)))
        if removable:
            layout.addWidget(button("×", lambda: self.remove_pending(row)))
        else:
            layout.addWidget(button("保存", lambda: self.save_attachment(row)))
        return card

    def render_pending(self):
        clear_layout(self.pending_layout)
        for row in self.pending:
            self.pending_layout.addWidget(self.attachment_card(row))
        self.pending_scroll.setVisible(bool(self.pending))

    def choose_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "添加图片、视频或文件", "", "支持的附件 (*.jpg *.jpeg *.png *.webp *.mp4 *.mov *.avi *.webm *.txt *.md *.markdown *.pdf *.docx *.xlsx *.pptx *.csv)")
        self.add_files(paths)

    def add_files(self, paths):
        if self.busy() or not paths:
            return
        if len(self.pending) + len(paths) > self.settings["max_attachments"]:
            self.show_error(f"每条消息最多 {self.settings['max_attachments']} 个附件")
            return
        self.upload_busy = True
        self.update_controls()
        self.status.setText("正在解析附件…")
        sid = self.sid
        def upload(emit):
            failures = []
            for path in paths:
                try:
                    emit(self.workspace.upload(sid, path))
                except Exception as exc:
                    failures.append(f"{Path(path).name}：{exc}")
            return failures
        def received(row):
            self.pending.append(row)
            self.render_pending()
        def result(errors):
            self.status.setText("附件已准备好" if not errors else "附件错误：" + "；".join(errors))
        def done():
            self.upload_busy = False
            self.update_controls()
        self.task(upload, result, received, done)

    def remove_pending(self, row):
        if self.busy():
            return
        try:
            self.workspace.remove_attachment(self.sid, row["id"])
            self.pending.remove(row)
            self.render_pending()
        except Exception as exc:
            self.show_error(str(exc))

    def preview_file(self, row):
        dialog = QDialog(self)
        dialog.setWindowTitle(row["name"])
        dialog.resize(750, 600)
        layout = QVBoxLayout(dialog)
        try:
            path = self.attachment_path(row)
            if row["kind"] in {"image", "video"}:
                scroll = QScrollArea()
                scroll.setWidgetResizable(True)
                content = QWidget()
                frames = QVBoxLayout(content)
                for index, name in enumerate(row["meta"].get("frames", ["preview.jpg"])):
                    image = QLabel()
                    frame = contained(path.parent, path.parent / name)
                    image.setPixmap(QPixmap(str(frame)).scaled(680, 440, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
                    frames.addWidget(image)
                    if row["kind"] == "video":
                        frames.addWidget(label(f"{row['meta']['timestamps'][index]} 秒"))
                scroll.setWidget(content)
                layout.addWidget(scroll)
            else:
                text = QPlainTextEdit()
                text.setReadOnly(True)
                text.setPlainText(self.workspace.store.attachment(row["session"], row["id"])["text"])
                layout.addWidget(text)
            layout.addWidget(button("另存文件", lambda: self.save_attachment(row)))
            dialog.exec()
        except Exception as exc:
            self.show_error(str(exc))

    def save_attachment(self, row):
        destination, _ = QFileDialog.getSaveFileName(self, "另存文件", row["name"])
        if destination:
            try:
                shutil.copy2(self.attachment_path(row), destination)
                self.status.setText("文件已保存：" + destination)
            except Exception as exc:
                self.show_error(str(exc))

    def send(self):
        if self.busy():
            return
        composer_text = self.composer.toPlainText()
        message = composer_text.strip()
        if not message and not self.pending:
            return
        self.sent_composer_text = composer_text
        self.chat_busy = True
        self.chat_sid = self.sid
        self.chat_message = message
        self.chat_files = list(self.pending)
        self.chat_notice = "正在准备模型…首次加载可能需要一些时间"
        self.chat_error = ""
        self.chat_model_notice = ""
        self.session_notices.pop(self.sid, None)
        self.generation_started = time.monotonic()
        self.first_delta_at = None
        self.live_metrics = None
        self.generation_info.setVisible(True)
        self.generation_info.setText("等待首字…")
        self.cancel = threading.Event()
        self.update_controls()
        self.status.setText(self.chat_notice)
        self.previous_message_ids = {m["id"] for m in self.workspace.store.messages(self.sid)}
        if not self.previous_message_ids:
            clear_layout(self.messages)
        self.add_message("user", message, list(self.pending), animate=True)
        self.assistant = self.add_message("assistant", "正在准备…", animate=True)
        self.scroll_bottom(force=True)
        self.answer = ""
        self.answer_dirty = False
        sid, model = self.sid, self.models.currentData()
        ids = [row["id"] for row in self.pending]
        self.refresh_sessions()
        def generate(emit):
            for event in self.workspace.chat(sid, message, model_id=model, attachment_ids=ids, cancel=self.cancel):
                emit(event)
        self.task(generate, event=self.chat_event, finished=self.chat_finished, error=self.chat_failed)

    def chat_failed(self, message):
        self.chat_error = message
        self.chat_notice = "回答未完成：" + message
        self.session_notices[self.chat_sid] = self.chat_notice
        self.show_error(("后台对话：" if self.sid != self.chat_sid else "") + message)

    def chat_event(self, event):
        kind = event.get("type")
        if kind == "delta":
            if self.first_delta_at is None:
                self.first_delta_at = time.monotonic()
            self.answer += event.get("text", "")
            self.answer_dirty = True
        elif kind == "status":
            if event.get("status") == "started" and event.get("model"):
                actual = self.workspace.catalog.get(event["model"])
                vision_request = event.get("runtime_variant") == "vision" or actual.get("role") == "vision"
                prefix = "本次使用看图模型：" if vision_request else "本次使用："
                self.chat_model_notice = prefix + actual["name"]
                compatibility = None if vision_request else (actual.get("runtime_profile") or {}).get("cpu_compatibility")
                if compatibility:
                    self.chat_model_notice += " · 低内存模式"
                    if compatibility == "staged":
                        self.chat_model_notice += "（逐层读取磁盘，首字和回答可能较慢）"
                self.update_generation_info()
            self.chat_notice = event.get("text") or event.get("message") or "正在处理…"
            self.status.setText(("后台对话：" if self.sid != self.chat_sid else "") + self.chat_notice)
        elif kind == "error":
            self.chat_failed(event.get("text") or event.get("message") or event.get("error") or "生成失败")
        elif kind == "done":
            if not self.chat_error:
                self.chat_notice = "已停止生成" if self.cancel.is_set() else "回答完成"
                self.status.setText(("后台对话：" if self.sid != self.chat_sid else "") + self.chat_notice)
        elif kind == "metrics":
            self.live_metrics = event.get("metrics") or {}
            self.update_generation_info()
            if self.assistant and self.sid == self.chat_sid:
                self.assistant.set_metrics(self.live_metrics)

    def flush_answer(self):
        if self.answer_dirty and self.assistant and self.sid == self.chat_sid:
            bar = self.scroll.verticalScrollBar()
            follow = self.follow_tail and bar.maximum() - bar.value() < 100
            self.assistant.set_text(self.answer)
            self.answer_dirty = False
            if follow:
                self.scroll_bottom()

    def chat_finished(self):
        self.flush_answer()
        position = self.scroll.verticalScrollBar().value()
        follow = self.follow_tail
        completed_sid = self.chat_sid
        visible = self.sid == completed_sid
        self.chat_busy = False
        if not self.live_metrics:
            details = f"本次耗时 {time.monotonic() - self.generation_started:.1f} 秒 · 后端未提供 token 计数"
            self.generation_info.setText((self.chat_model_notice + "\n" if self.chat_model_notice else "") + details)
        self.assistant = None
        accepted = any(m["role"] == "user" and m["id"] not in self.previous_message_ids for m in self.workspace.store.messages(completed_sid))
        # Clear only the text that was actually sent.  If the user edited the
        # composer while the response was running, preserve that next draft.
        draft = {"text": self.composer.toPlainText(), "pending": list(self.pending)} if visible else self.session_drafts.get(completed_sid, {"text": self.sent_composer_text, "pending": self.chat_files})
        draft_unchanged = self.sent_composer_text is not None and draft["text"] == self.sent_composer_text
        if accepted:
            draft["pending"] = []
            if draft_unchanged:
                draft["text"] = ""
        self.session_drafts[completed_sid] = draft
        if visible:
            self.pending = list(draft["pending"])
            if self.composer.toPlainText() != draft["text"]:
                self.composer.setPlainText(draft["text"])
        self.sent_composer_text = None
        self.chat_sid = None
        if visible:
            self.render_history(scroll_to_end=follow)
        if visible and not follow:
            revision = self.scroll_revision
            def restore_position():
                if self.closing or revision != self.scroll_revision:
                    return
                self.scroll.verticalScrollBar().setValue(position)
                # The rebuild can briefly reduce the range to zero, which looks
                # like reaching the bottom but is not a user's scroll action.
                self.follow_tail = False
                self.scroll_position_changed()
            QTimer.singleShot(40, self, restore_position)
        self.render_pending()
        self.refresh_sessions()
        self.update_controls()
        if self.chat_error:
            self.status.setText(("后台对话：" if not visible else "") + self.chat_notice)
        elif self.cancel.is_set():
            self.session_notices[completed_sid] = "已停止回答，可以继续提问。"
            self.status.setText("已停止后台回答" if not visible else "已停止生成")
        elif not visible:
            self.status.setText("后台对话已结束，可在左侧打开查看。")
        if not visible:
            self.generation_info.setVisible(False)

    def stop_chat(self):
        self.cancel.set()
        self.cancel_button.setEnabled(False)
        self.status.setText("正在停止生成并释放模型…")

    def run_python(self):
        if self.busy():
            return
        self.python_busy = True
        self.python_cancel = threading.Event()
        self.python_input_queue = queue.Queue(maxsize=32)
        self.python_last_stream = None
        self.python_streamed = {"stdout": "", "stderr": ""}
        self.python_input_closed = False
        self.update_controls()
        self.output.setPlainText("正在运行…")
        self.status.setText("Python 正在运行")
        sid, code, timeout = self.sid, self.code.toPlainText(), self.timeout.value()
        def event(data):
            if not isinstance(data, dict) or data.get("stream") not in ("stdout", "stderr"):
                return
            if self.output.toPlainText() == "正在运行…":
                self.output.clear()
            stream = data["stream"]
            if self.python_last_stream != stream:
                self.output.moveCursor(QTextCursor.MoveOperation.End)
                self.output.insertPlainText(f"\n\n{stream}\n")
                self.python_last_stream = stream
            self.output.moveCursor(QTextCursor.MoveOperation.End)
            chunk = data.get("text", "")
            self.python_streamed[stream] += chunk
            self.output.insertPlainText(chunk)
            self.output.ensureCursorVisible()

        def result(data):
            reason = {"timeout": "已超时终止", "output_limit": "输出达到限制", "disk_limit": "工作目录达到限制", "cancelled": "已取消"}.get(data.get("reason"), data.get("reason"))
            text = f"退出码：{data['returncode']}  ·  {data['seconds']} 秒" + (f"  ·  {reason}" if reason else "")
            if not self.output.toPlainText() or self.output.toPlainText() == "正在运行…":
                text += "\n\nstdout\n" + (data.get("stdout") or "（无输出）")
                text += "\n\nstderr\n" + (data.get("stderr") or "（无错误）")
                self.output.setPlainText(text)
            else:
                self.output.moveCursor(QTextCursor.MoveOperation.End)
                stderr = data.get("stderr") or ""
                streamed_stderr = self.python_streamed.get("stderr", "")
                extra_stderr = stderr[len(streamed_stderr):] if stderr.startswith(streamed_stderr) else stderr
                if extra_stderr:
                    self.output.insertPlainText("\n\nstderr\n" + extra_stderr)
                self.output.insertPlainText("\n\n" + text)
                self.output.ensureCursorVisible()
            self.status.setText("Python 运行结束")
            self.render_history()
            self.refresh_sessions()
        def finished():
            self.python_busy = False
            self.python_input_queue = None
            if self.output.toPlainText() == "正在运行…":
                self.output.setPlainText(self.status.text())
            self.update_controls()
        self.task(lambda emit: self.workspace.run_python(
            sid, code, timeout, user_initiated=True, cancel=self.python_cancel,
            stdin_queue=self.python_input_queue, on_output=emit), result,
                  event=event, finished=finished)

    def send_python_input(self):
        if not self.python_busy or self.python_input_queue is None or self.python_input_closed:
            return
        value = self.python_input.text()
        if len(value.encode("utf-8")) > 8192:
            self.show_error("单次输入不得超过 8 KiB")
            return
        try:
            self.python_input_queue.put_nowait(value)
        except queue.Full:
            self.show_error("待处理输入已满，请等待程序读取")
            return
        self.python_input.clear()
        self.output.moveCursor(QTextCursor.MoveOperation.End)
        self.output.insertPlainText(f"\n[input] {value}\n")
        self.output.ensureCursorVisible()
        self.status.setText("已发送 Python 输入")

    def end_python_input(self):
        if self.python_busy and self.python_input_queue is not None and not self.python_input_closed:
            try:
                self.python_input_queue.put_nowait(None)
            except queue.Full:
                self.show_error("待处理输入已满，请稍后结束输入")
                return
            self.python_input_closed = True
            self.update_controls()
            self.status.setText("已结束 Python 标准输入")

    def stop_python(self):
        if self.python_busy:
            self.python_cancel.set()
            self.update_controls()
            self.status.setText("正在中断 Python…")

    def load_python_code(self, code):
        """Place Python from an assistant message into the editor only."""
        if self.python_busy:
            self.show_error("Python 正在运行，请等待本次执行结束")
            return
        text = str(code or "").strip()
        blocks = extract_python_blocks(text)
        if blocks:
            text = blocks[0]
        if not text:
            self.show_error("没有可加入工作区的 Python 代码")
            return
        if len(text.encode("utf-8")) > 200000:
            self.show_error("代码超过 200KB")
            return
        self.code.setPlainText(text)
        self.set_python_visible(True)
        self.code.setFocus()
        self.status.setText("Python 代码已加入工作区；请点击“运行代码”执行")

    def open_code(self):
        if self.python_busy:
            return
        path, _ = QFileDialog.getOpenFileName(self, "打开 Python 文件", "", "Python (*.py);;文本 (*.txt)")
        if path:
            try:
                if Path(path).stat().st_size > 200000:
                    raise ValueError("代码超过 200KB")
                self.code.setPlainText(Path(path).read_text("utf-8-sig"))
            except Exception as exc:
                self.show_error(str(exc))

    def save_code(self):
        path, _ = QFileDialog.getSaveFileName(self, "保存 Python 文件", "workspace.py", "Python (*.py)")
        if path:
            try:
                Path(path).write_text(self.code.toPlainText(), "utf-8")
            except Exception as exc:
                self.show_error(str(exc))

    def toggle_python(self):
        visible = not self.python_panel.isVisible()
        self.set_python_visible(visible)

    def set_python_visible(self, visible):
        self.python_panel.setVisible(visible)
        if visible:
            self.splitter.setSizes([780, 410])
        self.python_toggle.setText("收起 Python 工作区" if visible else "打开 Python 工作区")

    def open_guide(self):
        """Open the bundled beginner guide without requiring an online service."""
        guide = self.settings.root / "docs" / "beginner-guide" / "index.html"
        if not guide.is_file():
            QMessageBox.information(self, "使用说明", "本机没有找到使用说明文件。请查看软件目录中的 docs/beginner-guide 文件夹。")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(guide.resolve()))):
            QMessageBox.information(self, "使用说明", "无法打开使用说明。请在软件目录中双击 docs/beginner-guide/index.html。")

    def save_preferences(self, changes):
        # Preserve persisted fields managed by other parts of the workspace.
        existing = json.loads(self.settings.path.read_text("utf-8")) if self.settings.path.exists() else {}
        values = existing | self.settings.values | changes
        values.pop("system_memory_reserve_gb", None)
        from .memory_limits import memory_settings
        memory_settings(values)
        staged = self.settings.path.with_suffix(".tmp")
        staged.write_text(json.dumps(values, ensure_ascii=False, indent=2), "utf-8")
        staged.replace(self.settings.path)
        self.settings.values = values

    def apply_send_shortcut(self):
        shortcut = self.settings.values.get("send_shortcut", "ctrl_enter")
        self.composer.send_shortcut = "enter" if shortcut == "enter" else "ctrl_enter"
        self.shortcut_hint.setText("Enter 发送 · Shift + Enter 换行" if shortcut == "enter" else "Ctrl + Enter 发送")

    def refresh_models(self):
        current = self.models.currentData() or self.settings["default_model"]
        self.models.blockSignals(True)
        self.models.clear()
        for model in self.workspace.catalog.entries():
            if model["role"] != "auxiliary":
                self.models.addItem(model_choice_text(model), model["id"])
                self.models.setItemData(self.models.count() - 1,
                                        model["name"] + "\n" + model.get("note", ""),
                                        Qt.ItemDataRole.ToolTipRole)
        self.models.setCurrentIndex(max(0, self.models.findData(current)))
        self.models.blockSignals(False)
        self.model_changed(persist=False)

    def model_changed(self, *args, persist=True):
        mid = self.models.currentData()
        if not mid:
            return
        if persist and mid != self.settings["default_model"]:
            try:
                self.save_preferences({"default_model": mid})
            except Exception as exc:
                self.show_error("模型选择未保存：" + str(exc))
        ready = self.workspace.catalog.ready(self.workspace.catalog.get(mid))
        self.model_state.setText("已就绪" if ready else "未下载")
        if self.workspace.runtime.model_id and self.workspace.runtime.model_id != mid and not self.chat_busy:
            self.unload_model()

    def unload_model(self):
        if self.busy():
            return
        self.maintenance_busy = True
        self.update_controls()
        self.status.setText("正在卸载模型并释放资源…")
        if self.model_dialog:
            self.model_dialog.refresh()
        def unload(_):
            with self.workspace.runtime.lock:
                self.workspace.runtime.unload()
        def finished():
            self.maintenance_busy = False
            self.update_controls()
            if self.model_dialog and not self.closing:
                self.model_dialog.refresh()
        self.task(unload, result=lambda _: self.status.setText("模型已卸载"), finished=finished)

    def _build_runtime_settings_page(self, parent):
        page = QWidget(parent)
        outer = QVBoxLayout(page)
        scroll = QScrollArea(page)
        scroll.setObjectName("runtimeSettingsScroll")
        scroll.setWidgetResizable(True)
        body = QWidget(scroll)
        layout = QVBoxLayout(body)
        scroll.setWidget(body)
        outer.addWidget(scroll)
        layout.addWidget(label("一般保留默认设置就能使用。修改后点击“保存设置”；内存保护可避免 AI 占满电脑。", "muted"))
        form = QFormLayout()
        form.setVerticalSpacing(12)
        advanced = QWidget()
        advanced.setObjectName("advancedRuntimeSettings")
        advanced_layout = QVBoxLayout(advanced)
        advanced_layout.setContentsMargins(0, 10, 0, 0)
        advanced_layout.addWidget(label("以下是模型的详细参数，不清楚含义时保留原值即可。", "muted"))
        advanced_form = QFormLayout()
        advanced_form.setVerticalSpacing(12)
        fields = {}
        definitions = [("gpu_memory_gb", "显存预算 / GB", 0.0, 96.0, 0.5),
                       ("gpu_layers", "GPU 层数上限（0 为 CPU）", 0, 100, 1),
                       ("cpu_memory_gb", "AI 最多使用的内存", 1, 512, 1),
                       ("context_length", "一次参考的内容量（token）", 512, 131072, 512),
                       ("max_new_tokens", "单次回答长度上限（token）", 16, 16384, 128)]
        tips = {
            "gpu_memory_gb": "显卡可用于模型的显存预算；没有 NVIDIA 显卡时，文字模型可使用处理器运行。",
            "gpu_layers": "放到显卡上计算的模型层数。0 表示使用处理器；层数更多可能需要更多显存。",
            "cpu_memory_gb": "GiB 是内存容量单位。12 GiB 只是上限；软件还会根据电脑当时可用内存缩小预算。",
            "context_length": "包括问题、旧对话、资料和回答。token 是文字片段，不一定等于一个汉字；数值越大通常越占内存。",
            "max_new_tokens": "限制一次回答的长度；达到上限会结束输出。必须小于一次参考的内容总量。",
        }
        for key, title, low, high, step in definitions:
            field = QDoubleSpinBox() if isinstance(step, float) else QSpinBox()
            field.setObjectName(key)
            field.setRange(low, high)
            field.setSingleStep(step)
            field.setValue(self.settings[key])
            field.setToolTip(tips[key])
            fields[key] = field
            if key == "cpu_memory_gb":
                field.setSuffix(" GiB")
                form.addRow(title, field)
            else:
                advanced_form.addRow(title, field)
        idle = QSpinBox()
        idle.setObjectName("idle_unload_minutes")
        idle.setRange(0, 1440)
        idle.setValue(max(0, int(self.settings["idle_unload_minutes"])))
        idle.setSpecialValueText("不自动释放")
        idle.setSuffix(" 分钟")
        idle.setToolTip("一段时间没有使用时释放模型内存。再次提问会重新准备模型。")
        form.addRow("多久不用时释放内存", idle)
        shortcut = QComboBox()
        shortcut.setObjectName("send_shortcut")
        shortcut.addItem("Ctrl + Enter 发送，Enter 换行", "ctrl_enter")
        shortcut.addItem("Enter 发送，Shift + Enter 换行", "enter")
        shortcut.setCurrentIndex(max(0, shortcut.findData(self.composer.send_shortcut)))
        form.addRow("发送快捷键", shortcut)
        compress = QCheckBox("长对话自动整理较早的内容")
        compress.setObjectName("auto_compress_context")
        compress.setChecked(bool(self.settings.values.get("auto_compress_context", True)))
        compress.setToolTip("把较早对话整理成摘要供 AI 参考，完整原文仍保存在历史中；摘要可能遗漏细节。")
        form.addRow("长对话", compress)
        threshold = QSpinBox()
        threshold.setObjectName("compression_threshold_percent")
        threshold.setRange(25, 95)
        threshold.setSuffix(" %")
        threshold.setValue(int(self.settings.values.get("compression_threshold_percent", 80)))
        threshold.setToolTip("当参考内容达到可用空间的这个比例时，开始整理旧内容。")
        advanced_form.addRow("用到多少空间时整理", threshold)
        layout.addLayout(form)
        memory_status = label("", "muted")
        memory_status.setObjectName("memoryBudgetStatus")
        memory_status.setWordWrap(True)
        layout.addWidget(memory_status)
        def refresh_memory_budget():
            from .memory_limits import budget_for, GIB
            try:
                budget = budget_for({"cpu_memory_gb": fields["cpu_memory_gb"].value()})
                text = (f"下一次模型可用驻留预算最多约 {budget.resident_bytes / GIB:.2f} GiB；"
                        "模型实际用量受此上限约束，启动时会按电脑当时的可用内存重新计算。")
                job = getattr(self.workspace.runtime, "job", None)
                status = getattr(job, "memory_status", {})
                if status:
                    text += f"\n当前模型：{status['rss'] / GIB:.2f} / {status['limit'] / GIB:.2f} GiB；系统可用 {status['available'] / GIB:.2f} GiB。"
                memory_status.setText(text)
            except (ValueError, OSError) as exc:
                memory_status.setText(str(exc))
        fields["cpu_memory_gb"].valueChanged.connect(refresh_memory_budget)
        timer = QTimer(page)
        timer.timeout.connect(lambda: refresh_memory_budget() if page.isVisible() else None)
        timer.start(1000)
        refresh_memory_budget()
        memory_note = label("修改内存上限并保存，会停止当前模型并释放内存；下次回答应用新预算。限制内存可能降低大模型速度。", "muted")
        memory_note.setWordWrap(True)
        layout.addWidget(memory_note)
        advanced_layout.addLayout(advanced_form)
        advanced_layout.addWidget(label("大模型和更长的对话通常会更慢，也更占内存。提高这里的数字不会增加电脑的实际性能。", "muted"))
        advanced_toggle = QPushButton("高级设置（展开）")
        advanced_toggle.setObjectName("advancedSettingsToggle")
        advanced_toggle.setCheckable(True)
        advanced_toggle.toggled.connect(advanced.setVisible)
        advanced_toggle.toggled.connect(lambda checked: advanced_toggle.setText("收起高级设置" if checked else "高级设置（展开）"))
        layout.addWidget(advanced_toggle)
        layout.addWidget(advanced)
        advanced.hide()
        layout.addStretch()
        check_row = QHBoxLayout()
        self.check_button = button("检查软件是否完整", self.check_installation)
        self.check_button.setObjectName("checkInstallation")
        check_row.addWidget(self.check_button)
        self.log_export_button = button("导出运行日志 ZIP", self.export_logs)
        self.log_export_button.setObjectName("exportRunLogs")
        self.log_export_button.setToolTip("导出设备、模型和性能统计；不包含聊天正文、提示词或附件。")
        check_row.addWidget(self.log_export_button)
        check_row.addStretch()
        layout.addLayout(check_row)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存设置")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(lambda: self._save_runtime_settings(page, fields, idle, shortcut, compress, threshold))
        buttons.rejected.connect(parent.reject)
        outer.addWidget(buttons)
        page.fields = fields
        page.idle = idle
        page.shortcut = shortcut
        page.compress = compress
        page.threshold = threshold
        page.buttons = buttons
        page.advanced = advanced
        page.advanced_toggle = advanced_toggle
        def reset_settings():
            for key, field in fields.items():
                field.setValue(self.settings[key])
            idle.setValue(max(0, int(self.settings["idle_unload_minutes"])))
            shortcut.setCurrentIndex(max(0, shortcut.findData(self.composer.send_shortcut)))
            compress.setChecked(bool(self.settings.values.get("auto_compress_context", True)))
            threshold.setValue(int(self.settings.values.get("compression_threshold_percent", 75)))
        page.reset_settings = reset_settings
        return page

    def _save_runtime_settings(self, page, fields, idle, shortcut, compress, threshold):
        try:
            changes = {key: field.value() for key, field in fields.items()}
            changes.update(auxiliary_enabled=False, idle_unload_minutes=idle.value(),
                           send_shortcut=shortcut.currentData(), auto_compress_context=compress.isChecked(),
                           compression_threshold_percent=threshold.value())
            if changes["max_new_tokens"] >= changes["context_length"]:
                raise ValueError("回答长度必须小于上下文长度")
            self.save_preferences(changes)
            apply_limits = getattr(self.workspace.runtime, "apply_memory_settings", None)
            if apply_limits is not None:
                apply_limits()
            self.apply_send_shortcut()
            # Memory-budget changes already stopped their own child above.
            # Other runtime settings reload once the session is idle.
            if not self.chat_busy and not self.python_busy:
                self.unload_model()
            self.status.setText("运行设置已保存")
            page.window().accept()
        except Exception as exc:
            self.show_error(str(exc))

    def _ensure_settings_hub(self):
        if self.settings_hub is None:
            self.settings_hub = SettingsHub(self)
        return self.settings_hub

    def _ensure_model_dialog(self):
        if self.model_dialog is None:
            self.model_dialog = ModelDialog(self)
            self.model_dialog.finished.connect(lambda _: self.refresh_models())
        return self.model_dialog

    def _ensure_model_tools_dialog(self):
        if self.model_tools_dialog is None:
            from .model_tools import ModelToolsDialog
            self.model_tools_dialog = ModelToolsDialog(self)
        return self.model_tools_dialog

    def _ensure_project_dialog(self):
        if self.project_dialog is None:
            from .project_tools import ProjectDialog
            self.project_dialog = ProjectDialog(self)
        return self.project_dialog

    def _ensure_project_tasks_dialog(self):
        if self.project_tasks_dialog is None:
            from .project_tasks_ui import ProjectTasksDialog
            self.project_tasks_dialog = ProjectTasksDialog(self)
        return self.project_tasks_dialog

    def _ensure_skills_dialog(self):
        if self.skills_dialog is None:
            from .skills_ui import SkillsDialog
            self.skills_dialog = SkillsDialog(self)
        return self.skills_dialog

    def _ensure_knowledge_dialog(self):
        if self.knowledge_dialog is None:
            from .knowledge_ui import KnowledgeDialog
            self.knowledge_dialog = KnowledgeDialog(self)
        return self.knowledge_dialog

    def _show_settings_page(self, key, page):
        hub = self._ensure_settings_hub()
        hub.show()
        hub.activate(page)
        hub.raise_()
        hub.activateWindow()
        page.show()
        return page

    def open_models(self):
        if self.closing:
            return
        return self._show_settings_page("models", self._ensure_model_dialog())

    def open_project(self):
        if self.closing:
            return
        return self._show_settings_page("project", self._ensure_project_dialog())

    def open_model_tools(self):
        if self.closing:
            return
        return self._show_settings_page("model_tools", self._ensure_model_tools_dialog())

    def open_project_tasks(self):
        if self.closing:
            return
        return self._show_settings_page("project_tasks", self._ensure_project_tasks_dialog())

    def open_skills(self):
        if self.closing:
            return
        return self._show_settings_page("skills", self._ensure_skills_dialog())

    def open_knowledge(self):
        if self.closing:
            return
        return self._show_settings_page("knowledge", self._ensure_knowledge_dialog())

    def import_finished(self):
        self.importing = False
        self.update_controls()
        self.refresh_models()

    def open_settings(self):
        if self.closing:
            return
        hub = self._ensure_settings_hub()
        hub.activate(hub.pages["runtime"])
        if self.chat_busy:
            # A streaming answer must continue while the user inspects tools.
            hub.setModal(False)
            hub.show()
            hub.raise_()
            hub.activateWindow()
        else:
            hub.setModal(True)
            hub.exec()

    def check_installation(self):
        check_button = self.check_button
        if check_button is not None:
            check_button.setEnabled(False)
        self.installation_report = None
        def check(_):
            from .selfcheck import check
            return check(self.settings)
        def result(report):
            self.installation_report = report
            missing = [c["name"] for c in report["checks"] if c["required"] and not c["ok"]]
            if missing:
                self.show_error("运行环境缺项：" + "、".join(missing) + "。详情见 data/logs/selfcheck.json")
            elif not self.busy():
                self.status.setText("运行环境检查通过 · 基础模型可离线使用")
        self.task(check, result, finished=lambda: check_button and check_button.setEnabled(True))

    def export_logs(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出运行日志", "LocalAIWorkspace-run-logs.zip", "ZIP 压缩包 (*.zip)"
        )
        if not path:
            return
        target = Path(path)
        if target.suffix.lower() != ".zip":
            target = target.with_suffix(".zip")
        export_button = getattr(self, "log_export_button", None)
        if export_button:
            export_button.setEnabled(False)

        def export(_):
            from .runlog import export_logs
            return export_logs(self.settings, target)

        def completed(result):
            self.status.setText("运行日志已导出：" + str(result))
            QMessageBox.information(self, "日志导出完成", "统计 ZIP 已保存到：\n" + str(result))

        self.task(export, result=completed,
                  finished=lambda: export_button and export_button.setEnabled(True))

    def housekeeping(self):
        if not self.busy():
            self.task(lambda _: self.workspace.housekeeping())

    def dragEnterEvent(self, event):
        if not self.busy() and event.mimeData().hasUrls() and all(u.isLocalFile() for u in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        self.add_files([url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()])
        event.acceptProposedAction()

    def closeEvent(self, event):
        if not self.closing:
            if self.project_dialog:
                try:
                    self.project_dialog.persist_draft()
                except Exception as exc:
                    self.show_error("项目编辑草稿保存失败，窗口保持打开：" + str(exc))
                    event.ignore()
                    return
            self.closing = True
            self.cancel.set()
            self.python_cancel.set()
            self.session_search_timer.stop()
            self.housekeeper.stop()
            self.activity_timer.stop()
            self.stream_timer.stop()
            self.scroll_animation.stop()
            self.update_controls()
            self.status.setText("正在关闭工作区并释放资源…")
            if self.model_dialog:
                self.model_dialog.close()
            if self.model_tools_dialog:
                self.model_tools_dialog.close()
            if self.project_dialog:
                self.project_dialog.close()
            if self.project_tasks_dialog:
                self.project_tasks_dialog.close()
            if self.skills_dialog:
                self.skills_dialog.close()
            if self.knowledge_dialog:
                self.knowledge_dialog.close()
            if self.settings_hub:
                self.settings_hub.close()
            self.task(lambda _: self.workspace.close())
        if self.jobs:
            event.ignore()
            QTimer.singleShot(150, self, self.close)
        else:
            event.accept()


def run(settings=None):
    settings = settings or Settings()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Local AI Workspace")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    lock = QLockFile(str(settings.data / "desktop.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            user32.FindWindowW.restype = wintypes.HWND
            user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
            user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.SetForegroundWindow.argtypes = [wintypes.HWND]
            window = user32.FindWindowW(None, TITLE)
            if window:
                user32.ShowWindow(window, 9)
                user32.SetForegroundWindow(window)
        return 0
    workspace = Workspace(settings)
    window = Window(workspace)
    window.show()
    try:
        return app.exec()
    finally:
        workspace.close()
        lock.unlock()
