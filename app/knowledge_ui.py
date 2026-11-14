"""Native knowledge library controls; all imported content is displayed as text."""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QPushButton, QSplitter, QVBoxLayout,
)

from .knowledge import KnowledgeBase


class KnowledgeTask(QObject):
    completed = Signal(object, object)


class KnowledgeDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.knowledge = getattr(owner.workspace, "knowledge", None)
        if self.knowledge is None:
            self.knowledge = KnowledgeBase(owner.workspace.settings)
        self.task = None
        self.setWindowTitle("离线资料")
        self.resize(900, 690)
        layout = QVBoxLayout(self)
        note = QLabel("在这里查找电脑里的资料，也可以让 AI 回答时参考它们。内置英文维基百科精选资料含 4,985 个条目，断网也能查；查百科时建议使用英文关键词。")
        note.setToolTip("条目数不等于互不重复的完整文章数；这个精选包自身的说明为百篇精选。")
        note.setTextFormat(Qt.TextFormat.PlainText)
        note.setWordWrap(True)
        layout.addWidget(note)
        self.enabled = QCheckBox("回答时参考这些资料")
        self.enabled.setToolTip("开启后，下一次提问会查找相关段落。资料不会代替你的指令，也不会运行其中的代码。")
        self.enabled.setObjectName("knowledgeEnabled")
        self.enabled.setChecked(self.knowledge.values["enabled"])
        self.enabled.toggled.connect(self.set_enabled)
        layout.addWidget(self.enabled)
        controls = QHBoxLayout()
        self.import_button = QPushButton("添加资料")
        self.import_button.setToolTip("支持 TXT、Markdown、JSON、JSONL 和带全文索引的 ZIM 离线资料包。")
        self.import_button.setObjectName("importKnowledge")
        self.import_button.clicked.connect(self.choose_import)
        controls.addWidget(self.import_button)
        self.delete_button = QPushButton("移除所选资料")
        self.delete_button.setObjectName("deleteKnowledge")
        self.delete_button.clicked.connect(self.delete_selected)
        controls.addWidget(self.delete_button)
        controls.addStretch()
        layout.addLayout(controls)
        self.sources = QListWidget()
        self.sources.setObjectName("knowledgeSources")
        self.sources.setMaximumHeight(150)
        self.sources.itemSelectionChanged.connect(self._update_delete_button)
        layout.addWidget(self.sources)
        search_row = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setObjectName("knowledgeQuery")
        self.query.setPlaceholderText("输入关键词，例如：太阳能；英文百科可试 solar energy")
        self.query.returnPressed.connect(self.search)
        search_row.addWidget(self.query)
        self.search_button = QPushButton("搜索")
        self.search_button.setObjectName("searchKnowledge")
        self.search_button.clicked.connect(self.search)
        search_row.addWidget(self.search_button)
        layout.addLayout(search_row)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.results = QListWidget()
        self.results.setObjectName("knowledgeResults")
        self.results.itemSelectionChanged.connect(self.show_result)
        split.addWidget(self.results)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("搜索后，点击左侧结果即可阅读内容和来源。")
        self.preview.setObjectName("knowledgePreview")
        split.addWidget(self.preview)
        split.setStretchFactor(1, 2)
        layout.addWidget(split, 1)
        self.status = QLabel("")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.refresh()

    def refresh(self):
        report = self.knowledge.report()
        self.sources.clear()
        for row in report["sources"]:
            suffix = " · 内置资料" if row["kind"] == "bundled-zim" else f" · {row['kind'].upper()}"
            item = QListWidgetItem(f"{row['name']} · {row['entries']} 条{suffix}")
            item.setData(Qt.ItemDataRole.UserRole, row["id"])
            item.setData(Qt.ItemDataRole.UserRole + 1, row["kind"])
            self.sources.addItem(item)
        zim = report["zim"]
        entries = sum(row["entries"] for row in report["sources"])
        self.status.setText(f"共有 {len(report['sources'])} 份资料，{entries:,} 条内容。" +
                            ("可以离线搜索。" if zim["available"] else "离线百科暂时不可用，请运行安装检查；已添加的文本资料仍可搜索。"))
        self.status.setToolTip(f"百科读取组件：libzim {zim['version']}" if zim["available"] else zim["error"])
        self._update_delete_button()

    def _update_delete_button(self):
        item = self.sources.currentItem()
        bundled = item is not None and item.data(Qt.ItemDataRole.UserRole + 1) == "bundled-zim"
        self.delete_button.setText("内置资料已保留" if bundled else "移除所选资料")
        self.delete_button.setToolTip("内置百科随便携包提供；取消上方勾选可停止在回答中使用。" if bundled else "只移除软件内的副本，原文件保留。")
        self.delete_button.setEnabled(item is not None and not bundled and self.task is None)

    def set_enabled(self, checked):
        try:
            self.knowledge.set_enabled(checked)
            self.refresh()
            self.status.setText("下次回答会查找相关资料。" if checked else "回答时不再查找这些资料，你仍可在这里手动搜索。")
        except Exception as exc:
            self.enabled.blockSignals(True)
            self.enabled.setChecked(self.knowledge.values["enabled"])
            self.enabled.blockSignals(False)
            self.status.setText(str(exc))

    def _run(self, function, done):
        if self.task is not None or getattr(self.owner, "closing", False):
            return
        # The desktop owner tracks its Task objects and waits for them during
        # shutdown.  Keep the small fallback for isolated widget tests and
        # embedders that do not provide the desktop task manager.
        owner_task = getattr(self.owner, "task", None)
        if callable(owner_task):
            self._run_owner_task(owner_task, function, done)
            return
        self._run_thread(function, done)

    def _set_busy(self, busy):
        self.task = busy or None
        for widget in (self.import_button, self.search_button, self.enabled, self.query):
            widget.setEnabled(not busy)
        self._update_delete_button()
        if not busy and getattr(self.owner, "closing", False):
            self.close()

    def _run_owner_task(self, owner_task, function, done):
        self._set_busy(True)

        def work(_):
            try:
                return True, function()
            except Exception as exc:
                return False, str(exc)

        def result(payload):
            ok, value = payload
            if ok:
                done(value)
            else:
                self.status.setText(value)

        def finished():
            self._set_busy(False)

        self.task = owner_task(work, result=result, finished=finished)

    def _run_thread(self, function, done):
        task = KnowledgeTask(self)
        self._set_busy(task)

        def completed(result, error):
            self._set_busy(None)
            if error:
                self.status.setText(str(error))
            else:
                done(result)
            task.deleteLater()

        task.completed.connect(completed)

        def work():
            try:
                task.completed.emit(function(), None)
            except Exception as exc:
                task.completed.emit(None, str(exc))

        task.thread = threading.Thread(target=work, daemon=True)
        task.thread.start()

    def choose_import(self):
        filename, _ = QFileDialog.getOpenFileName(self, "选择要添加的资料", "",
            "资料文件 (*.txt *.md *.markdown *.json *.jsonl *.ndjson *.zim)")
        if filename:
            self.import_file(filename)

    def import_file(self, filename):
        self.status.setText("正在读取资料，较大的文件可能需要一点时间…")
        def done(row):
            self.refresh()
            self.status.setText(("该文件已导入：" if row["duplicate"] else "导入完成：") + row["name"] + f" · {row['entries']} 条")
        self._run(lambda: self.knowledge.import_file(filename), done)

    def delete_selected(self):
        item = self.sources.currentItem()
        if item is None:
            return
        if item.data(Qt.ItemDataRole.UserRole + 1) == "bundled-zim":
            self.status.setText("内置 Wikipedia 随资源包提供，无法删除；可取消勾选以停止用于普通对话。")
            return
        if QMessageBox.question(self, "移除资料", "从软件中移除所选资料？你导入时的原文件会保留。") != QMessageBox.StandardButton.Yes:
            return
        sid = item.data(Qt.ItemDataRole.UserRole)
        self._run(lambda: self.knowledge.delete_source(sid), lambda _: self.refresh())

    def search(self):
        query = self.query.text().strip()
        if not query:
            self.status.setText("先输入要查找的关键词，再点击搜索。")
            self.query.setFocus()
            return
        self.status.setText("正在查找本机资料…")
        def done(rows):
            self.results.clear()
            self.preview.clear()
            for row in rows:
                item = QListWidgetItem(row["title"] + "\n" + row["source_name"])
                item.setData(Qt.ItemDataRole.UserRole, row)
                self.results.addItem(item)
            self.status.setText(f"找到 {len(rows)} 条结果，点击即可阅读。" if rows else "没有找到相关内容。试试更短的关键词；内置英文百科建议用英文搜索。")
            if rows:
                self.results.setCurrentRow(0)
        self._run(lambda: self.knowledge.search(query), done)

    def show_result(self):
        item = self.results.currentItem()
        if item is not None:
            row = item.data(Qt.ItemDataRole.UserRole)
            self.preview.setPlainText(row["source"] + "\n来源：" + (row["url"] or row["source_name"]) + "\n\n" + row["text"])

    def reject(self):
        if self.task is None:
            super().reject()
        else:
            self.status.setText("知识库操作正在完成，请稍候关闭")

    def closeEvent(self, event):
        if self.task is not None:
            event.ignore()
            self.status.setText("知识库操作正在完成，请稍候关闭")
        else:
            super().closeEvent(event)
