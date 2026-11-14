"""Manual project patches, verification commands and review evidence."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from .config import contained
from .project_tasks import ProjectTasks


def ProjectTasksDialog(owner):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (QCheckBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                                  QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton,
                                  QSpinBox, QSplitter, QTabWidget, QVBoxLayout, QWidget)

    class Dialog(QDialog):
        def __init__(self):
            super().__init__(owner)
            self.setWindowTitle("项目任务（高级）")
            self.resize(1120, 780)
            self.service = ProjectTasks(owner.settings)
            self.current = None
            self.fingerprint = None
            self.running = False
            self.close_requested = False
            self.cancel = threading.Event()
            layout = QVBoxLayout(self)
            introduction = QLabel("用于修改程序项目：创建任务 → 请 AI 提供修改 → 在副本中测试 → 检查后应用。需要已用 Git 保存版本的项目；日常聊天无需使用这里。")
            introduction.setWordWrap(True)
            layout.addWidget(introduction)
            self.project_label = QLabel()
            self.project_label.setTextFormat(Qt.TextFormat.PlainText)
            self.project_label.setWordWrap(True)
            layout.addWidget(self.project_label)
            row = QHBoxLayout()
            self.create_button = QPushButton("创建项目任务")
            self.create_button.clicked.connect(self.create_task)
            row.addWidget(self.create_button)
            self.refresh_button = QPushButton("刷新任务")
            self.refresh_button.clicked.connect(self.refresh)
            row.addWidget(self.refresh_button)
            self.prompt_buttons = []
            for title, kind in [("请 AI 帮忙修改", "implement"), ("请 AI 检查代码", "code"),
                                ("请 AI 核对需求", "requirements")]:
                control = QPushButton(title)
                control.setToolTip("将任务说明填入聊天输入框，由你确认后发送；不会自动执行修改。")
                control.clicked.connect(lambda checked=False, kind=kind: self.fill_prompt(kind))
                row.addWidget(control)
                self.prompt_buttons.append(control)
            row.addStretch()
            layout.addLayout(row)
            split = QSplitter()
            self.tasks = QListWidget()
            self.tasks.setObjectName("projectTasks")
            self.tasks.currentItemChanged.connect(self.select_task)
            split.addWidget(self.tasks)
            self.tabs = QTabWidget()
            self.detail = QPlainTextEdit()
            self.detail.setReadOnly(True)
            self.detail.setPlaceholderText("先点击“创建项目任务”，填写要解决的问题和怎样算完成。")
            self.tabs.addTab(self.detail, "1. 任务说明")
            patch_page = QWidget()
            box = QVBoxLayout(patch_page)
            self.patch = QPlainTextEdit()
            self.patch.setObjectName("projectTaskPatch")
            self.patch.setPlaceholderText("在这里粘贴 AI 给出的修改补丁（通常以 diff --git 开头）。点击下方按钮只修改任务副本，暂不改动原项目。")
            box.addWidget(self.patch)
            self.apply_button = QPushButton("将修改应用到副本")
            self.apply_button.clicked.connect(self.apply_patch)
            box.addWidget(self.apply_button)
            self.tabs.addTab(patch_page, "2. 填入修改")
            diff_page = QWidget()
            box = QVBoxLayout(diff_page)
            self.diff_output = QPlainTextEdit()
            self.diff_output.setReadOnly(True)
            self.diff_output.setObjectName("projectTaskDiff")
            box.addWidget(self.diff_output)
            self.diff_button = QPushButton("刷新改动内容")
            self.diff_button.setToolTip("重新读取任务副本的实际改动，并检查已有验证是否仍然有效。")
            self.diff_button.clicked.connect(self.refresh_diff)
            box.addWidget(self.diff_button)
            self.tabs.addTab(diff_page, "3. 查看改动")
            validation_page = QWidget()
            box = QVBoxLayout(validation_page)
            self.command = QPlainTextEdit()
            self.command.setMaximumHeight(120)
            self.command.setObjectName("projectTaskCommand")
            self.command.setPlaceholderText("用户验证命令；使用独立任务目录和内置 Python，只有点击运行才执行")
            box.addWidget(self.command)
            row = QHBoxLayout()
            self.timeout = QSpinBox()
            self.timeout.setRange(1, 300)
            self.timeout.setValue(60)
            self.timeout.setSuffix(" 秒")
            row.addWidget(QLabel("最多运行"))
            row.addWidget(self.timeout)
            self.validate_button = QPushButton("手动运行验证")
            self.validate_button.clicked.connect(self.validate)
            row.addWidget(self.validate_button)
            self.stop_button = QPushButton("停止验证")
            self.stop_button.clicked.connect(lambda: self.cancel.set())
            row.addWidget(self.stop_button)
            row.addStretch()
            box.addLayout(row)
            self.output = QPlainTextEdit()
            self.output.setReadOnly(True)
            self.output.setObjectName("projectTaskOutput")
            box.addWidget(self.output)
            self.tabs.addTab(validation_page, "4. 运行检查")
            review_page = QWidget()
            box = QVBoxLayout(review_page)
            self.code_review = QPlainTextEdit()
            self.code_review.setObjectName("projectTaskCodeReview")
            self.code_review.setPlaceholderText("代码审核：记录已检查的实际差异、问题和证据")
            box.addWidget(self.code_review)
            self.code_passed = QCheckBox("确认当前补丁的代码审核通过")
            box.addWidget(self.code_passed)
            self.requirement_review = QPlainTextEdit()
            self.requirement_review.setObjectName("projectTaskRequirementReview")
            self.requirement_review.setPlaceholderText("需求审核：逐项核对验收，记录已实现、缺失与未验证项")
            box.addWidget(self.requirement_review)
            self.requirement_passed = QCheckBox("确认当前补丁的需求审核通过")
            box.addWidget(self.requirement_passed)
            row = QHBoxLayout()
            self.record_button = QPushButton("保存检查结果")
            self.record_button.clicked.connect(self.record_reviews)
            row.addWidget(self.record_button)
            self.merge_button = QPushButton("确认应用到原项目")
            self.merge_button.clicked.connect(self.merge)
            row.addWidget(self.merge_button)
            box.addLayout(row)
            self.tabs.addTab(review_page, "5. 确认并应用")
            split.addWidget(self.tabs)
            split.setSizes([250, 870])
            layout.addWidget(split, 1)
            self.notice = QLabel("修改和检查先在独立副本中进行；最后一步才应用到原项目。AI 不会自动执行命令或确认检查通过。")
            self.notice.setTextFormat(Qt.TextFormat.PlainText)
            self.notice.setWordWrap(True)
            layout.addWidget(self.notice)
            self.controls = [self.tasks, self.create_button, self.refresh_button, self.apply_button,
                             self.diff_button, self.validate_button, self.record_button, self.merge_button,
                             *self.prompt_buttons, self.patch, self.command, self.code_review,
                             self.requirement_review, self.code_passed, self.requirement_passed]
            self.refresh()

        def project(self):
            state = contained(owner.settings.data, owner.settings.data / "project-tools.json")
            try:
                if state.stat().st_size > 65536:
                    raise ValueError("项目记录过大")
                project = json.loads(state.read_text("utf-8"))["project"]
                if not isinstance(project, str) or not Path(project).is_absolute():
                    raise ValueError("请显式选择项目绝对路径")
                self.project_label.setText("当前项目：" + project)
                return project
            except (OSError, ValueError, KeyError, TypeError):
                self.project_label.setText("先在“文件与项目”打开已用 Git 保存版本的文件夹；已有任务仍可打开。")
                return None

        def draft_path(self, task_id):
            folder = contained(owner.settings.data, owner.settings.data / "project-task-drafts")
            folder.mkdir(exist_ok=True)
            # IDs come from the validated service, never from a filename input.
            self.service._directory(task_id)
            return contained(folder, folder / (task_id + ".json"))

        def save_draft(self):
            if self.current is None:
                return
            path = self.draft_path(self.current["id"])
            snapshot = {"patch": self.patch.toPlainText(), "command": self.command.toPlainText(),
                        "code": self.code_review.toPlainText(), "requirements": self.requirement_review.toPlainText(),
                        "code_passed": self.code_passed.isChecked(),
                        "requirements_passed": self.requirement_passed.isChecked(), "fingerprint": self.fingerprint}
            handle, temporary = tempfile.mkstemp(prefix="task-draft-", suffix=".tmp", dir=path.parent)
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as file:
                    json.dump(snapshot, file, ensure_ascii=False, indent=2)
                Path(temporary).replace(path)
            finally:
                Path(temporary).unlink(missing_ok=True)

        def update_controls(self):
            active = self.current is not None and self.current["status"] != "applied"
            for control in self.controls:
                control.setEnabled(not self.running)
            for control in (self.apply_button, self.validate_button, self.record_button, self.merge_button,
                            self.patch, self.command, self.code_review, self.requirement_review,
                            self.code_passed, self.requirement_passed, *self.prompt_buttons):
                control.setEnabled(active and not self.running)
            self.diff_button.setEnabled(self.current is not None and not self.running)
            self.stop_button.setEnabled(self.running)

        def run_task(self, function, done):
            if self.running or owner.closing:
                return
            self.running = True
            self.cancel = threading.Event()
            self.update_controls()
            self.notice.setText("正在处理，请稍候…")
            def finish():
                self.running = False
                self.update_controls()
                if self.close_requested:
                    self.close()
            job = owner.task(function, result=done, finished=finish)
            job.error.connect(self.notice.setText, Qt.ConnectionType.QueuedConnection)

        def refresh(self, select=None):
            try:
                self.save_draft()
                self.project()
                rows = self.service.list()
                chosen = select or (self.current["id"] if self.current else None)
                self.tasks.blockSignals(True)
                self.tasks.clear()
                selected = None
                for task in rows:
                    item = QListWidgetItem(task["title"] + " · " + task["status"])
                    item.setData(Qt.ItemDataRole.UserRole, task["id"])
                    item.setToolTip(task["worktree"])
                    self.tasks.addItem(item)
                    if task["id"] == chosen:
                        selected = item
                self.tasks.setCurrentItem(selected or (self.tasks.item(0) if self.tasks.count() else None))
                self.tasks.blockSignals(False)
                self.load_task(self.tasks.currentItem())
            except Exception as exc:
                self.tasks.blockSignals(False)
                self.notice.setText(str(exc))

        def select_task(self, item, previous=None):
            try:
                self.save_draft()
                self.load_task(item)
            except Exception as exc:
                self.tasks.blockSignals(True)
                self.tasks.setCurrentItem(previous)
                self.tasks.blockSignals(False)
                self.notice.setText(str(exc))

        def load_task(self, item):
            task = self.service.get(item.data(Qt.ItemDataRole.UserRole)) if item else None
            self.current, self.fingerprint = task, None
            for field in (self.detail, self.patch, self.command, self.diff_output, self.output,
                          self.code_review, self.requirement_review):
                field.clear()
            self.code_passed.setChecked(False)
            self.requirement_passed.setChecked(False)
            if task is not None:
                self.detail.setPlainText("任务：" + task["title"] + "\n需求与验收：\n" + task["requirements"]
                                         + "\n\n主项目：" + task["project"] + "\n独立任务目录：" + task["worktree"]
                                         + "\n基准提交：" + task["base"] + "\n状态：" + task["status"])
                reviews = task.get("reviews", {})
                self.code_review.setPlainText(reviews.get("code", {}).get("text", ""))
                self.requirement_review.setPlainText(reviews.get("requirements", {}).get("text", ""))
                self.code_passed.setChecked(bool(reviews.get("code", {}).get("passed")))
                self.requirement_passed.setChecked(bool(reviews.get("requirements", {}).get("passed")))
                if task.get("validation"):
                    self.command.setPlainText(task["validation"]["command"])
                    self.output.setPlainText(json.dumps(task["validation"], ensure_ascii=False, indent=2))
                path = self.draft_path(task["id"])
                if path.is_file():
                    if path.stat().st_size > 10 * 1024**2:
                        raise ValueError("任务草稿超过 10 MiB")
                    draft = json.loads(path.read_text("utf-8"))
                    for key, field in [("patch", self.patch), ("command", self.command),
                                       ("code", self.code_review), ("requirements", self.requirement_review)]:
                        if isinstance(draft.get(key), str):
                            field.setPlainText(draft[key])
                    self.code_passed.setChecked(draft.get("code_passed") is True)
                    self.requirement_passed.setChecked(draft.get("requirements_passed") is True)
                    self.fingerprint = draft.get("fingerprint")
                self.refresh_diff(show=False, keep_fingerprint=True)
            self.update_controls()

        def create_task(self):
            project = self.project()
            if project is None:
                self.notice.setText("请先在“文件与项目”打开已用 Git 保存版本的项目文件夹。")
                return
            from PySide6.QtWidgets import QDialogButtonBox
            prompt = QDialog(self)
            prompt.setWindowTitle("创建独立项目任务")
            box = QVBoxLayout(prompt)
            title = QLineEdit()
            title.setObjectName("newProjectTaskTitle")
            title.setPlaceholderText("任务标题")
            requirements = QPlainTextEdit()
            requirements.setObjectName("newProjectTaskRequirements")
            requirements.setPlaceholderText("需求与可验证的验收条件；任务从当前干净 Git 提交创建")
            box.addWidget(title)
            box.addWidget(requirements)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            buttons.accepted.connect(prompt.accept)
            buttons.rejected.connect(prompt.reject)
            box.addWidget(buttons)
            if prompt.exec() == QDialog.DialogCode.Accepted:
                name, text, model = title.text(), requirements.toPlainText(), owner.models.currentData()
                self.run_task(lambda _: self.service.create(project, name, text, model), self.created)

        def created(self, task):
            self.refresh(task["id"])
            self.notice.setText("独立任务已创建，请准备补丁并手动检查、验证与审核。")

        def refresh_diff(self, show=True, keep_fingerprint=False):
            if self.current is None:
                return
            result = self.service.diff(self.current["id"])
            if not keep_fingerprint:
                self.fingerprint = result["fingerprint"]
            self.diff_output.setPlainText("补丁指纹：" + result["fingerprint"]
                                          + "\n未纳入补丁文件：\n" + (result["untracked"] or "无")
                                          + "\n实际差异：\n" + result["patch"])
            if show:
                self.tabs.setCurrentIndex(2)
                self.notice.setText("已读取实际差异；后续审核绑定当前指纹。")

        def apply_patch(self):
            if self.current is None:
                return
            task_id, text = self.current["id"], self.patch.toPlainText()
            if QMessageBox.question(self, "应用任务补丁", "将编辑区补丁应用到以下独立目录？\n" + self.current["worktree"]) != QMessageBox.StandardButton.Yes:
                return
            self.run_task(lambda _: self.service.apply_patch(task_id, text, user_initiated=True), self.applied)

        def applied(self, result):
            self.current = self.service.get(self.current["id"])
            self.fingerprint = result["fingerprint"]
            self.patch.clear()
            self.code_passed.setChecked(False)
            self.requirement_passed.setChecked(False)
            self.output.clear()
            self.refresh_diff()
            self.save_draft()
            self.notice.setText("补丁已应用到任务目录；旧证据已失效，请重新验证与审核。")

        def validate(self):
            if self.current is None or not self.command.toPlainText().strip():
                self.notice.setText("请填写验证命令")
                return
            task_id, command, timeout = self.current["id"], self.command.toPlainText(), self.timeout.value()
            self.save_draft()
            self.tabs.setCurrentIndex(3)
            self.run_task(lambda _: self.service.validate(task_id, command, user_initiated=True,
                                                          timeout=timeout, cancel=self.cancel), self.validated)

        def validated(self, result):
            self.current = self.service.get(self.current["id"])
            self.output.setPlainText(json.dumps(result, ensure_ascii=False, indent=2))
            self.notice.setText("验证通过" if result["passed"] else "验证失败或补丁发生变化；请检查真实输出。")

        def record_reviews(self):
            if self.current is None or not self.fingerprint:
                self.notice.setText("请先查看实际差异，审核必须绑定已查看的补丁指纹")
                return
            texts = [("code", self.code_review.toPlainText(), self.code_passed.isChecked()),
                     ("requirements", self.requirement_review.toPlainText(), self.requirement_passed.isChecked())]
            if any(not text.strip() for _, text, _ in texts):
                self.notice.setText("请填写两项审核文本；仅勾选通过不能作为审核证据")
                return
            task_id, fingerprint = self.current["id"], self.fingerprint
            def record(_):
                for kind, text, passed in texts:
                    self.service.record_review(task_id, kind, text, passed, fingerprint)
                return self.service.get(task_id)
            def done(task):
                self.current = task
                self.save_draft()
                self.notice.setText("当前补丁的两项审核已记录；人工应用仍需成功验证。")
            self.run_task(record, done)

        def merge(self):
            if self.current is None or not self.fingerprint:
                self.notice.setText("请先查看实际差异与补丁指纹")
                return
            task_id, fingerprint = self.current["id"], self.fingerprint
            text = ("将已验证、审核的补丁应用到主项目并保留为 Git 暂存改动？\n"
                    + self.current["project"] + "\n补丁指纹：" + fingerprint)
            if QMessageBox.question(self, "人工应用到主项目", text) != QMessageBox.StandardButton.Yes:
                return
            def done(task):
                self.current = task
                self.refresh(task_id)
                self.notice.setText("补丁已应用到主项目并保留为暂存改动，等待用户检查和提交。")
            self.run_task(lambda _: self.service.merge(task_id, fingerprint, user_initiated=True), done)

        def fill_prompt(self, kind):
            if self.current is None or owner.busy():
                self.notice.setText("请先选择任务并等待对话空闲")
                return
            if owner.composer.toPlainText().strip():
                self.notice.setText("对话输入框已有未发送内容，请先发送或保存，再填入任务提示词。")
                return
            try:
                owner.composer.setPlainText(self.service.prompt(self.current["id"], kind))
                owner.composer.setFocus()
                self.notice.setText("提示词已填入普通对话，用户检查后点击发送；模型不会自动应用补丁或运行命令。")
            except Exception as exc:
                self.notice.setText(str(exc))

        def closeEvent(self, event):
            try:
                self.save_draft()
            except Exception as exc:
                self.notice.setText("任务输入保留失败：" + str(exc))
                event.ignore()
                return
            if self.running and not owner.closing:
                self.close_requested = True
                self.cancel.set()
                self.notice.setText("正在停止验证，输入已保留，结束后关闭窗口。")
                event.ignore()
            else:
                self.cancel.set()
                event.accept()

        def showEvent(self, event):
            self.close_requested = False
            super().showEvent(event)

        def reject(self):
            self.close()

    return Dialog()
