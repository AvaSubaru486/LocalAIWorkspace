"""Build-machine provisioning. End users do not need network or a system Python."""
import concurrent.futures
import hashlib
import json
import pathlib
import shutil
import urllib.request
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
BUILD.mkdir(exist_ok=True)


def fetch(url, path, digest=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and (not digest or hash_file(path) == digest):
        return path
    partial = path.with_suffix(path.suffix + ".part")
    offset = partial.stat().st_size if partial.exists() else 0
    req = urllib.request.Request(url, headers={"User-Agent": "LocalAIWorkspace/1.0", **({"Range": f"bytes={offset}-"} if offset else {})})
    with urllib.request.urlopen(req, timeout=120) as response:
        resume = response.status == 206 and response.headers.get("Content-Range", "").startswith(f"bytes {offset}-")
        with partial.open("ab" if resume else "wb") as out:
            shutil.copyfileobj(response, out, 4 * 1024 * 1024)
    if digest and hash_file(partial) != digest:
        raise ValueError(f"Checksum mismatch: {path}")
    partial.replace(path)
    print("Downloaded", path, path.stat().st_size, flush=True)
    return path


def hash_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def qwen():
    spec = json.loads((ROOT / "config/models.json").read_text("utf-8"))["models"][0]
    item = spec["files"][0]
    fetch(item["urls"][0], ROOT / spec["path"] / item["path"], item["sha256"])
    fetch("https://modelscope.cn/models/Qwen/Qwen3-8B-GGUF/resolve/master/LICENSE", ROOT / spec["path"] / "LICENSE")


def auxiliary():
    base = ROOT / "data/auxiliary"
    for name in ["rl_agent_config.json", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "config.json", "README.md"]:
        try:
            fetch(f"https://hf-mirror.com/convaiinnovations/laya/resolve/main/{name}", base / name)
        except Exception as exc:
            print("Laya file", name, str(exc), flush=True)
    # Laya constructs ModernBERT from its architecture config, never its full base weights.
    for name in ["config.json", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json"]:
        try:
            fetch(f"https://hf-mirror.com/answerdotai/ModernBERT-large/resolve/main/{name}", base / "encoder" / name)
        except Exception as exc:
            print("Encoder file", name, str(exc), flush=True)


def ffmpeg():
    package = fetch("https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip", BUILD / "ffmpeg.zip")
    target = ROOT / "runtime/ffmpeg"
    target.mkdir(exist_ok=True)
    with zipfile.ZipFile(package) as archive:
        for item in archive.infolist():
            name = pathlib.PurePosixPath(item.filename).name
            if name in {"ffmpeg.exe", "ffprobe.exe", "LICENSE", "README.txt"}:
                with archive.open(item) as src, (target / name).open("wb") as out:
                    shutil.copyfileobj(src, out)


if __name__ == "__main__":
    with concurrent.futures.ThreadPoolExecutor(3) as executor:
        futures = {executor.submit(fn): fn.__name__ for fn in (qwen, auxiliary, ffmpeg)}
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
                print(futures[future], "complete", flush=True)
            except Exception as exc:
                print(futures[future], "FAILED", repr(exc), flush=True)
