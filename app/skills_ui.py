"""Explicit, editable skill preferences for native ordinary conversation."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from .config import contained
from .skills import Skills, TEMPLATE


def SkillsDialog(owner):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
        QInputDialog, QLabel, QListWidget, QListWidgetItem, QMessageBox,
        QPlainTextEdit, QPushButton, QSplitter, QVBoxLayout, QWidget,
    )

    class Dialog(QDialog):
        def __init__(self):
            super().__init__(owner)
            self.setWindowTitle("回答偏好")
            self.resize(1040, 720)
            self.service = Skills(owner.settings)
            self.project = None
            self.current = None
            self.original = ""
            self.digest = None
            self.draft_path = None
            self.downloading = False
            self.download_cancel = threading.Event()
            layout = QVBoxLayout(self)
            introduction = QLabel("告诉 AI 你喜欢怎样的回答，例如“先说结论，再举例”。新建后编辑、保存，再勾选使用；不需要编程。")
            introduction.setWordWrap(True)
            layout.addWidget(introduction)
            self.location = QLabel()
            self.location.setTextFormat(Qt.TextFormat.PlainText)
            self.location.setWordWrap(True)
            layout.addWidget(self.location)
            row = QHBoxLayout()
            self.scope = QComboBox()
            self.scope.addItem("所有对话", "user")
            self.scope.addItem("仅当前项目", "project")
            self.scope.setToolTip("选择新建或导入的回答偏好放在哪里。")
            row.addWidget(self.scope)
            self.more_panel = QWidget()
            self.more_panel.setObjectName("skill_more_panel")
            more_row = QHBoxLayout(self.more_panel)
            more_row.setContentsMargins(0, 0, 0, 0)
            self.actions = {}
            for title, name, action in [("新建偏好", "create", self.create), ("导入文件", "import", self.import_file),
                                        ("从网址导入", "url", self.import_url),
                                        ("保存", "save", self.save), ("对比外部修改", "merge", self.merge),
                                        ("更改适用范围", "move", self.move),
                                        ("刷新列表", "refresh", self.refresh_project)]:
                control = QPushButton(title)
                control.setObjectName("skill_" + name)
                control.clicked.connect(action)
                self.actions[name] = control
                (row if name in {"create", "import", "save"} else more_row).addWidget(control)
            self.more_button = QPushButton("更多操作")
            self.more_button.setObjectName("skill_more")
            self.more_button.setCheckable(True)
            self.more_button.toggled.connect(self.more_panel.setVisible)
            row.addWidget(self.more_button)
            row.addStretch()
            self.recover = QPushButton("恢复未保存的草稿")
            self.recover.setObjectName("skill_recover_draft")
            self.recover.clicked.connect(self.restore_draft)
            more_row.addWidget(self.recover)
            more_row.addStretch()
            layout.addLayout(row)
            layout.addWidget(self.more_panel)
            self.more_panel.hide()
            split = QSplitter()
            self.items = QListWidget()
            self.items.currentItemChanged.connect(self.selected)
            split.addWidget(self.items)
            self.editor = QPlainTextEdit()
            self.editor.setObjectName("skill_editor")
            self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            self.editor.setPlaceholderText("点击“新建偏好”开始，或从左侧选择已有偏好进行修改。")
            split.addWidget(self.editor)
            split.setSizes([270, 730])
            layout.addWidget(split, 1)
            self.enabled = QCheckBox("回答时使用这份偏好")
            self.enabled.setObjectName("skill_enabled")
            self.enabled.clicked.connect(self.toggle)
            layout.addWidget(self.enabled)
            self.notice = QLabel("保存后勾选才会生效。偏好只影响回答方式，不会自动运行代码或安装软件。")
            self.notice.setTextFormat(Qt.TextFormat.PlainText)
            self.notice.setWordWrap(True)
            layout.addWidget(self.notice)
            self.refresh_project()
            self.update_drafts()

        def draft_files(self):
            drafts = contained(owner.settings.data, owner.settings.data / "skills-drafts")
            return sorted(drafts.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True) if drafts.is_dir() else []

        def update_drafts(self):
            drafts = self.draft_files()
            self.recover.setEnabled(bool(drafts))
            if drafts:
                self.more_button.setChecked(True)
                self.notice.setText("有未保存的草稿，点击“恢复未保存的草稿”可继续编辑。")

        def error(self, exc):
            self.notice.setText(str(exc))

        def clean(self):
            if self.editor.toPlainText() == self.original:
                return True
            answer = QMessageBox.question(self, "未保存的技能编辑", "保存当前技能修改？",
                                          QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                                          | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
            if answer == QMessageBox.StandardButton.Save:
                return self.save()
            return answer == QMessageBox.StandardButton.Discard

        def refresh_project(self):
            if not self.clean():
                return
            project = None
            path = owner.settings.data / "project-tools.json"
            try:
                if path.stat().st_size > 1024 * 1024:
                    raise ValueError("项目记录过大")
                candidate = json.loads(path.read_text("utf-8"))["project"]
                self.service.directory("project", candidate)
                project = str(Path(candidate).absolute())
            except (OSError, ValueError, KeyError, TypeError):
                pass
            self.project = project
            self.location.setText("当前项目：" + project if project else "默认用于所有对话。需要项目专用偏好时，先在“文件与项目”中打开文件夹。")
            self.scope.model().item(1).setEnabled(project is not None)
            if project is None:
                self.scope.setCurrentIndex(0)
            self.current, self.original, self.digest = None, "", None
            self.editor.clear()
            self.refresh()

        def refresh(self, select=None):
            try:
                rows = self.service.list(self.project)
            except Exception as exc:
                self.error(exc)
                return
            self.items.blockSignals(True)
            self.items.clear()
            choice = None
            for row in rows:
                scope = "用户" if row["scope"] == "user" else "项目"
                item = QListWidgetItem(("● " if row["enabled"] else "○ ") + row["name"] + " · " + scope)
                item.setToolTip(row["path"])
                item.setData(Qt.ItemDataRole.UserRole, row)
                self.items.addItem(item)
                if select == (row["name"], row["scope"]):
                    choice = item
            self.items.setCurrentItem(choice)
            self.items.blockSignals(False)
            self.selected(choice)

        def selected(self, item, previous=None):
            if not self.clean():
                self.items.blockSignals(True)
                self.items.setCurrentItem(previous)
                self.items.blockSignals(False)
                return
            row = item.data(Qt.ItemDataRole.UserRole) if item else None
            try:
                text, digest = self.service.read(row["name"], row["scope"], self.project) if row else ("", None)
            except Exception as exc:
                self.error(exc)
                self.items.blockSignals(True)
                self.items.setCurrentItem(previous)
                self.items.blockSignals(False)
                return
            self.current, self.original, self.digest = row, text, digest
            self.draft_path = None
            self.editor.setPlainText(text)
            self.editor.setReadOnly(row is None)
            self.enabled.setEnabled(row is not None)
            self.enabled.setChecked(bool(row and row["enabled"]))
            self.actions["save"].setEnabled(row is not None)
            self.actions["merge"].setEnabled(row is not None)
            self.actions["move"].setEnabled(row is not None and self.project is not None)

        def save(self):
            if not self.current:
                return False
            row = self.current
            try:
                self.digest = self.service.save(row["name"], self.editor.toPlainText(), self.digest, row["scope"], self.project)
                self.original = self.editor.toPlainText()
                if self.draft_path:
                    self.draft_path.unlink(missing_ok=True)
                    self.draft_path = None
                    self.update_drafts()
                self.notice.setText("已保存：" + row["path"])
                return True
            except Exception as exc:
                self.error(exc)
                return False

        def toggle(self, checked):
            if not self.current:
                return
            row = self.current
            try:
                self.service.set_enabled(row["name"], checked, row["scope"], self.project)
                row["enabled"] = checked
                item = self.items.currentItem()
                scope = "用户" if row["scope"] == "user" else "项目"
                item.setText(("● " if checked else "○ ") + row["name"] + " · " + scope)
                self.notice.setText("下次发送消息时生效；正在进行的回答保留原有上下文。")
            except Exception as exc:
                self.enabled.setChecked(row["enabled"])
                self.error(exc)

        def merge(self):
            if not self.current:
                return
            row = self.current
            try:
                current, digest = self.service.read(row["name"], row["scope"], self.project)
            except Exception as exc:
                self.error(exc)
                return
            review = QDialog(self)
            review.setWindowTitle("磁盘内容与当前编辑")
            review.resize(1000, 620)
            layout = QVBoxLayout(review)
            hint = QLabel("左侧为当前磁盘，右侧为待保存编辑。合并后点击确认；编辑区将采用合并文本，保存仍需手动点击。")
            hint.setWordWrap(True)
            layout.addWidget(hint)
            split = QSplitter()
            disk = QPlainTextEdit(current)
            disk.setReadOnly(True)
            disk.setObjectName("skill_disk_text")
            pending = QPlainTextEdit(self.editor.toPlainText())
            pending.setObjectName("skill_merge_text")
            split.addWidget(disk)
            split.addWidget(pending)
            layout.addWidget(split)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            buttons.button(QDialogButtonBox.StandardButton.Ok).setText("采用合并文本")
            buttons.accepted.connect(review.accept)
            buttons.rejected.connect(review.reject)
            layout.addWidget(buttons)
            if review.exec() == QDialog.DialogCode.Accepted:
                self.original, self.digest = current, digest
                self.editor.setPlainText(pending.toPlainText())
                self.notice.setText("合并编辑已保留；点击保存后才会更新 SKILL.md。")

        def create(self):
            if not self.clean():
                return
            name, accepted = QInputDialog.getText(self, "新建回答偏好", "取个名字，例如：简单说明、学习助手")
            if accepted and name:
                try:
                    scope = self.scope.currentData()
                    self.service.create(name, TEMPLATE, scope, self.project)
                    self.original = self.editor.toPlainText()
                    self.refresh((name, scope))
                except Exception as exc:
                    self.error(exc)

        def import_file(self):
            if not self.clean():
                return
            source, _ = QFileDialog.getOpenFileName(self, "选择 SKILL.md", "", "技能 (SKILL.md)")
            if not source:
                return
            try:
                snapshot = self.service.preview_file(source)
            except Exception as exc:
                self.error(exc)
                return
            self.preview_import(snapshot, str(Path(source).absolute()), Path(source).parent.name)

        def preview_import(self, snapshot, source, default_name="imported-skill"):
            if owner.closing:
                return
            preview = QDialog(self)
            preview.setWindowTitle("预览技能文本")
            preview.resize(760, 560)
            layout = QVBoxLayout(preview)
            origin = QLabel("来源：" + source)
            origin.setTextFormat(Qt.TextFormat.PlainText)
            origin.setWordWrap(True)
            layout.addWidget(origin)
            text = QPlainTextEdit(snapshot)
            text.setObjectName("skill_import_preview")
            text.setReadOnly(True)
            layout.addWidget(text)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            buttons.button(QDialogButtonBox.StandardButton.Ok).setText("导入预览内容")
            buttons.accepted.connect(preview.accept)
            buttons.rejected.connect(preview.reject)
            layout.addWidget(buttons)
            if preview.exec() != QDialog.DialogCode.Accepted:
                return
            name, accepted = QInputDialog.getText(self, "导入回答偏好", "给这份偏好取个名字", text=default_name)
            if accepted and name:
                try:
                    scope = self.scope.currentData()
                    # Import exactly the preview, even if source changes meanwhile.
                    self.service.create(name, snapshot, scope, self.project)
                    self.original = self.editor.toPlainText()
                    self.refresh((name, scope))
                except Exception as exc:
                    self.error(exc)

        def import_url(self):
            if self.downloading or not self.clean() or owner.closing:
                return
            url, accepted = QInputDialog.getText(self, "从网址导入回答偏好", "粘贴 SKILL.md 文件的 https:// 地址（下载后先预览）")
            if not accepted or not url.strip():
                return
            self.downloading = True
            self.download_cancel = threading.Event()
            for widget in [*self.actions.values(), self.items, self.editor, self.enabled, self.scope]:
                widget.setEnabled(False)
            self.notice.setText("正在下载原始技能文本，完成后需要用户预览确认。")
            def finish():
                self.downloading = False
                for widget in [*self.actions.values(), self.items, self.editor, self.enabled, self.scope]:
                    widget.setEnabled(True)
                selected = self.current is not None
                self.editor.setReadOnly(not selected)
                self.enabled.setEnabled(selected)
                self.actions["save"].setEnabled(selected)
                self.actions["merge"].setEnabled(selected)
                self.actions["move"].setEnabled(selected and self.project is not None)
            def received(result):
                self.preview_import(result["text"], result["url"])
            job = owner.task(lambda _: self.service.preview_url(url.strip(), self.download_cancel),
                             result=received, finished=finish)
            job.error.connect(self.error, Qt.ConnectionType.QueuedConnection)

        def move(self):
            if not self.current or not self.clean():
                return
            row = self.current
            target = "project" if row["scope"] == "user" else "user"
            try:
                self.service.move(row["name"], row["scope"], target, self.project, self.digest)
                self.original = self.editor.toPlainText()
                self.refresh((row["name"], target))
            except Exception as exc:
                self.error(exc)

        def restore_draft(self):
            if not self.clean():
                return
            for path in self.draft_files():
                try:
                    draft = json.loads(path.read_text("utf-8"))
                    row = draft.get("skill")
                    if not isinstance(row, dict) or row.get("scope") not in {"user", "project"}:
                        continue
                    project = draft.get("project")
                    if row["scope"] == "project" and project != self.project:
                        continue
                    text = draft.get("text")
                    if not isinstance(text, str):
                        continue
                    self.service._text(text)
                    current, digest = self.service.read(row["name"], row["scope"], self.project)
                    item = next((self.items.item(i) for i in range(self.items.count())
                                 if self.items.item(i).data(Qt.ItemDataRole.UserRole)["name"] == row["name"]
                                 and self.items.item(i).data(Qt.ItemDataRole.UserRole)["scope"] == row["scope"]), None)
                    if item is None:
                        continue
                    self.items.setCurrentItem(item)
                    self.current = item.data(Qt.ItemDataRole.UserRole)
                    self.original, self.digest = current, draft.get("original_digest")
                    self.editor.setReadOnly(False)
                    self.editor.setPlainText(text)
                    self.draft_path = path
                    self.notice.setText("草稿已恢复；保存时仍会检查磁盘修改。")
                    return
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    self.error(exc)
            self.notice.setText("没有找到属于当前用户/项目且仍可编辑的技能草稿。")

        def closeEvent(self, event):
            # Owner shutdown must also preserve unfinished skill edits.
            if self.downloading:
                self.download_cancel.set()
                if not owner.closing:
                    self.notice.setText("正在取消技能文本下载，请稍候关闭。")
                    event.ignore()
                    return
            if owner.closing and self.editor.toPlainText() != self.original:
                try:
                    folder = contained(owner.settings.data, owner.settings.data / "skills-drafts")
                    folder.mkdir(exist_ok=True)
                    handle, target = tempfile.mkstemp(prefix="skill-draft-", suffix=".json", dir=folder)
                    with os.fdopen(handle, "w", encoding="utf-8") as file:
                        json.dump({"skill": self.current, "project": self.project,
                                   "text": self.editor.toPlainText(),
                                   "original_digest": self.digest,
                                   "base_text": self.original},
                                  file, ensure_ascii=False, indent=2)
                    self.original = self.editor.toPlainText()
                    self.draft_path = Path(target)
                    self.update_drafts()
                    event.accept()
                    return
                except Exception as exc:
                    self.error("技能草稿保留失败：" + str(exc))
            if self.clean():
                event.accept()
            else:
                event.ignore()

        def reject(self):
            self.close()

    return Dialog()
