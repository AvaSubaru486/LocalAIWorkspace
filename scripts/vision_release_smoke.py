"""Serial real application attachment smoke; short samples are not benchmarks."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import zipfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", default="images,video,switch,cancel")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    import psutil
    from PIL import Image, ImageDraw
    from app.config import Settings
    from app.workspace import Workspace
    from app.runlog import export_logs
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {"status": "RUNNING", "root": str(root), "cases": [], "speed_acceptance": False,
              "capability_retention_acceptance": False}
    def save():
        temp = output / "report.json.tmp"
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
        temp.replace(output / "report.json")
    def idle():
        active = [p.info for p in psutil.process_iter(["pid", "name"])
                  if (p.info["name"] or "").lower() in
                  {"llama-server.exe", "llama-cli.exe", "llama-bench.exe", "ninja.exe", "cc1plus.exe"}]
        assert not active, active
    idle()
    for name, color, shape in (("a", "red", "circle"), ("b", "blue", "square")):
        picture = Image.new("RGB", (256, 256), "white")
        draw = ImageDraw.Draw(picture)
        getattr(draw, "ellipse" if shape == "circle" else "rectangle")((50, 50, 206, 206), fill=color)
        picture.save(output / (name + ".png"))
    cases = set(args.cases.split(","))
    if "video" in cases:
        # Produce a two-second sequence before loading any model.
        pixels = b"".join(Image.open(output / (letter + ".png")).convert("RGB").tobytes() * 4
                          for letter in ("a", "b"))
        subprocess.run([str(root / "runtime/ffmpeg/ffmpeg.exe"), "-nostdin", "-v", "error",
                        "-f", "rawvideo", "-pixel_format", "rgb24", "-video_size", "256x256", "-framerate", "4", "-i", "pipe:0",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-threads", "2", str(output / "c.mp4")],
                       input=pixels, check=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        probe = subprocess.run([str(root / "runtime/ffmpeg/ffprobe.exe"), "-v", "error", "-show_entries",
                                "format=duration", "-of", "json", str(output / "c.mp4")],
                               capture_output=True, check=True, timeout=15,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert float(json.loads(probe.stdout)["format"]["duration"]) == 2.0
    settings = Settings(output / "isolated-user")
    settings.root = root
    settings.values.update(auxiliary_enabled=False, vision_model="", default_model="qwen3-8b",
                           max_new_tokens=32, auto_compress_context=False)
    workspace = Workspace(settings)
    owned = []
    save()
    def one(name, prompt, filename=None, *, text=False, cancel_after=None):
        sid = workspace.store.new_session()["id"]
        attachment = workspace.upload(sid, output / filename) if filename else None
        if attachment and attachment["kind"] == "video":
            saved = workspace.store.attachment(sid, attachment["id"])
            centers = []
            for frame in saved["meta"]["frames"]:
                with Image.open(Path(saved["path"]).parent / frame) as picture:
                    centers.append(picture.getpixel((picture.width // 2, picture.height // 2)))
            assert any(r > 200 and b < 20 for r, g, b in centers), centers
            assert any(b > 200 and r < 20 for r, g, b in centers), centers
        row = {"name": name, "prompt": prompt, "attachment": attachment, "events": [], "launches": []}
        report["cases"].append(row)
        stop = threading.Event()
        done = threading.Event()
        began = time.monotonic()
        def observe():
            last = 0
            while not done.wait(0.25):
                elapsed = time.monotonic() - began
                process = workspace.runtime.process
                if process is not None and all(p is not process for p in owned):
                    owned.append(process)
                    command = list(process.args)
                    if "--api-key" in command:
                        command[command.index("--api-key") + 1] = "[REDACTED]"
                    row["launches"].append({"pid": process.pid, "command": command})
                if elapsed - last >= 25:
                    last = elapsed
                    print(json.dumps({"case": name, "elapsed_s": round(elapsed),
                                      "available_mb": psutil.virtual_memory().available // 1024**2}), flush=True)
                    save()
                if elapsed > (cancel_after or 900):
                    row["cancellation_requested"] = True
                    stop.set()
                    workspace.runtime.unload()
                    return
        watcher = threading.Thread(target=observe, daemon=True)
        watcher.start()
        try:
            for event in workspace.chat(sid, prompt, model_id="gemma3-27b-fast" if text else None,
                                        attachment_ids=[attachment["id"]] if attachment else [], cancel=stop):
                row["events"].append(event)
                if event.get("type") in {"error", "done"}:
                    print(json.dumps({"case": name, "event": event}, ensure_ascii=False), flush=True)
            row["seconds"] = round(time.monotonic() - began, 3)
            row["history"] = workspace.history(sid)["messages"]
            row["answer"] = "".join(e.get("text", "") for e in row["events"] if e.get("type") == "delta")
            row["runtime_variant"] = workspace.runtime._runtime_variant
            if cancel_after:
                assert stop.is_set() and workspace.runtime.process is None
            else:
                assert not stop.is_set() and any(e["type"] == "done" for e in row["events"]), row
                assert not any(e["type"] == "error" for e in row["events"]), row
                assert workspace.runtime.model_id == "gemma3-27b-fast"
                assert row["runtime_variant"] == ("text" if text else "vision")
                assert len(row["history"]) == 2 and row["history"][-1]["metrics"]["generated_tokens"] > 0
                assert row["history"][-1]["metrics"]["first_token_seconds"] > 0
            row["status"] = "PASSED"
        finally:
            done.set()
            watcher.join(timeout=5)
            save()
        return row
    try:
        question = "State the color and shape of the single object in the image. Reply with only two English words."
        if "images" in cases:
            for letter, expected in (("a", ("red", "circle")), ("b", ("blue", "square"))):
                row = one(letter, question, letter + ".png")
                assert all(word in row["answer"].lower() for word in expected), row["answer"]
        if "video" in cases:
            row = one("video", "For each of the two distinct objects in these sampled frames, state BOTH its COLOR and its SHAPE. Include the colors explicitly. Reply only in this format: color shape; color shape.", "c.mp4")
            assert len(row["attachment"]["meta"]["frames"]) >= 2
            assert all(word in row["answer"].lower() for word in ("red", "circle", "blue", "square")), row["answer"]
        if "switch" in cases:
            if workspace.runtime._runtime_variant != "vision":
                row = one("switch-image", question, "a.png")
                assert "red" in row["answer"].lower() and "circle" in row["answer"].lower()
            one("text-switch", "What is 2 plus 3? Reply with one digit.", text=True)
        if "cancel" in cases:
            one("cancel-vision", question, "a.png", cancel_after=30)
        workspace.close()
        report["owned_processes_exited"] = all(p.poll() is not None for p in owned)
        assert report["owned_processes_exited"]
        archive = export_logs(settings, output / "runtime-logs.zip")
        with zipfile.ZipFile(archive) as z:
            assert z.testzip() is None
            contents = b"".join(z.read(name) for name in z.namelist())
            assert question.encode() not in contents and str(output).encode() not in contents
            records = [json.loads(line) for line in z.read("runs.jsonl").decode().splitlines()]
            generations = [r for r in records if r["kind"] == "generation"]
            assert len(generations) == len(report["cases"])
            for row in generations:
                if row.get("runtime_variant") == "vision":
                    assert row["runtime_options"]["ubatch_size"] == 256
            report["exported_metrics"] = generations
        report["log_zip_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
        report["status"] = "PASSED_APPLICATION_FUNCTIONALITY_ONLY"
    except BaseException as exc:
        report.update(status="FAILED", error=repr(exc))
        raise
    finally:
        workspace.close()
        report["owned_processes_exited"] = all(p.poll() is not None for p in owned)
        save()
    print(json.dumps({"status": report["status"], "report": str(output / "report.json")}), flush=True)


if __name__ == "__main__":
    main()
