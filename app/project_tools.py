"""User-operated project files and terminal; never exposed as model tools."""
from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import tempfile
import threading
import uuid
from pathlib import Path

from .config import contained
from .processes import bounded_run

TEXT_LIMIT = 2 * 1024 * 1024
IGNORED = {".git", ".localai-trash", "node_modules", "__pycache__", ".venv", "runtime", "offload"}


class ProjectFiles:
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("请选择项目目录")

    def path(self, relative):
        relative = str(relative)
        # Windows alternate data streams, device names and trailing dots can
        # address a different file than the one displayed in the editor.
        parts = relative.replace("\\", "/").split("/")
        reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
        if any(not part or part in {".", ".."} or part.endswith((" ", ".")) or
               any(char in part for char in ':<>"|?*') or any(ord(char) < 32 for char in part) or
               part.split(".")[0].upper() in reserved for part in parts):
            raise ValueError("请使用项目内的普通相对文件路径")
        return contained(self.root, self.root / relative)

    def files(self, query="", limit=2000):
        result, visited = [], 0
        for base, dirs, names in os.walk(self.root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in IGNORED and not d.startswith(".")
                             and not Path(base, d).is_symlink()
                             and not getattr(Path(base, d).lstat(), "st_file_attributes", 0) & 0x400)
            for name in sorted(names):
                visited += 1
                if visited > 10000:
                    return result
                relative = (Path(base) / name).relative_to(self.root).as_posix()
                try:
                    path = self.path(relative)
                    if query.casefold() not in relative.casefold():
                        if path.stat().st_size > TEXT_LIMIT or query.casefold() not in path.read_text("utf-8-sig").casefold():
                            continue
                    result.append(relative)
                except (OSError, UnicodeError, ValueError):
                    continue
                if len(result) >= limit:
                    return result
        return result

    def read(self, relative):
        path = self.path(relative)
        if path.stat().st_size > TEXT_LIMIT:
            raise ValueError("编辑器只打开 2 MiB 以内的文本；更大文件请作为附件分析")
        raw = path.read_bytes()
        if b"\0" in raw:
            raise ValueError("所选文件不是可编辑的 UTF-8 文本")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeError as exc:
            raise ValueError("编辑器支持 UTF-8 文本，请先转换编码") from exc
        return text, hashlib.sha256(raw).hexdigest()

    def save(self, relative, text, expected=None, create=False):
        path = self.path(relative)
        raw = text.encode("utf-8")
        if len(raw) > TEXT_LIMIT:
            raise ValueError("文本超过 2 MiB 编辑限制")
        if create:
            if not path.parent.is_dir():
                raise ValueError("父目录不存在")
            with path.open("xb") as stream:
                stream.write(raw)
        else:
            if expected is None or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("文件已在外部修改，请重新打开后合并；当前编辑内容已保留")
            handle, staged = tempfile.mkstemp(prefix=".localai-save-", dir=path.parent)
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(raw)
                # Check again after writing the staging file.
                if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                    raise ValueError("保存时检测到外部修改，请重新打开后合并")
                os.replace(staged, path)
            finally:
                Path(staged).unlink(missing_ok=True)
        return hashlib.sha256(raw).hexdigest()

    def rename(self, relative, target):
        source, destination = self.path(relative), self.path(target)
        if not source.is_file():
            raise ValueError("仅支持重命名普通文件")
        if destination.exists():
            raise ValueError("目标文件已经存在")
        source.rename(destination)

    def trash(self, relative):
        source = self.path(relative)
        if not source.is_file():
            raise ValueError("仅支持移走普通文件")
        folder = self.path(".localai-trash")
        folder.mkdir(exist_ok=True)
        target = self.path(f".localai-trash/{uuid.uuid4().hex}-{source.name}")
        source.rename(target)
        return target


def git_binary(settings):
    binary = settings.root / "runtime/git/cmd/git.exe"
    if not binary.is_file():
        raise ValueError("缺少包内 Git，请使用完整资源目录")
    return binary


def run_command(settings, root, command, *, cancel=None, timeout=30, user_initiated=False):
    if not user_initiated:
        raise ValueError("终端只能由用户点击运行")
    if not command.strip() or len(command) > 32000:
        raise ValueError("命令为空或过长")
    shell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    if not shell.is_file():
        raise ValueError("找不到 Windows PowerShell")
    prelude = "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); $OutputEncoding = [Console]::OutputEncoding;\n"
    encoded = base64.b64encode((prelude + command).encode("utf-16le")).decode("ascii")
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join([str(settings.root / "runtime/python"), str(settings.root / "runtime/git/cmd"),
                                   str(shell.parent), str(shell.parent.parent.parent)])
    return bounded_run([str(shell), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                       cwd=Path(root), env=env, timeout=min(300, max(1, timeout)), limit=1024 * 1024,
                       memory_limit=4 * 1024**3, cancelled=cancel)


def git_diff(settings, root, cancel=None):
    return bounded_run([str(git_binary(settings)), "--no-pager", "diff", "--no-ext-diff", "--no-textconv", "HEAD", "--"],
                       cwd=Path(root), timeout=30, limit=1024 * 1024, cancelled=cancel)


def project_dialog(owner):
    """Import Qt only when opened; the service remains independently testable."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QLineEdit,
                                  QPlainTextEdit, QTreeWidget, QTreeWidgetItem, QFileDialog, QMessageBox,
                                  QInputDialog, QSplitter, QSpinBox, QWidget)

    class ProjectDialog(QDialog):
        def __init__(self):
            super().__init__(owner)
            self.setWindowTitle("文件与项目")
            self.resize(1150, 780)
            self.service = None
            self.relative = None
            self.original = ""
            self.digest = None
            self.running = False
            self.search_revision = 0
            self.cancel = threading.Event()
            self.state = owner.settings.data / "project-tools.json"
            self.draft = contained(owner.settings.data, owner.settings.data / "project-editor-draft.json")
            layout = QVBoxLayout(self)
            introduction = QLabel("先打开文件夹，再双击左侧文件查看或编辑。需要 AI 帮忙时，点击“添加到对话”；编辑后点击“保存”才会修改原文件。")
            introduction.setWordWrap(True)
            layout.addWidget(introduction)
            row = QHBoxLayout()
            self.location = QLabel("尚未打开文件夹")
            self.location.setTextFormat(Qt.TextFormat.PlainText)
            self.location.setWordWrap(True)
            row.addWidget(self.location, 1)
            self.choose = QPushButton("打开文件夹")
            self.choose.clicked.connect(self.choose_project)
            row.addWidget(self.choose)
            layout.addLayout(row)
            self.search = QLineEdit()
            self.search.setPlaceholderText("按文件路径或文本内容查找")
            find = QPushButton("查找 / 刷新")
            find.clicked.connect(self.refresh)
            self.search.returnPressed.connect(self.refresh)
            row = QHBoxLayout()
            row.addWidget(self.search)
            row.addWidget(find)
            layout.addLayout(row)
            split = QSplitter()
            self.tree = QTreeWidget()
            self.tree.setHeaderLabels(["项目文件（最多 2000 项）"])
            self.tree.itemDoubleClicked.connect(self.open_file)
            split.addWidget(self.tree)
            self.editor = QPlainTextEdit()
            self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            self.editor.setPlaceholderText("打开文件夹后，双击左侧文件即可查看内容。")
            split.addWidget(self.editor)
            split.setSizes([300, 800])
            layout.addWidget(split, 3)
            row = QHBoxLayout()
            self.file_buttons = {}
            for title, fn in [("新建文件", self.create), ("保存", self.save), ("重命名", self.rename),
                              ("移到回收站", self.remove), ("查看改动", self.diff),
                              ("项目版本改动", self.git), ("添加到对话", self.attach)]:
                btn = QPushButton(title)
                btn.clicked.connect(fn)
                self.file_buttons[fn.__name__] = btn
                row.addWidget(btn)
            layout.addLayout(row)
            self.terminal_toggle = QPushButton("运行命令（高级）")
            self.terminal_toggle.setObjectName("projectTerminalToggle")
            self.terminal_toggle.setCheckable(True)
            layout.addWidget(self.terminal_toggle)
            self.terminal_panel = QWidget()
            self.terminal_panel.setObjectName("projectTerminalPanel")
            terminal_layout = QVBoxLayout(self.terminal_panel)
            terminal_layout.setContentsMargins(0, 0, 0, 0)
            self.terminal_toggle.toggled.connect(self.terminal_panel.setVisible)
            self.command = QPlainTextEdit()
            self.command.setPlaceholderText("输入 PowerShell 命令。仅运行你理解并信任的命令；点击“运行命令”才会执行。")
            self.command.setMaximumHeight(80)
            terminal_layout.addWidget(self.command)
            row = QHBoxLayout()
            self.timeout = QSpinBox()
            self.timeout.setRange(1, 300)
            self.timeout.setValue(30)
            self.timeout.setSuffix(" 秒")
            self.timeout.setToolTip("最长运行时间，到时自动停止。")
            row.addWidget(QLabel("最多运行"))
            row.addWidget(self.timeout)
            self.run = QPushButton("运行命令")
            self.run.clicked.connect(self.execute)
            row.addWidget(self.run)
            self.stop = QPushButton("停止")
            self.stop.clicked.connect(lambda: self.cancel.set())
            row.addWidget(self.stop)
            terminal_layout.addLayout(row)
            layout.addWidget(self.terminal_panel)
            self.terminal_panel.hide()
            restore = QPushButton("恢复未保存的草稿")
            restore.clicked.connect(self.restore_draft)
            layout.addWidget(restore)
            self.output = QPlainTextEdit()
            self.output.setReadOnly(True)
            self.output.setPlaceholderText("查看改动或运行命令后，结果会显示在这里。")
            layout.addWidget(self.output, 1)
            self.notice = QLabel("只聊天无需使用这里。移除的文件会留在该文件夹内的 .localai-trash 回收目录。")
            self.notice.setTextFormat(Qt.TextFormat.PlainText)
            self.notice.setWordWrap(True)
            layout.addWidget(self.notice)
            try:
                root = json.loads(self.state.read_text("utf-8"))["project"]
                self.set_project(root)
            except (OSError, ValueError, KeyError):
                pass
            self.update_file_controls()

        def update_file_controls(self):
            has_project = self.service is not None
            has_file = has_project and self.relative is not None
            self.editor.setReadOnly(not has_file)
            for action, control in self.file_buttons.items():
                control.setEnabled(has_project if action in {"create", "git"} else has_file)
                control.setToolTip("" if control.isEnabled() else "先打开文件夹，再双击要处理的文件。")
            self.run.setEnabled(has_project and not self.running)
            self.stop.setEnabled(self.running)

        def restore_draft(self):
            if not self.clean():
                return
            def restore():
                if not self.draft.is_file():
                    raise ValueError("没有退出前草稿")
                if self.draft.stat().st_size > 8 * 1024**2:
                    raise ValueError("退出前草稿超过限制")
                snapshot = json.loads(self.draft.read_text("utf-8"))
                self.set_project(snapshot["project"])
                self.service.path(snapshot["relative"])
                self.relative = snapshot["relative"]
                self.original, self.digest = snapshot["original"], snapshot["digest"]
                self.editor.setPlainText(snapshot["text"])
                self.update_file_controls()
                self.notice.setText("已恢复退出前草稿；保存仍会检测磁盘外部修改。")
            self.attempt(restore)

        def attempt(self, fn):
            try:
                return fn()
            except Exception as exc:
                self.notice.setText(str(exc))

        def clean(self):
            return self.editor.toPlainText() == self.original or QMessageBox.question(
                self, "未保存的编辑", "放弃当前未保存的编辑？", QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel) == QMessageBox.StandardButton.Discard

        def set_project(self, root):
            self.service = ProjectFiles(root)
            self.location.setText(str(self.service.root))
            self.relative, self.original, self.digest = None, "", None
            self.editor.clear()
            self.state.write_text(json.dumps({"project": str(self.service.root)}, ensure_ascii=False), "utf-8")
            self.refresh()
            self.update_file_controls()

        def choose_project(self):
            if self.running or not self.clean():
                return
            path = QFileDialog.getExistingDirectory(self, "选择项目")
            if path:
                self.attempt(lambda: self.set_project(path))

        def refresh(self):
            if not self.service:
                return
            self.search_revision += 1
            revision, service, query = self.search_revision, self.service, self.search.text().strip()
            self.notice.setText("正在查找项目文件…")
            def done(files):
                if revision == self.search_revision and not owner.closing:
                    self.show_files(files)
                    self.notice.setText(f"找到 {len(files)} 个文件（最多显示 2000 项）")
            owner.task(lambda _: service.files(query), done)

        def show_files(self, files):
            self.tree.clear()
            nodes = {}
            for relative in files:
                parent = self.tree.invisibleRootItem()
                parts = Path(relative).parts
                for index, part in enumerate(parts):
                    key = "/".join(parts[:index + 1])
                    if key not in nodes:
                        nodes[key] = QTreeWidgetItem(parent, [part])
                    parent = nodes[key]
                parent.setData(0, Qt.ItemDataRole.UserRole, relative)

        def open_file(self, item, column=0):
            relative = item.data(0, Qt.ItemDataRole.UserRole)
            if relative and self.clean():
                def load():
                    text, digest = self.service.read(relative)
                    self.relative, self.original, self.digest = relative, text, digest
                    self.editor.setPlainText(text)
                    self.update_file_controls()
                    self.notice.setText(relative)
                self.attempt(load)

        def save(self):
            if self.service and self.relative:
                def write():
                    self.digest = self.service.save(self.relative, self.editor.toPlainText(), self.digest)
                    self.original = self.editor.toPlainText()
                    self.notice.setText("已保存：" + self.relative)
                self.attempt(write)

        def create(self):
            if self.service:
                name, ok = QInputDialog.getText(self, "新建文本文件", "相对项目路径")
                if ok and name:
                    self.attempt(lambda: self.service.save(name, "", create=True))
                    self.refresh()

        def rename(self):
            if self.service and self.relative and self.clean():
                name, ok = QInputDialog.getText(self, "重命名", "新相对路径", text=self.relative)
                if ok and name:
                    def move():
                        self.service.rename(self.relative, name)
                        self.relative = name
                        self.notice.setText("已重命名：" + name)
                        self.refresh()
                    self.attempt(move)

        def remove(self):
            if self.service and self.relative and self.clean():
                if QMessageBox.question(self, "移走文件", "将当前文件移到项目 .localai-trash 回收目录？") != QMessageBox.StandardButton.Yes:
                    return
                def move():
                    target = self.service.trash(self.relative)
                    self.relative, self.original, self.digest = None, "", None
                    self.editor.clear()
                    self.update_file_controls()
                    self.refresh()
                    self.notice.setText("已保留在：" + str(target))
                self.attempt(move)

        def diff(self):
            self.output.setPlainText("".join(difflib.unified_diff(self.original.splitlines(True), self.editor.toPlainText().splitlines(True),
                                                               fromfile="磁盘原文", tofile="编辑内容")) or "没有编辑差异")

        def attach(self):
            if self.service and self.relative and not owner.busy():
                if len(owner.pending) >= owner.settings["max_attachments"]:
                    self.notice.setText("当前消息附件数量已达上限")
                    return
                # A text snapshot can contain source extensions not accepted by
                # the general document importer. Copy the editor's visible text.
                text, name, sid = self.editor.toPlainText(), Path(self.relative).name, owner.sid
                def publish(_):
                    with tempfile.TemporaryDirectory(prefix="project-context-") as folder:
                        path = Path(folder) / (name + ".txt")
                        path.write_text(text, "utf-8")
                        return owner.workspace.upload(sid, path)
                owner.upload_busy = True
                owner.update_controls()
                def received(row):
                    owner.pending.append(row)
                    owner.render_pending()
                def done():
                    owner.upload_busy = False
                    owner.update_controls()
                owner.task(publish, received, finished=done)

        def start(self, fn):
            if self.running or not self.service:
                return
            self.running = True
            self.cancel = threading.Event()
            self.update_file_controls()
            self.choose.setEnabled(False)
            self.output.setPlainText("正在运行…")
            def result(report):
                self.output.setPlainText(f"退出码：{report['returncode']}  耗时：{report['seconds']} 秒  {report.get('reason') or ''}\n"
                                         + report["stdout"] + "\n" + report["stderr"])
            def done():
                self.running = False
                self.update_file_controls()
                self.choose.setEnabled(True)
            owner.task(fn, result, finished=done)

        def execute(self):
            if self.service:
                command, root, timeout = self.command.toPlainText(), self.service.root, self.timeout.value()
                self.start(lambda _: run_command(owner.settings, root, command, cancel=self.cancel, timeout=timeout, user_initiated=True))

        def git(self):
            if self.service:
                root = self.service.root
                self.start(lambda _: git_diff(owner.settings, root, self.cancel))

        def persist_draft(self):
            if self.service and self.relative and self.editor.toPlainText() != self.original:
                snapshot = {"project": str(self.service.root), "relative": self.relative,
                            "original": self.original, "digest": self.digest, "text": self.editor.toPlainText()}
                staged = contained(owner.settings.data, self.draft.with_suffix(".tmp"))
                staged.write_text(json.dumps(snapshot, ensure_ascii=False), "utf-8")
                staged.replace(self.draft)

        def closeEvent(self, event):
            if not owner.closing and not self.clean():
                event.ignore()
                return
            if owner.closing and self.service and self.relative and self.editor.toPlainText() != self.original:
                try:
                    self.persist_draft()
                except Exception as exc:
                    self.notice.setText("草稿保存失败：" + str(exc))
                    event.ignore()
                    return
            self.cancel.set()
            event.accept()

        def reject(self):
            self.close()

    return ProjectDialog()


def ProjectDialog(owner):
    return project_dialog(owner)
