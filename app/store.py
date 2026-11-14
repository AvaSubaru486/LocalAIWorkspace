from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def identifier(value: str):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("无效的文件或会话标识")
    return value


class Store:
    def __init__(self, data: Path):
        self.path = data / "workspace.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, title TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, session TEXT, role TEXT,
                    content TEXT, attachments TEXT, created REAL, metrics TEXT);
                CREATE TABLE IF NOT EXISTS attachments(id TEXT PRIMARY KEY, session TEXT, name TEXT,
                    kind TEXT, size INTEGER, path TEXT, text TEXT, meta TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS legacy_imports(source TEXT, legacy_id TEXT,
                    PRIMARY KEY(source, legacy_id));
                CREATE TABLE IF NOT EXISTS context_summaries(session TEXT PRIMARY KEY, content TEXT,
                    source_ids TEXT, source_digest TEXT, model TEXT, covered_turns INTEGER,
                    version INTEGER, created REAL);
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(messages)")}
            if "metrics" not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN metrics TEXT")
        self._import_legacy_history(data)

    def _import_legacy_history(self, data):
        source = data / "conversation-summaries.json"
        report = {"source": source.name, "imported_sessions": 0, "imported_messages": 0,
                  "existing_sessions": 0, "already_processed": 0, "issues": []}
        self.migration_report = report
        if not source.is_file():
            return
        try:
            if source.stat().st_size > 32 * 1024 * 1024:
                raise ValueError("旧历史文件超过 32 MiB")
            records = json.loads(source.read_text("utf-8-sig"))
            if not isinstance(records, list):
                raise ValueError("旧历史必须是 JSON 数组")
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                for index, record in enumerate(records):
                    try:
                        if not isinstance(record, dict):
                            raise ValueError("会话记录必须是对象")
                        sid = identifier(record.get("Id"))
                        title, messages = record.get("Title"), record.get("Messages")
                        if not isinstance(title, str) or not isinstance(messages, list):
                            raise ValueError("会话缺少有效标题或消息数组")
                    except ValueError as exc:
                        report["issues"].append({"record": index, "reason": str(exc)})
                        continue
                    if db.execute("SELECT 1 FROM legacy_imports WHERE source=? AND legacy_id=?", (source.name, sid)).fetchone():
                        report["already_processed"] += 1
                        continue
                    if db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
                        report["existing_sessions"] += 1
                        db.execute("INSERT INTO legacy_imports VALUES (?,?)", (source.name, sid))
                        continue
                    try:
                        created = datetime.fromisoformat(record["CreatedAt"])
                        if created.tzinfo is None:
                            created = created.replace(tzinfo=timezone.utc)
                        stamp = created.timestamp()
                    except (KeyError, TypeError, ValueError, OverflowError):
                        stamp = source.stat().st_mtime
                        report["issues"].append({"record": index, "reason": "无效创建时间，使用源文件时间"})
                    db.execute("INSERT INTO sessions VALUES (?,?,?)", (sid, title, stamp))
                    for message_index, message in enumerate(messages):
                        if not isinstance(message, dict):
                            report["issues"].append({"record": index, "message": message_index, "reason": "消息必须是对象"})
                            continue
                        role = message.get("role", message.get("Role"))
                        content = message.get("content", message.get("Content"))
                        if not isinstance(role, str) or role not in {"user", "assistant", "system", "tool"} or not isinstance(content, str):
                            report["issues"].append({"record": index, "message": message_index, "reason": "无效消息角色或内容"})
                            continue
                        mid = uuid.uuid5(uuid.NAMESPACE_URL, f"localai:legacy:{sid}:{message_index}").hex
                        db.execute("INSERT INTO messages(id,session,role,content,attachments,created,metrics) VALUES (?,?,?,?,?,?,?)",
                                   (mid, sid, role, content, "[]", stamp + message_index * 0.001, None))
                        report["imported_messages"] += 1
                    # Mode is ignored: every imported session is a regular conversation.
                    db.execute("INSERT INTO legacy_imports VALUES (?,?)", (source.name, sid))
                    report["imported_sessions"] += 1
        except (OSError, ValueError, RecursionError) as exc:
            report["issues"].append({"reason": str(exc)})
        try:
            (data / "logs").mkdir(exist_ok=True)
            (data / "logs/history-migration.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
        except OSError:
            pass

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def new_session(self):
        row = {"id": uuid.uuid4().hex, "title": "新对话", "created": time.time()}
        with self.connect() as db:
            db.execute("INSERT INTO sessions VALUES (:id,:title,:created)", row)
        return row

    def session(self, sid):
        identifier(sid)
        with self.connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if row is None:
            raise ValueError("会话不存在")
        return dict(row)

    def sessions(self):
        with self.connect() as db:
            return [dict(x) for x in db.execute("SELECT * FROM sessions ORDER BY created DESC")]

    def messages(self, sid):
        self.session(sid)
        with self.connect() as db:
            rows = [dict(x) for x in db.execute("SELECT * FROM messages WHERE session=? ORDER BY created, rowid", (sid,))]
        for row in rows:
            row["attachments"] = json.loads(row["attachments"])
            row["metrics"] = json.loads(row["metrics"]) if row["metrics"] else None
        return rows

    def add_message(self, sid, role, content, attachments=None, metrics=None):
        self.session(sid)
        mid = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO messages(id,session,role,content,attachments,created,metrics) VALUES (?,?,?,?,?,?,?)", (mid, sid, role, content,
                       json.dumps(attachments or []), time.time(), json.dumps(metrics, ensure_ascii=False) if metrics else None))
            if role == "user":
                db.execute("UPDATE sessions SET title=? WHERE id=? AND title='新对话'", (content[:40] or "附件对话", sid))
        return mid

    def save_attachment(self, row):
        with self.connect() as db:
            db.execute("INSERT INTO attachments VALUES (:id,:session,:name,:kind,:size,:path,:text,:meta,:created)",
                       row | {"meta": json.dumps(row["meta"], ensure_ascii=False)})

    def context_summary(self, sid):
        self.session(sid)
        with self.connect() as db:
            row = db.execute("SELECT * FROM context_summaries WHERE session=?", (sid,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["source_ids"] = json.loads(result["source_ids"])
        return result

    def save_context_summary(self, sid, content, source_ids, source_digest, model, covered_turns, version):
        """Cache derived context separately; the original messages are never updated."""
        identifier(sid)
        if not isinstance(content, str) or not content.strip() or len(content) > 16000:
            raise ValueError("摘要内容为空或超过上限")
        if not source_ids or len(set(source_ids)) != len(source_ids):
            raise ValueError("摘要来源消息无效")
        if (not isinstance(source_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", source_digest) or
                not isinstance(model, str) or not model or type(covered_turns) is not int or covered_turns < 1 or
                type(version) is not int or version != 1):
            raise ValueError("摘要来源元数据无效")
        for mid in source_ids:
            identifier(mid)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
                raise ValueError("会话不存在")
            existing = {row["id"] for row in db.execute("SELECT id FROM messages WHERE session=? AND role IN ('user','assistant')", (sid,))}
            if not set(source_ids).issubset(existing):
                raise ValueError("摘要来源消息不属于当前会话")
            db.execute("INSERT OR REPLACE INTO context_summaries VALUES (?,?,?,?,?,?,?,?)",
                       (sid, content.strip(), json.dumps(source_ids), source_digest, model,
                        covered_turns, version, time.time()))

    def discard_context_summary(self, sid):
        identifier(sid)
        with self.connect() as db:
            db.execute("DELETE FROM context_summaries WHERE session=?", (sid,))

    def attachments(self, sid):
        self.session(sid)
        with self.connect() as db:
            rows = [dict(x) for x in db.execute("SELECT * FROM attachments WHERE session=? ORDER BY created", (sid,))]
        for row in rows:
            row["meta"] = json.loads(row["meta"])
        return rows

    def attachment(self, sid, aid):
        identifier(aid)
        for row in self.attachments(sid):
            if row["id"] == aid:
                return row
        raise ValueError("附件不存在、已过期或不属于当前会话")

    def remove_attachment(self, aid):
        with self.connect() as db:
            db.execute("DELETE FROM attachments WHERE id=?", (aid,))

    def delete_session(self, sid):
        self.session(sid)
        with self.connect() as db:
            for table in ("messages", "attachments", "context_summaries"):
                db.execute(f"DELETE FROM {table} WHERE session=?", (sid,))
            db.execute("DELETE FROM sessions WHERE id=?", (sid,))
