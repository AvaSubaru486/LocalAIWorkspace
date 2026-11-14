"""Checked resumable range downloader for build assets on slow connections."""
import argparse
import concurrent.futures
import hashlib
import pathlib
import time
import urllib.request


def download(url, target, expected_hash=None, workers=6):
    target = pathlib.Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    def request(start, end):
        return urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}", "User-Agent": "LocalAIWorkspace/1.0"})
    with urllib.request.urlopen(request(0, 0), timeout=60) as response:
        content_range = response.headers.get("Content-Range", "")
        if response.status != 206 or not content_range.startswith("bytes 0-0/"):
            raise ValueError("Server must support verified byte ranges")
        size = int(content_range.split("/")[1])
        url = response.geturl()
    parts = target.parent / (target.name + ".chunks")
    parts.mkdir(exist_ok=True)
    chunk = 8 * 1024 * 1024
    def piece(start):
        end = min(size - 1, start + chunk - 1)
        part = parts / str(start)
        if part.exists() and part.stat().st_size == end - start + 1:
            return part
        for attempt in range(8):
            try:
                with urllib.request.urlopen(request(start, end), timeout=90) as response:
                    if response.headers.get("Content-Range") != f"bytes {start}-{end}/{size}":
                        raise ValueError("Unexpected Content-Range")
                    data = response.read(end - start + 2)
                if len(data) != end - start + 1:
                    raise ValueError("Truncated range")
                part.write_bytes(data)
                return part
            except Exception:
                if attempt == 7:
                    raise
                time.sleep(1 + attempt)
    completed = 0
    with concurrent.futures.ThreadPoolExecutor(workers) as executor:
        for part in executor.map(piece, range(0, size, chunk)):
            completed += part.stat().st_size
            print(f"{target.name} {completed}/{size}", flush=True)
    partial = target.with_suffix(target.suffix + ".assembled")
    digest = hashlib.sha256()
    with partial.open("wb") as out:
        for start in range(0, size, chunk):
            data = (parts / str(start)).read_bytes()
            out.write(data)
            digest.update(data)
    if expected_hash and digest.hexdigest() != expected_hash:
        raise ValueError("SHA256 mismatch")
    partial.replace(target)
    print("COMPLETE", target, digest.hexdigest(), flush=True)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("target")
    parser.add_argument("--sha256")
    args = parser.parse_args()
    download(args.url, args.target, args.sha256)
