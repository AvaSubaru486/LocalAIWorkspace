from __future__ import annotations

import csv
import io
import json
import shutil
import time
import uuid
import warnings
import zipfile
from pathlib import Path

from PIL import Image, ImageOps

from .config import contained
from .processes import bounded_run

IMAGES = {".jpg", ".jpeg", ".png", ".webp"}
VIDEOS = {".mp4", ".mov", ".avi", ".webm"}
DOCUMENTS = {".txt", ".md", ".markdown", ".pdf", ".docx", ".xlsx", ".pptx", ".csv"}
Image.MAX_IMAGE_PIXELS = 30_000_000


def safe_name(name):
    if not name or len(name) > 180 or any(c in name for c in '/\\:\x00') or any(ord(c) < 32 for c in name):
        raise ValueError("文件名不合法：不能包含路径、冒号或控制字符")
    if name in (".", "..") or name.endswith((" ", ".")):
        raise ValueError("文件名不合法")
    return name


def decode_text(data):
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    if b"\x00" in data:
        raise ValueError("文本文件包含二进制内容")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("无法读取文本编码，请转换为 UTF-8")


def check_office(path, expected):
    try:
        with zipfile.ZipFile(path) as z:
            entries = z.infolist()
            if len(entries) > 10000 or sum(i.file_size for i in entries) > 200 * 1024 * 1024:
                raise ValueError("文档解压后过大")
            for item in entries:
                parts = Path(item.filename.replace("\\", "/")).parts
                if ".." in parts or item.filename.startswith(("/", "\\")) or ":" in item.filename:
                    raise ValueError("文档包含非法路径")
                if item.file_size / max(item.compress_size, 1) > 1000:
                    raise ValueError("文档压缩比例异常")
            if expected not in z.namelist() or "[Content_Types].xml" not in z.namelist():
                raise ValueError("文件内容与扩展名不匹配")
    except zipfile.BadZipFile as exc:
        raise ValueError("文档已损坏") from exc


def extract_document(path: Path, limit: int):
    """Invoked in a disposable worker process, so malformed parsers have a deadline."""
    ext = path.suffix.lower()
    chunks, total, truncated = [], 0, False
    extracted_pdf_text = False

    def add(text):
        nonlocal total, truncated
        if not text:
            return
        remaining = max(0, limit - total)
        chunks.append(str(text)[:remaining])
        total += len(str(text))
        truncated = truncated or total > limit

    if ext in {".txt", ".md", ".markdown", ".csv"}:
        text = decode_text(path.read_bytes())
        if ext == ".csv":
            for row in csv.reader(io.StringIO(text)):
                add(" | ".join(row) + "\n")
                if total >= limit:
                    break
        else:
            add(text)
    elif ext == ".pdf":
        if not path.read_bytes()[:1024].lstrip().startswith(b"%PDF-"):
            raise ValueError("文件内容不是 PDF")
        from pypdf import PdfReader
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ValueError("暂不支持加密 PDF，请先解除密码")
        for index, page in enumerate(reader.pages[:200]):
            page_text = (page.extract_text(extraction_mode="layout") or "") if "/Contents" in page else ""
            extracted_pdf_text = extracted_pdf_text or bool(page_text.strip())
            add(f"\n[第 {index + 1} 页]\n" + page_text)
            if total >= limit:
                break
        if len(reader.pages) > 200:
            truncated = True
    elif ext == ".docx":
        check_office(path, "word/document.xml")
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        for item in Document(path).iter_inner_content():
            if isinstance(item, Paragraph):
                add(item.text + "\n")
            elif isinstance(item, Table):
                for row in item.rows:
                    add(" | ".join(cell.text for cell in row.cells) + "\n")
            if total >= limit:
                break
    elif ext == ".xlsx":
        check_office(path, "xl/workbook.xml")
        from openpyxl import load_workbook
        book = load_workbook(path, read_only=True, data_only=True)
        try:
            for sheet in book:
                if (sheet.max_row or 0) > 10000 or (sheet.max_column or 0) > 100:
                    truncated = True
                add(f"\n[工作表 {sheet.title}]\n")
                for row in sheet.iter_rows(max_row=min(sheet.max_row or 10000, 10000), max_col=min(sheet.max_column or 100, 100), values_only=True):
                    add(" | ".join("" if v is None else str(v) for v in row) + "\n")
                    if total >= limit:
                        break
                if total >= limit:
                    break
        finally:
            book.close()
    elif ext == ".pptx":
        check_office(path, "ppt/presentation.xml")
        from pptx import Presentation
        for index, slide in enumerate(Presentation(path).slides):
            add(f"\n[幻灯片 {index + 1}]\n")
            for shape in slide.shapes:
                if shape.has_text_frame:
                    add(shape.text + "\n")
                if shape.has_table:
                    for row in shape.table.rows:
                        add(" | ".join(c.text for c in row.cells) + "\n")
            if total >= limit:
                break
    else:
        raise ValueError("不支持的文档格式")
    text = "".join(chunks)
    if not text.strip() or (ext == ".pdf" and not extracted_pdf_text):
        raise ValueError("没有可提取的文字；扫描件请转为图片后使用视觉模型进行 OCR")
    return {"text": text[:limit], "meta": {"truncated": truncated}}


class Attachments:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store

    def directory(self, sid):
        self.store.session(sid)
        path = contained(self.settings.data / "sessions", self.settings.data / "sessions" / sid / "attachments")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def prepare(self, sid, name):
        safe_name(name)
        ext = Path(name).suffix.lower()
        if ext not in IMAGES | VIDEOS | DOCUMENTS:
            raise ValueError("不支持此格式。可上传图片、视频、TXT、Markdown、PDF、DOCX、XLSX、PPTX、CSV。")
        aid = uuid.uuid4().hex
        folder = self.directory(sid) / aid
        folder.mkdir()
        kind = "image" if ext in IMAGES else "video" if ext in VIDEOS else "document"
        return {"id": aid, "session": sid, "name": name, "kind": kind,
                "path": str(folder / ("original" + ext)), "text": "", "meta": {}, "created": time.time()}

    def process(self, row):
        path = Path(row["path"])
        row["size"] = path.stat().st_size
        if row["size"] == 0:
            raise ValueError("附件为空")
        if row["kind"] == "image":
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(path) as check:
                    expected = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP"}
                    if check.format != expected[path.suffix]:
                        raise ValueError("图片内容与扩展名不匹配")
                    check.verify()
                with Image.open(path) as source:
                    img = ImageOps.exif_transpose(source).convert("RGB")
                    row["meta"] = {"width": img.width, "height": img.height}
                    img.thumbnail((1600, 1600))
                    img.save(path.parent / "preview.jpg", quality=88)
        elif row["kind"] == "video":
            self.video(row)
        else:
            result = bounded_run([str(self.settings.python), "-I", "-m", "app.document_worker", str(path),
                                  str(self.settings["max_document_chars"])], cwd=path.parent,
                                 timeout=30, limit=2 * 1024 * 1024, memory_limit=2 * 1024**3)
            if result["reason"] or result["returncode"]:
                raise ValueError("文档解析失败或超过限制：" + (result["stderr"][-400:] or str(result["reason"])))
            parsed = json.loads(result["stdout"])
            if "error" in parsed:
                raise ValueError(parsed["error"])
            row.update(parsed)
        self.store.save_attachment(row)
        return row

    def video(self, row):
        path = Path(row["path"])
        result = bounded_run([str(self.settings.binary("ffprobe")), "-v", "error", "-show_format", "-show_streams",
                              "-of", "json", str(path)], cwd=path.parent, timeout=20, limit=262144, memory_limit=1024**3)
        if result["reason"] or result["returncode"]:
            raise ValueError("视频已损坏或无法识别")
        info = json.loads(result["stdout"])
        streams = [x for x in info.get("streams", []) if x.get("codec_type") == "video"]
        if not streams:
            raise ValueError("文件中没有视频轨道")
        fmt = info.get("format", {}).get("format_name", "")
        expected = {".mp4": "mp4", ".mov": "mov", ".avi": "avi", ".webm": "webm"}
        if expected[path.suffix] not in fmt:
            raise ValueError("视频内容与扩展名不匹配")
        duration = float(info.get("format", {}).get("duration", 0))
        if not 0 < duration <= 6 * 3600:
            raise ValueError("视频时长无法读取或超过 6 小时")
        count = min(self.settings["max_video_frames"], max(1, int(duration) + 1))
        timestamps = [round(duration * i / count, 2) for i in range(count)]
        frames = []
        for index, timestamp in enumerate(timestamps):
            out = path.parent / f"frame-{index}.jpg"
            result = bounded_run([str(self.settings.binary("ffmpeg")), "-nostdin", "-v", "error", "-ss", str(timestamp),
                                  "-i", str(path), "-frames:v", "1", "-vf", "scale=1280:1280:force_original_aspect_ratio=decrease",
                                  "-threads", "2", "-y", str(out)], cwd=path.parent, timeout=25, limit=131072,
                                 memory_limit=2 * 1024**3)
            if result["returncode"] or result["reason"] or not out.is_file():
                raise ValueError("视频抽帧失败：" + result["stderr"][-200:])
            frames.append(out.name)
        shutil.copy2(path.parent / frames[0], path.parent / "preview.jpg")
        row["meta"] = {"duration": duration, "width": streams[0].get("width"), "height": streams[0].get("height"),
                       "frames": frames, "timestamps": timestamps, "audio_transcribed": False}
        row["text"] = f"视频时长 {duration:.2f} 秒，抽样时间点 {timestamps} 秒。只分析这些画面，未进行音频转写。"

    @staticmethod
    def public(row):
        public = {k: row[k] for k in ("id", "session", "name", "kind", "size", "meta", "created")}
        if row["kind"] == "document":
            public["preview"] = row["text"][:240]
        return public

    def remove(self, sid, aid):
        row = self.store.attachment(sid, aid)
        folder = contained(self.directory(sid), Path(row["path"]).parent)
        shutil.rmtree(folder)
        self.store.remove_attachment(aid)

    def cleanup(self, session_locks=None):
        cutoff = time.time() - self.settings["attachment_ttl_hours"] * 3600
        for session in self.store.sessions():
            sid = session["id"]
            lock = session_locks.get(sid) if session_locks else None
            acquired = False
            if lock is not None:
                if not lock.acquire(False):
                    continue
                acquired = True
            try:
                rows = self.store.attachments(sid)
                for row in rows:
                    if row["created"] < cutoff:
                        try:
                            self.remove(sid, row["id"])
                        except FileNotFoundError:
                            self.store.remove_attachment(row["id"])
                known = {r["id"] for r in self.store.attachments(sid)}
                directory = self.directory(sid)
                for folder in directory.iterdir():
                    if folder.name not in known and folder.stat().st_mtime < time.time() - self.settings["unfinished_upload_ttl_hours"] * 3600:
                        shutil.rmtree(contained(directory, folder))
                runs = self.settings.data / "sessions" / sid / "runs"
                if runs.exists():
                    for folder in runs.iterdir():
                        if folder.stat().st_mtime < time.time() - self.settings["unfinished_upload_ttl_hours"] * 3600:
                            shutil.rmtree(contained(self.settings.data / "sessions", folder))
            finally:
                if acquired:
                    lock.release()
